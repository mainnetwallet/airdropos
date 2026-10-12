#!/usr/bin/env python3
"""Start AirdropOS (backend API + worker + frontend dashboard) with one command.

Works on Windows CMD, Windows PowerShell, Ubuntu and a VPS. Standard library only.

    python run.py            # Windows: py run.py
    python run.py --help

First run: creates .venv, installs Python and npm dependencies, initialises the
database, then starts everything. Press Ctrl+C once to stop all processes.
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
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
IS_WIN = os.name == "nt"
VENV = ROOT / ".venv"
VENV_PY = VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")
FRONTEND = ROOT / "frontend"
STAMP = VENV / ".requirements-stamp"


def log(msg: str) -> None:
    print(f"[run] {msg}", flush=True)


def die(msg: str, code: int = 1) -> None:
    print(f"[run] ERROR: {msg}", file=sys.stderr, flush=True)
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


class Proc:
    def __init__(self, name: str, cmd: list[str], cwd: Path, env: dict[str, str]):
        self.name = name
        kwargs: dict = {}
        if IS_WIN:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        self.p = subprocess.Popen(
            cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs
        )
        self.t = threading.Thread(target=self._pump, daemon=True)
        self.t.start()

    def _pump(self) -> None:
        assert self.p.stdout is not None
        for raw in iter(self.p.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            print(f"[{self.name}] {line}", flush=True)

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


def main() -> None:
    ap = argparse.ArgumentParser(description="Run AirdropOS backend + worker + frontend.")
    ap.add_argument("--no-worker", action="store_true", help="do not start the background worker")
    ap.add_argument("--no-frontend", action="store_true", help="do not start the dashboard")
    ap.add_argument("--sqlite", action="store_true", help="use a local SQLite file instead of PostgreSQL (no Docker needed); saved in .env")
    ap.add_argument("--no-infra", action="store_true", help="do not check/start PostgreSQL and Redis")
    ap.add_argument("--skip-install", action="store_true", help="skip pip/npm install steps")
    ap.add_argument("--update", action="store_true", help="git pull the newest commits, refresh dependencies, then start")
    ap.add_argument("--set-password", action="store_true", help="set the operator login password in .env, then exit")
    ap.add_argument("--hide-password", action="store_true", help="with --set-password: do not show the password while typing")
    ap.add_argument("--host", default="127.0.0.1", help="backend bind address (default 127.0.0.1)")
    ap.add_argument("--reload", action="store_true", help="auto-reload backend on code changes")
    ap.add_argument("--keep-existing", action="store_true", help="do not stop old AirdropOS processes that still hold the ports")
    args = ap.parse_args()

    if sys.version_info < (3, 10):
        die("Python 3.10+ is required (3.11 recommended).")

    if args.update and not os.environ.get("AIRDROPOS_UPDATED"):
        update_code()

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

    child_env = os.environ.copy()
    child_env["PYTHONUNBUFFERED"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["FORCE_COLOR"] = "0"

    host = args.host
    port = env_vals.get("BACKEND_PORT", "8000")

    if not args.keep_existing:
        # A previous `py run.py` that is still running (another terminal, closed window, crash) would
        # hold these ports. Stop it so this run starts fresh instead of clashing with it.
        ports: dict[int, str] = {int(port): "backend"}
        if not args.no_worker and env_vals.get("LIVE_GATEWAY_ENABLED", "true").lower() != "false":
            ports[int(env_vals.get("LIVE_GATEWAY_PORT", "8765"))] = "worker gateway"
        if npm:
            ports[5173] = "dashboard"
        free_ports(ports)

    log("Initialising database...")
    run_checked([str(VENV_PY), "-m", "backend.app.services.init_db"], env=child_env)

    procs: list[Proc] = []
    uvicorn_cmd = [str(VENV_PY), "-m", "uvicorn", "backend.app.main:app", "--host", host, "--port", port]
    if args.reload:
        uvicorn_cmd.append("--reload")
    procs.append(Proc("backend", uvicorn_cmd, ROOT, child_env))
    if not args.no_worker:
        procs.append(Proc("worker", [str(VENV_PY), "-m", "backend.app.scheduler.worker"], ROOT, child_env))
    if npm:
        procs.append(Proc("web", [npm, "run", "dev"], FRONTEND, child_env))

    log("-" * 60)
    log(f"Backend API : http://localhost:{port}   (docs: /docs)")
    if npm:
        log("Dashboard   : http://localhost:5173")
    log("Press Ctrl+C to stop everything.")
    log("-" * 60)

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
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        log("Stopping...")
        for pr in reversed(procs):
            pr.stop()
        log("Stopped.")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
