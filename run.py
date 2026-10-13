#!/usr/bin/env python3
"""Start AirdropOS (backend API + worker + frontend dashboard) with one command.

Works on Windows CMD, Windows PowerShell, Ubuntu and a VPS. Standard library only.

    python run.py            # Windows: py run.py
    python run.py --help

First run: creates .venv, installs Python and npm dependencies, initialises the
database, then starts everything. Press Ctrl+C once to stop all processes.

Terminal output
---------------
`run.py` is the only thing that owns the screen. When stdout is an interactive
terminal it starts one Rich dashboard (``python -m backend.app.terminal``) that
renders the whole deployment and stays put, and routes every process's log
records into the dashboard's activity feed through a shared, structured event
stream (``backend.app.terminal.events``) instead of letting them print over it.
Nothing is lost: the same records go to a rotating diagnostic log, and
``--no-dashboard`` restores the previous behaviour of streaming them to the
terminal directly.

This script stays standard-library only. It imports ``backend.app.terminal.events``
- which is itself standard-library only - to publish its own progress lines, and
never imports the application, the tool registry or a database driver.

Redirecting stdout (``python run.py > session.txt``) is the supported way to
capture a run: the dashboard is skipped and the structured logs are streamed
instead, because escape sequences written to a file would be meaningless bytes.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
IS_WIN = os.name == "nt"
VENV = ROOT / ".venv"
VENV_PY = VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")
FRONTEND = ROOT / "frontend"
STAMP = VENV / ".requirements-stamp"

# Set once the dashboard owns the screen. Everything printed after that point
# would punch a hole in its live region, so it goes to the event stream instead.
_quiet = False


def _events():
    """The shared event stream, or None if this checkout cannot provide it.

    Standard library only and import-light by design, so `run.py` never has to
    import the application to publish a line. A failure here degrades the
    dashboard's activity feed and nothing else: the platform still runs.
    """
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from backend.app.terminal import events

        return events
    except Exception:
        return None


def publish(level: str, source: str, message: str, *, proc: str = "run") -> None:
    """Record one structured event. Best effort, and silent when unavailable."""
    module = _events()
    if module is None:
        return
    try:
        module.publish(level, source, message, proc=proc)
    except Exception:
        pass


def emit(text: str) -> None:
    """Print one line without ever dying on an unencodable character.

    A child process is free to print anything, including characters the local
    code page cannot represent. Losing a glyph is cosmetic; losing the reader
    thread would silently hide every later line from that process.
    """
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(encoding, "replace").decode(encoding, "replace"), flush=True)
    except Exception:
        pass


def enable_utf8_console() -> None:
    """Let box-drawing characters survive on a Windows console.

    Delegated to the shared terminal module so the launcher and the dashboard
    cannot disagree about it; `run.py` still only imports standard library plus
    that one module.
    """
    module = _events()
    if module is not None:
        try:
            module.enable_utf8_output()
            return
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def log(msg: str) -> None:
    if not _quiet:
        emit(f"[run] {msg}")
    publish("INFO", "run", msg)


def die(msg: str, code: int = 1) -> None:
    publish("ERROR", "run", msg)
    if not _quiet:
        try:
            print(f"[run] ERROR: {msg}", file=sys.stderr, flush=True)
        except Exception:
            pass
    sys.exit(code)


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        values[key.strip()] = val
    return values


def set_env_value(path: Path, key: str, value: str) -> None:
    """Set KEY=value in a .env file (replace the line if present, else append)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    new = f"{key}={value}"
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key and not line.lstrip().startswith("#"):
            lines[i] = new
            break
    else:
        lines.append(new)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def create_env_file(env_file: Path) -> None:
    shutil.copy(ROOT / ".env.example", env_file)
    set_env_value(env_file, "SECRET_KEY", secrets.token_hex(32))
    set_env_value(env_file, "WALLET_VAULT_MASTER_KEY", secrets.token_hex(32))
    # docker compose has no default database password. A fresh .env gets a random one, and the
    # local DATABASE_URL uses the same value so `docker compose up postgres` and the app agree.
    db_password = secrets.token_hex(24)
    set_env_value(env_file, "POSTGRES_PASSWORD", db_password)
    set_env_value(env_file, "DATABASE_URL", f"postgresql+asyncpg://airdropos:{db_password}@localhost:5432/airdropos_db")


