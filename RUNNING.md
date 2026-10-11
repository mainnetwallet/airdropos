# Running AirdropOS

Two ways. Both start **backend + worker + frontend with one command.**

| | Command | Needs |
|---|---|---|
| **A. Without Docker for the app** (Windows / Ubuntu) | `python run.py` | Python 3.10+, Node.js 20+, Docker (only for PostgreSQL + Redis) |
| **B. Docker** (Windows / Ubuntu / **VPS**) | `docker compose up -d --build` | Docker with Compose v2 |

Clone first:

```
git clone https://github.com/mainnetwallet/airdropos.git
cd airdropos
```

---

## A. One command: `python run.py`

### Windows CMD

```cmd
py run.py --set-password
py run.py
```

### Windows PowerShell

```powershell
py run.py --set-password
py run.py
```

### Ubuntu

```bash
sudo apt update && sudo apt install -y python3 python3-venv nodejs npm git   # Node 20+ required
python3 run.py --set-password
python3 run.py
```

What the two commands do:

1. **`run.py --set-password`** (once): creates `.env` with fresh random `SECRET_KEY` and `WALLET_VAULT_MASTER_KEY`, installs Python packages, asks for your login password (at least 12 characters) and saves its hash into `.env`.
2. **`run.py`**: starts everything.

After the first time, you only ever run `python run.py` (Windows: `py run.py`).

Edit `.env` once and set `LLM_API_KEY` (and `LLM_BASE_URL`, `LLM_MODEL_ID` if not OpenAI).
**Back up `.env`.** If `WALLET_VAULT_MASTER_KEY` is lost, stored wallet keys cannot be decrypted.

### What `run.py` does

1. Creates `.env` (random keys) if missing.
2. Starts PostgreSQL + Redis with Docker if they are not already running.
3. Creates `.venv`, installs Python packages, installs Playwright Chromium (first run only).
4. Runs `npm install` for the dashboard (first run only).
5. Initialises the database.
6. Starts the **backend** (`:8000`), the **worker**, and the **dashboard** (`:5173`), with prefixed logs: `[backend]`, `[worker]`, `[web]`.
7. **Ctrl+C stops all three.** If one of them crashes, the rest are stopped too and the reason is shown.

Open:

- Dashboard: http://localhost:5173
- API docs: http://localhost:8000/docs

Options:

| Option | Meaning |
|---|---|
| `--update` | `git pull` the newest commits, refresh pip/npm packages, then start |
| `--set-password` | Set the operator login password, then exit |
| `--no-worker` | Do not start the background worker |
| `--no-frontend` | Backend + worker only (no Node.js needed) |
| `--no-infra` | Do not check/start PostgreSQL and Redis (use your own, or SQLite in `DATABASE_URL`) |
| `--skip-install` | Skip pip install |
| `--reload` | Auto-reload backend on code changes (development) |
| `--host 0.0.0.0` | Bind backend to all interfaces (default `127.0.0.1`) |

### Update to the newest version (new commits)

Stop the running app (Ctrl+C), then:

```bash
python run.py --update        # Windows: py run.py --update
```

This runs `git pull --ff-only`, reinstalls Python and npm packages, and starts everything again. Your `.env` and data are not touched. If `git pull` refuses because you edited tracked files, run `git stash` (or commit your change) first.

### First login

The operator account is created once from `OPERATOR_EMAIL` and the password hash.

Ubuntu / CMD (`curl.exe`) / Git Bash:
```bash
curl -X POST http://localhost:8000/api/v1/auth/bootstrap
```
PowerShell:
```powershell
Invoke-RestMethod -Method Post http://localhost:8000/api/v1/auth/bootstrap
```

Then log in on the dashboard with `OPERATOR_EMAIL` (default `operator@airdropos.local`) and the password you set.
If it says an account already exists, just log in.

---

## B. Docker (also the VPS method)

### 1. Create `.env`

Windows CMD: `copy .env.example .env`
Windows PowerShell: `Copy-Item .env.example .env`
Ubuntu / VPS: `cp .env.example .env`

Then edit it (`notepad .env` on Windows, `nano .env` on Linux) and set:

| Variable | What to do |
|---|---|
| `SECRET_KEY` | Random string, 32+ chars |
| `WALLET_VAULT_MASTER_KEY` | Exactly 64 hex chars. **Back it up.** |
| `LLM_API_KEY` | Your LLM provider key |
| `OPERATOR_PASSWORD_HASH` | Hash of your password, in single quotes (below) |

Random value:
```bash
openssl rand -hex 32                                               # Ubuntu / VPS / Git Bash
```
```powershell
-join ((1..32) | ForEach-Object { '{0:x2}' -f (Get-Random -Maximum 256) })   # PowerShell
```

Password hash:
```bash
docker compose run --rm backend python -c "from backend.app.security.auth import hash_password; print(hash_password('YOUR-PASSWORD'))"
```
Paste the result in `.env` with single quotes (it contains `$`):
```
OPERATOR_PASSWORD_HASH='pbkdf2$390000$....$....'
```

Leave `DATABASE_URL` and `REDIS_URL` as they are; Docker overrides them with the container hostnames.

### 2. Start

```bash
docker compose up -d --build
docker compose exec backend python -m backend.app.services.init_db
curl -X POST http://localhost:8000/api/v1/auth/bootstrap
```

Dashboard: http://localhost:3000

Useful:
```bash
docker compose ps
docker compose logs -f backend worker
docker compose down                # stop (data is kept)
```

Update to the newest commits (Docker, VPS):
```bash
git pull && docker compose up -d --build
```

---

## VPS (Ubuntu 22.04 / 24.04)

```bash
# Firewall
sudo apt update && sudo apt upgrade -y
sudo apt install -y git curl ufw
sudo ufw allow OpenSSH
sudo ufw allow 3000/tcp
sudo ufw enable

# Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker

# App
git clone https://github.com/mainnetwallet/airdropos.git
cd airdropos
cp .env.example .env
nano .env        # SECRET_KEY, WALLET_VAULT_MASTER_KEY, LLM_API_KEY, OPERATOR_PASSWORD_HASH
                 # keep AUTH_ENABLED=true, set ENVIRONMENT=production
docker compose up -d --build
docker compose exec backend python -m backend.app.services.init_db
curl -X POST http://localhost:8000/api/v1/auth/bootstrap
```

Open `http://YOUR_VPS_IP:3000`.

- Postgres (5432), Redis (6379) and the API (8000) are bound to `127.0.0.1` in `docker-compose.yml`, so they are not reachable from the internet. Only the dashboard (3000) is. Docker bypasses `ufw`, so do not remove those `127.0.0.1:` bindings.
- All services use `restart: unless-stopped`, so they come back after a reboot.
- Port 3000 is plain HTTP, so your login travels unencrypted. Before real use, put HTTPS (Caddy, or nginx + Let's Encrypt) in front, or use an SSH tunnel: `ssh -L 3000:localhost:3000 user@YOUR_VPS_IP` and open http://localhost:3000.
- `python run.py` also works on a VPS, but it starts the Vite **dev** server. Use Docker for a VPS.
- Back up `.env` and the Postgres volume. See `docs/DATABASE_BACKUP.md`.

---

## Run the tests

```bash
pip install -r backend/requirements.txt
python -m pytest backend/tests/ -q
```

## Common problems

| Problem | Fix |
|---|---|
| `Node.js/npm not found` | Install Node.js 20+ from https://nodejs.org, or use `--no-frontend` |
| `PostgreSQL and Redis must be running` | Install Docker, or start them yourself, or use Method B |
| Cannot log in / bootstrap returns 503 | Run `python run.py --set-password` (Docker: set `OPERATOR_PASSWORD_HASH` in `.env`) |
| `ModuleNotFoundError: backend` | Run from the repository root (the folder with `run.py`) |
| `WALLET_VAULT_MASTER_KEY must be exactly 64 hex characters` | Use `openssl rand -hex 32` |
| Port 8000 / 5173 / 3000 in use | Stop the other program, or change the port |
| PowerShell: running scripts is disabled | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |

More: `docs/TROUBLESHOOTING.md`, `docs/DEPLOYMENT.md`.
