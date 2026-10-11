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
import os
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


MIN_PASSWORD_LEN = 12  # must match hash_password() in backend/app/security/auth.py


def set_password(env_file: Path) -> None:
    pw = ""
    for _ in range(3):
        pw = getpass.getpass(f"New operator password (at least {MIN_PASSWORD_LEN} characters): ")
        if len(pw) < MIN_PASSWORD_LEN:
            print(f"[run] Too short: use at least {MIN_PASSWORD_LEN} characters. Try again.", flush=True)
            continue
        if pw != getpass.getpass("Repeat password: "):
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


def ensure_infra(env_vals: dict[str, str]) -> None:
    """Make sure PostgreSQL and Redis are reachable; start them with Docker if local."""
    targets = []
    for name, default in (
        ("DATABASE_URL", "postgresql+asyncpg://x@localhost:5432/x"),
        ("REDIS_URL", "redis://localhost:6379/0"),
    ):
        url = urlparse(env_vals.get(name, default))
        if url.scheme.startswith("sqlite"):
            continue
        targets.append((name, url.hostname or "localhost", url.port or (6379 if "redis" in url.scheme else 5432)))

    missing = [t for t in targets if not port_open(t[1], t[2])]
    if not missing:
        return

    local_only = all(host in ("localhost", "127.0.0.1", "::1") for _, host, _ in missing)
    if local_only and shutil.which("docker"):
        log("PostgreSQL/Redis not running. Starting them with Docker...")
        run_checked(["docker", "compose", "up", "-d", "postgres", "redis"])
        deadline = time.time() + 60
        while time.time() < deadline:
            missing = [t for t in missing if not port_open(t[1], t[2])]
            if not missing:
                time.sleep(2)  # let Postgres finish its first init
                return
            time.sleep(1)
    for name, host, port in missing:
        print(f"[run] {name}: cannot reach {host}:{port}", file=sys.stderr)
    die(
        "PostgreSQL and Redis must be running. Install Docker (then re-run this command), "
        "or start them yourself, or use: docker compose up -d --build"
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
    ap.add_argument("--no-infra", action="store_true", help="do not check/start PostgreSQL and Redis")
    ap.add_argument("--skip-install", action="store_true", help="skip pip/npm install steps")
    ap.add_argument("--update", action="store_true", help="git pull the newest commits, refresh dependencies, then start")
    ap.add_argument("--set-password", action="store_true", help="set the operator login password in .env, then exit")
    ap.add_argument("--host", default="127.0.0.1", help="backend bind address (default 127.0.0.1)")
    ap.add_argument("--reload", action="store_true", help="auto-reload backend on code changes")
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
        set_password(env_file)
        return

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

    log("Initialising database...")
    run_checked([str(VENV_PY), "-m", "backend.app.services.init_db"], env=child_env)

    host = args.host
    port = env_vals.get("BACKEND_PORT", "8000")

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