MIN_PASSWORD_LEN = 4  # must match MIN_PASSWORD_LENGTH in backend/app/security/auth.py


def set_password(env_file: Path, hidden: bool = False) -> None:
    ask = getpass.getpass if hidden else input
    pw = ""
    for _ in range(3):
        pw = ask(f"New operator password (at least {MIN_PASSWORD_LEN} characters): ")
        if len(pw) < MIN_PASSWORD_LEN:
            print(f"[run] Too short: use at least {MIN_PASSWORD_LEN} characters. Try again.", flush=True)
            continue
        if pw != ask("Repeat password: "):
            print("[run] Passwords do not match. Try again.", flush=True)
            continue
        break
    else:
        die("No valid password entered. Run: py run.py --set-password")
    code = "import sys; from backend.app.security.auth import hash_password; print(hash_password(sys.stdin.read()))"
    r = subprocess.run(
        [str(VENV_PY), "-c", code], cwd=ROOT, input=pw, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    if r.returncode != 0:
        die("Could not hash password:\n" + r.stderr)
    set_env_value(env_file, "OPERATOR_PASSWORD_HASH", "'" + r.stdout.strip() + "'")
    log("Saved OPERATOR_PASSWORD_HASH to .env. Log in with OPERATOR_EMAIL and this password.")


def update_code() -> None:
    """git pull the newest commits, then restart this script so new code and deps are used."""
    git = shutil.which("git")
    if not git:
        die("git not found. Install git first.")
    if not (ROOT / ".git").exists():
        die("This folder is not a git clone, so it cannot be updated with git pull.")
    log("Updating: git pull --ff-only")
    run_checked([git, "pull", "--ff-only"])
    cmd = [sys.executable, str(ROOT / "run.py")] + [a for a in sys.argv[1:] if a != "--update"]
    log("Update done. Refreshing dependencies and starting...")
    env = {**os.environ, "AIRDROPOS_UPDATED": "1"}
    try:
        sys.exit(subprocess.run(cmd, cwd=ROOT, env=env).returncode)
    except KeyboardInterrupt:
        sys.exit(0)


def run_checked(cmd: list[str], cwd: Path = ROOT, env: dict | None = None) -> None:
    log("$ " + " ".join(cmd))
    result = subprocess.run(cmd, cwd=cwd, env=env)
    if result.returncode != 0:
        die(f"command failed ({result.returncode}): {' '.join(cmd)}")


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _listening_pids(port: int) -> set[int]:
    """PIDs of processes listening on a TCP port (empty if none, or if no lookup tool exists)."""
    pids: set[int] = set()
    try:
        if IS_WIN:
            r = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, errors="replace")
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) < 5 or parts[0].upper() != "TCP":
                    continue
                local, foreign, pid = parts[1], parts[2], parts[-1]
                # A listening socket has no remote peer (0.0.0.0:0 / [::]:0). Checking that instead of the
                # word LISTENING keeps this working on non-English Windows.
                if local.rsplit(":", 1)[-1] == str(port) and foreign.rsplit(":", 1)[-1] == "0" and pid.isdigit():
                    pids.add(int(pid))
        elif shutil.which("lsof"):
            r = subprocess.run(
                ["lsof", "-t", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True
            )
            pids.update(int(x) for x in r.stdout.split() if x.isdigit())
        elif shutil.which("ss"):
            r = subprocess.run(["ss", "-ltnpH", f"sport = :{port}"], capture_output=True, text=True)
            pids.update(int(x) for x in re.findall(r"pid=(\d+)", r.stdout))
    except OSError:
        pass
    return pids


def _process_table() -> dict[int, tuple[int, str]]:
    """Windows only: {pid: (parent_pid, command_line)} for every running process."""
    ps = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        data = json.loads(r.stdout) if r.stdout.strip() else []
    except (OSError, ValueError):
        return {}
    if isinstance(data, dict):  # PowerShell emits a bare object when there is only one row
        data = [data]
    table: dict[int, tuple[int, str]] = {}
    for row in data:
        try:
            table[int(row["ProcessId"])] = (int(row.get("ParentProcessId") or 0), row.get("CommandLine") or "")
        except (KeyError, TypeError, ValueError):
            continue
    return table


def _cmdline(pid: int) -> str:
    try:
        r = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True)
        return r.stdout.strip()
    except OSError:
        return ""


def _is_ours(pid: int, table: dict[int, tuple[int, str]] | None = None, root: str | None = None) -> bool:
    """True if this process, or one of its ancestors, was started from this project folder.

    On Windows the venv's python.exe is only a launcher: it spawns the *base* python, whose command
    line has no project path. So the process that owns the port looks foreign when judged alone.
    Walking up to the launcher (whose command line does contain the venv path) fixes that.
    """
    norm = lambda s: s.replace("\\", "/").lower()  # noqa: E731
    marker = norm(root if root is not None else str(ROOT))
    if not IS_WIN and table is None:
        cmd = _cmdline(pid)
        return bool(cmd) and marker in norm(cmd)
    if table is None:
        table = _process_table()
    seen: set[int] = set()
    cur = pid
    for _ in range(8):  # bounded walk; also guards against PID-reuse cycles
        if cur in seen or cur not in table:
            break
        seen.add(cur)
        ppid, cmd = table[cur]
        if cmd and marker in norm(cmd):
            return True
        cur = ppid
    return False


def _kill_tree(pid: int) -> None:
    if IS_WIN:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return
    try:
        pgid = os.getpgid(pid)
        if pgid != os.getpgid(0):
            os.killpg(pgid, signal.SIGTERM)  # children were started in their own session
        else:
            os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    for _ in range(20):  # up to ~5s to exit cleanly, then force
        time.sleep(0.25)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def free_ports(ports: dict[int, str]) -> None:
    """Stop leftover AirdropOS processes that still hold our ports from an earlier run.

    Only processes whose command line points into this project folder are stopped. Anything else
    that uses a port is left alone and reported, so an unrelated program is never killed.
    """
    me = os.getpid()
    killed: set[int] = set()
    table = _process_table() if IS_WIN else None
    for port, name in ports.items():
        for pid in sorted(_listening_pids(port) - {me}):
            if _is_ours(pid, table):
                log(f"Port {port} ({name}) is held by an old AirdropOS process (PID {pid}). Stopping it...")
                _kill_tree(pid)
                killed.add(port)
            else:
                log(f"WARNING: port {port} ({name}) is used by another program (PID {pid}). Not touching it.")
    deadline = time.time() + 10
    while killed and time.time() < deadline:
        killed = {p for p in killed if _listening_pids(p) - {me}}
        if killed:
            time.sleep(0.5)
    if killed:
        log(f"WARNING: port(s) {', '.join(map(str, sorted(killed)))} still busy after stopping the old process.")


def ensure_infra(env_vals: dict[str, str]) -> None:
    """Make sure the database is reachable; start PostgreSQL with Docker if it is local.

    SQLite needs nothing. Redis is not used by the code, so it is not checked.
    """
    url = urlparse(env_vals.get("DATABASE_URL", "postgresql+asyncpg://x@localhost:5432/x"))
    if url.scheme.startswith("sqlite"):
        return
    host, port = url.hostname or "localhost", url.port or 5432
    if port_open(host, port):
        return

    if host in ("localhost", "127.0.0.1", "::1") and shutil.which("docker"):
        if not (env_vals.get("POSTGRES_PASSWORD") or os.environ.get("POSTGRES_PASSWORD")):
            die(
                "POSTGRES_PASSWORD is not set in .env, and docker compose no longer has a built-in default.\n"
                "       - New database: add POSTGRES_PASSWORD=<random value> (openssl rand -hex 24) to .env and use\n"
                "         the same password in DATABASE_URL.\n"
                "       - Existing database from an older version: set POSTGRES_PASSWORD to the password that database\n"
                "         was created with (the one in your DATABASE_URL). It is only read when the data volume is first\n"
                "         created, so it will NOT change an existing database's password. Rotate it afterwards with\n"
                "         ALTER USER, then update both values.\n"
                "       - Or run without Docker: py run.py --sqlite"
            )
        log("PostgreSQL is not running. Starting it with Docker...")
        r = subprocess.run(["docker", "compose", "up", "-d", "postgres"], cwd=ROOT)
        if r.returncode != 0:
            die(
                "Docker could not start PostgreSQL.\n"
                "       - Open Docker Desktop and wait until it says 'Engine running', then run this command again.\n"
                "       - Or run without Docker: py run.py --sqlite"
            )
        deadline = time.time() + 60
        while time.time() < deadline:
            if port_open(host, port):
                time.sleep(2)  # let Postgres finish its first init
                return
            time.sleep(1)
    die(
        f"Cannot reach the database at {host}:{port}.\n"
        "       Start Docker Desktop and run again, or run without Docker: py run.py --sqlite"
    )


def ensure_python_env(skip_install: bool) -> None:
    if not VENV_PY.exists():
        log("Creating virtual environment (.venv)...")
        run_checked([sys.executable, "-m", "venv", str(VENV)])
    req = ROOT / "backend" / "requirements.txt"
    stale = (
        not STAMP.exists()
        or STAMP.stat().st_mtime < req.stat().st_mtime
        or bool(os.environ.get("AIRDROPOS_UPDATED"))
    )
    if stale and not skip_install:
        log("Installing Python dependencies (first run takes a few minutes)...")
        run_checked([str(VENV_PY), "-m", "pip", "install", "-q", "--upgrade", "pip"])
        run_checked([str(VENV_PY), "-m", "pip", "install", "-q", "-r", str(req)])
        log("Installing Playwright Chromium (browser tools)...")
        r = subprocess.run([str(VENV_PY), "-m", "playwright", "install", "chromium"], cwd=ROOT)
        if r.returncode != 0:
            log("WARNING: Playwright install failed. Browser tools will be unavailable.")
        STAMP.write_text(str(time.time()))


def ensure_node() -> str:
    npm = shutil.which("npm")
    if not npm:
        die("Node.js/npm not found. Install Node.js 20+ from https://nodejs.org, or run with --no-frontend.")
    if not (FRONTEND / "node_modules").exists() or os.environ.get("AIRDROPOS_UPDATED"):
        log("Installing frontend dependencies (npm install)...")
        run_checked([npm, "install"], cwd=FRONTEND)
    return npm


def _env_flag(env_vals: dict[str, str], key: str) -> bool:
    """Read a boolean out of the already-parsed .env values.

    Only the spellings an operator realistically writes are accepted; anything else
    is False, which is the safe answer for a logging toggle.
    """
    return env_vals.get(key, "").strip().lower() in ("1", "true", "yes", "on")


def uvicorn_command(venv_python: Path, host: str, port: str, *, access_log: bool, reload: bool) -> list[str]:
    """Build the backend command line.

    The access log prints one line per HTTP request. The dashboard polls several
    endpoints every few seconds, so it produced far more output than everything else
    combined and pushed warnings and errors off the screen. It is off unless it is
    explicitly asked for, which it can be from .env (ACCESS_LOG) or with --access-log.
    Nothing else is filtered: server warnings, errors and tracebacks still print.
    """
    cmd = [str(venv_python), "-m", "uvicorn", "backend.app.main:app", "--host", host, "--port", port]
    if reload:
        cmd.append("--reload")
    if not access_log:
        cmd.append("--no-access-log")
    return cmd


def _line_level(line: str) -> str:
    """Severity for a line of a child's unstructured output.

    Only for output with no Python logging behind it (the Vite dev server, and
    uvicorn's own logger, which keeps its own handler). Anything that does log
    reaches the event stream through its own handlers, so this never has to guess
    about the API or the worker.

    The severity word has to LEAD the line, which is how node and vite emit it.
    Matching the word anywhere would turn a request for `/api/v1/errors` into an
    error event.
    """
    low = line.strip().lower()
    if low.startswith("critical") or "traceback (most recent call last)" in low:
        return "CRITICAL"
    if low.startswith(("error", "err!", "fatal")) or low.endswith("exception"):
        return "ERROR"
    if low.startswith(("warn", "warning")):
        return "WARNING"
    return "INFO"


class Proc:
    """One managed child process.

    stdout and stderr are normally captured so the lines can be published as
    structured events instead of being printed over the dashboard.
    ``capture_stdout=False`` hands the child the real console, which is what the
    dashboard needs: Rich only redraws in place when stdout is a terminal, and a
    pipe would make it dump a full screen of text on every refresh.
    """

    def __init__(
        self,
        name: str,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
        *,
        capture_stdout: bool = True,
        pipe_stdin: bool = False,
    ):
        self.name = name
        kwargs: dict = {}
        if IS_WIN:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        self.p = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            # stdout and stderr are merged into ONE pipe on purpose. Draining them
            # with two sequential readers deadlocks: a process that writes nothing
            # to stdout (uvicorn logs to stderr) leaves the stdout reader blocked
            # forever, and its stderr fills the pipe buffer, so the service looks
            # silent until it exits.
            stdout=subprocess.PIPE if capture_stdout else None,
            stderr=subprocess.STDOUT if capture_stdout else None,
            stdin=subprocess.PIPE if pipe_stdin else None,
            **kwargs,
        )
        self.t = threading.Thread(target=self._pump, daemon=True)
        self.t.start()

    def _pump(self) -> None:
        if self.p.stdout is None:
            return
        for raw in iter(self.p.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not _quiet:
                emit(f"[{self.name}] {line}")
            publish(_line_level(line), self.name, line, proc=self.name)

    def close_stdin(self) -> None:
        """Ask a child to shut down cleanly (the dashboard watches for EOF)."""
        try:
            if self.p.stdin is not None and not self.p.stdin.closed:
                self.p.stdin.close()
        except (OSError, ValueError):
            pass

    def stop(self) -> None:
        if self.p.poll() is not None:
            return
        try:
            if IS_WIN:
                subprocess.run(
                    ["taskkill", "/PID", str(self.p.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            else:
                os.killpg(os.getpgid(self.p.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            if not IS_WIN:
                try:
                    os.killpg(os.getpgid(self.p.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass


def run_capture(cmd: list[str], env: dict[str, str], cwd: Path = ROOT) -> int:
    """Run a command to completion, echoing its output to the terminal.

    Used for the database bootstrap, which has to finish before the dashboard can
    describe anything. The child's log records reach the event stream through its
    own handlers, so echoing here does not duplicate them: it only replaces an
    inherited console handle with a captured one.
    """
    log("$ " + " ".join(cmd))
    # Name the process after the module it runs, so its events are attributed to
    # `init_db` rather than to the `-m` flag in front of it.
    name = cmd[2].rsplit(".", 1)[-1] if len(cmd) > 2 and cmd[1] == "-m" else "child"
    result = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=dict(env, AIRDROPOS_PROC=name),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert result.stdout is not None
    for raw in iter(result.stdout.readline, b""):
        emit("  " + raw.decode("utf-8", errors="replace").rstrip())
    result.wait()
    return result.returncode


# ----------------------------------------------------------------------
# Dashboard
# ----------------------------------------------------------------------
DASHBOARD_READY_TIMEOUT_SECONDS = 30.0


def _terminal_is_interactive() -> bool:
    """Whether this terminal may be taken over.

    A pipe, a redirect and `TERM=dumb` all mean escape sequences would be written
    as literal bytes into a file instead of moving a cursor, so the services
    print their logs directly, exactly as they always have. That is the supported
    way to capture a session, and it needs no flag.
    """
    try:
        if not sys.stdout.isatty():
            return False
    except Exception:
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    return os.environ.get("AIRDROPOS_DASHBOARD", "").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _ready_file() -> Optional[Path]:
    module = _events()
    if module is None:
        return None
    return module.LOG_DIR / "dashboard.ready"


def _dashboard_ready(path: Optional[Path], token: str) -> bool:
    """Whether the dashboard owns the screen yet.

    Matched on a token rather than a pid on purpose. On Windows the venv's
    ``python.exe`` is a launcher that spawns the real interpreter as a child, so
    the pid ``run.py`` holds is not the pid the dashboard reports from the
    inside. The token is generated here and handed over on the command line, so
    it is this run's dashboard and no other. A file left behind by a killed run
    therefore cannot make this one think the screen is taken.
    """
    if path is None:
        return False
    try:
        return path.read_text(encoding="utf-8").strip().endswith(token)
    except OSError:
        return False


def start_dashboard(
    env: dict[str, str],
    *,
    started_at: float,
    backend_url: str,
    api_prefix: str,
    frontend_url: str,
    gateway_host: str,
    gateway_port: int,
    interactive: bool,
) -> Optional[Proc]:
    """Start the dashboard, or return None to keep printing to the terminal.

    Returning None is a supported outcome, not a failure: stdout may be a pipe,
    the terminal may be dumb, or the dashboard may be unable to start. The
    services are then launched exactly as they were before it existed.
    """
    if not interactive or not VENV_PY.exists():
        return None
    ready = _ready_file()
    if ready is None:
        # Without the shared event stream there is no activity feed to render and
        # no handshake file to wait on; stream the logs instead.
        log("The shared event stream is unavailable; streaming service logs instead.")
        return None
    token = secrets.token_hex(8)
    cmd = [
        str(VENV_PY), "-m", "backend.app.terminal",
        "--started", str(started_at),
        "--ready-token", token,
        "--backend-url", backend_url,
        "--api-prefix", api_prefix,
        "--frontend-url", frontend_url,
        "--gateway-host", gateway_host,
        "--gateway-port", str(gateway_port),
    ]
    dashboard_env = dict(env)
    dashboard_env["AIRDROPOS_PROC"] = "dashboard"
    dashboard_env.pop("AIRDROPOS_QUIET", None)
    proc = Proc("dashboard", cmd, ROOT, dashboard_env, capture_stdout=False, pipe_stdin=True)

    deadline = time.time() + DASHBOARD_READY_TIMEOUT_SECONDS
    while time.time() < deadline:
        if _dashboard_ready(ready, token):
            # Not printed: the dashboard has cleared the screen and owns it now,
            # so a line here would land on top of the first frame. It goes to the
            # activity feed and logs/airdropos.log instead.
            publish("INFO", "run", "Terminal dashboard ready.")
            return proc
        if proc.p.poll() is not None:
            log(
                f"The dashboard exited immediately (code {proc.p.returncode}); "
                "using plain logs on this terminal."
            )
            return None
        time.sleep(0.2)

    log("The dashboard did not take over the screen in time; stopping it and using plain logs.")
    proc.stop()
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Run AirdropOS backend + worker + frontend.")
    ap.add_argument("--no-worker", action="store_true", help="do not start the background worker")
    ap.add_argument("--no-frontend", action="store_true", help="do not start the web dashboard (Vite dev server)")
    ap.add_argument("--sqlite", action="store_true", help="use a local SQLite file instead of PostgreSQL (no Docker needed); saved in .env")
    ap.add_argument("--no-infra", action="store_true", help="do not check/start PostgreSQL and Redis")
    ap.add_argument("--skip-install", action="store_true", help="skip pip/npm install steps")
    ap.add_argument("--update", action="store_true", help="git pull the newest commits, refresh dependencies, then start")
    ap.add_argument("--set-password", action="store_true", help="set the operator login password in .env, then exit")
    ap.add_argument("--hide-password", action="store_true", help="with --set-password: do not show the password while typing")
    ap.add_argument("--host", default="127.0.0.1", help="backend bind address (default 127.0.0.1)")
    ap.add_argument("--reload", action="store_true", help="auto-reload backend on code changes")
    ap.add_argument(
        "--access-log",
        action="store_true",
        help="print one line per HTTP request (off by default; also settable with ACCESS_LOG=true in .env)",
    )
    ap.add_argument("--keep-existing", action="store_true", help="do not stop old AirdropOS processes that still hold the ports")
    ap.add_argument(
        "--no-dashboard",
        action="store_true",
        help="stream service logs to the terminal instead of the live dashboard",
    )
    args = ap.parse_args()

    if sys.version_info < (3, 10):
        die("Python 3.10+ is required (3.11 recommended).")

    # Before anything prints: the dashboard and the captured service output are
    # both full of characters a default Windows code page cannot represent.
    enable_utf8_console()

    if args.update and not os.environ.get("AIRDROPOS_UPDATED"):
        update_code()

    # One event stream per run. Truncating before anything else starts means the
    # dashboard's activity feed is exactly this run, never a previous one's.
    events = _events()
    if events is not None:
        try:
            events.reset_event_stream()
        except Exception:
            pass

    env_file = ROOT / ".env"
    if not env_file.exists():
        create_env_file(env_file)
        log("Created .env with new random SECRET_KEY and WALLET_VAULT_MASTER_KEY.")
        log("BACK UP .env. If WALLET_VAULT_MASTER_KEY is lost, stored wallet keys cannot be decrypted.")

    if args.set_password:
        ensure_python_env(args.skip_install)
        set_password(env_file, hidden=args.hide_password)
        return

    if args.sqlite:
        set_env_value(env_file, "DATABASE_URL", "sqlite+aiosqlite:///./airdropos.db")
        log("DATABASE_URL set to SQLite file ./airdropos.db (saved in .env).")
    env_vals = read_env(env_file)
    if not env_vals.get("OPERATOR_PASSWORD_HASH") and env_vals.get("AUTH_ENABLED", "true").lower() != "false":
        log("WARNING: OPERATOR_PASSWORD_HASH is empty in .env. Run: python run.py --set-password")

    if not args.no_infra:
        ensure_infra(env_vals)
    ensure_python_env(args.skip_install)
    npm = None if args.no_frontend else ensure_node()

    started_at = time.time()
    child_env = os.environ.copy()
    child_env["PYTHONUNBUFFERED"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["FORCE_COLOR"] = "0"
    # The launcher owns rotation of the shared diagnostic log, and only the
    # processes it starts may rename it.
    child_env["AIRDROPOS_LOG_OWNER"] = "1"

    host = args.host
    port = env_vals.get("BACKEND_PORT", "8000")
    gateway_host = env_vals.get("LIVE_GATEWAY_HOST", "127.0.0.1")
    gateway_port = env_vals.get("LIVE_GATEWAY_PORT", "8765")

    if not args.keep_existing:
        # A previous `py run.py` that is still running (another terminal, closed window, crash) would
        # hold these ports. Stop it so this run starts fresh instead of clashing with it.
        ports: dict[int, str] = {int(port): "backend"}
        if not args.no_worker and env_vals.get("LIVE_GATEWAY_ENABLED", "true").lower() != "false":
            ports[int(gateway_port)] = "worker gateway"
        if npm:
            ports[5173] = "dashboard"
        free_ports(ports)

    log("Initialising database...")
    if run_capture([str(VENV_PY), "-m", "backend.app.services.init_db"], child_env) != 0:
        die("Database initialisation failed. See the output above.")

    # The dashboard comes up before the services so it can report their startup
    # as it happens, which is what an operator watching a cold start needs.
    dashboard = start_dashboard(
        child_env,
        started_at=started_at,
        backend_url=f"http://127.0.0.1:{port}",
        api_prefix=env_vals.get("API_V1_PREFIX", "/api/v1"),
        frontend_url="http://127.0.0.1:5173" if npm else "",
        gateway_host=gateway_host,
        gateway_port=int(gateway_port),
        interactive=not args.no_dashboard and _terminal_is_interactive(),
    )
    if dashboard is not None:
        # From here the dashboard owns the screen, so the services stop printing
        # to it and report through the shared event stream instead.
        globals()["_quiet"] = True
        child_env["AIRDROPOS_QUIET"] = "1"

    procs: list[Proc] = []
    uvicorn_cmd = uvicorn_command(
        VENV_PY,
        host,
        port,
        access_log=args.access_log or _env_flag(env_vals, "ACCESS_LOG"),
        reload=args.reload,
    )
    procs.append(Proc("backend", uvicorn_cmd, ROOT, dict(child_env, AIRDROPOS_PROC="backend")))
    if not args.no_worker:
        procs.append(
            Proc(
                "worker",
                [str(VENV_PY), "-m", "backend.app.scheduler.worker"],
                ROOT,
                dict(child_env, AIRDROPOS_PROC="worker"),
            )
        )
    if npm:
        procs.append(Proc("web", [npm, "run", "dev"], FRONTEND, dict(child_env, AIRDROPOS_PROC="web")))

    log(f"Backend API : http://localhost:{port}   (docs: /docs)")
    if npm:
        log("Dashboard   : http://localhost:5173")
    log("Press Ctrl+C to stop everything.")

    def _graceful(signum, frame):  # Ctrl+C, `kill`, systemd stop
        raise KeyboardInterrupt

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _graceful)

    exit_code = 0
    try:
        while True:
            for pr in procs:
                rc = pr.p.poll()
                if rc is not None:
                    log(f"'{pr.name}' exited with code {rc}. Stopping everything.")
                    exit_code = rc or 1
                    raise KeyboardInterrupt
            if dashboard is not None and dashboard.p.poll() is not None:
                # The dashboard is the operator's only view. If it dies, say so
                # and stop rather than leaving services running invisibly.
                log(
                    f"The terminal dashboard exited with code {dashboard.p.returncode}. "
                    "Stopping everything; see logs/airdropos.log for details."
                )
                exit_code = dashboard.p.returncode or 1
                raise KeyboardInterrupt
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        # The dashboard goes first: closing its stdin lets it leave the live
        # display cleanly, which restores the cursor and clears the screen before
        # anything else prints.
        if dashboard is not None:
            dashboard.close_stdin()
            try:
                dashboard.p.wait(timeout=6)
            except subprocess.TimeoutExpired:
                pass
        globals()["_quiet"] = False
        log("Stopping...")
        for pr in reversed(procs):
            pr.stop()
        if dashboard is not None:
            dashboard.stop()
        log("Stopped.")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
