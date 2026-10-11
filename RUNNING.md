# Running AirdropOS

Choose one method:

- **Method A: Docker** (recommended). Works the same on Windows, Ubuntu and a VPS.
- **Method B: Manual** (no Docker for the app). Windows CMD, Windows PowerShell, Ubuntu.

Dashboard: http://localhost:3000 (Docker) or http://localhost:5173 (manual `npm run dev`)
API docs: http://localhost:8000/docs

---

## 0. Before you start (all systems)

You need `git`. Then:

| Method | Needs |
|---|---|
| A: Docker | Docker with Compose v2 (Docker Desktop on Windows) |
| B: Manual | Python 3.11+, Node.js 20+, Docker (only for PostgreSQL + Redis) |

Clone:

```
git clone https://github.com/mainnetwallet/airdropos.git
cd airdropos
```

---

## 1. Create `.env`

**Windows CMD**
```cmd
copy .env.example .env
notepad .env
```

**Windows PowerShell**
```powershell
Copy-Item .env.example .env
notepad .env
```

**Ubuntu / VPS**
```bash
cp .env.example .env
nano .env
```

Change these values in `.env`:

| Variable | What to do |
|---|---|
| `SECRET_KEY` | Random string, 32+ chars |
| `WALLET_VAULT_MASTER_KEY` | Exactly 64 hex characters. **Back it up. If you lose it, stored wallet keys cannot be decrypted.** |
| `LLM_API_KEY` (and `LLM_BASE_URL`, `LLM_MODEL_ID`) | Your LLM provider key |
| `OPERATOR_PASSWORD_HASH` | Hash of your login password (see below). Keep the single quotes. |

Generate random values:

```bash
# Ubuntu / VPS / Git Bash
openssl rand -hex 32
```
```powershell
# Windows PowerShell
-join ((1..32) | ForEach-Object { '{0:x2}' -f (Get-Random -Maximum 256) })
```

Generate the password hash (run after step 2 Docker start, or after `pip install` in Method B):

```bash
# Docker
docker compose run --rm backend python -c "from backend.app.security.auth import hash_password; print(hash_password('YOUR-PASSWORD'))"

# Manual (venv active)
python -c "from backend.app.security.auth import hash_password; print(hash_password('YOUR-PASSWORD'))"
```

Put it in `.env` inside single quotes:

```
OPERATOR_PASSWORD_HASH='pbkdf2$390000$....$....'
```

Leave `DATABASE_URL` and `REDIS_URL` as they are. In Docker mode they are overridden automatically to the container hostnames.

---

## Method A: Docker

Same commands on CMD, PowerShell and Ubuntu.

```bash
docker compose up -d --build
docker compose exec backend python -m backend.app.services.init_db
docker compose ps
docker compose logs -f backend
```

Open http://localhost:3000.

Stop: `docker compose down` (data is kept). Update: `git pull` then `docker compose up -d --build`.

---

## Method B: Manual

PostgreSQL and Redis are still needed. Start only those two with Docker:

```bash
docker compose up -d postgres redis
```

### Windows CMD

```cmd
py -3.11 -m venv .venv
.venv\Scripts\activate.bat
pip install -r backend\requirements.txt
playwright install chromium
python -m backend.app.services.init_db
uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

New CMD window, from the repo folder (worker):
```cmd
.venv\Scripts\activate.bat
python -m backend.app.scheduler.worker
```

New CMD window (dashboard):
```cmd
cd frontend
npm install
npm run dev
```

### Windows PowerShell

If activation is blocked, run once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
playwright install chromium
python -m backend.app.services.init_db
uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

New PowerShell window (worker):
```powershell
.\.venv\Scripts\Activate.ps1
python -m backend.app.scheduler.worker
```

New PowerShell window (dashboard):
```powershell
cd frontend
npm install
npm run dev
```

### Ubuntu

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv git curl   # on 24.04 use python3 python3-venv
./scripts/start_backend.sh     # creates .venv, installs, runs init_db, starts API
```

Second terminal (worker):
```bash
./scripts/start_worker.sh
```

Third terminal (dashboard):
```bash
cd frontend && npm install && npm run dev
```

If `python3.11` does not exist on your Ubuntu, edit the `python3.11 -m venv` line in `scripts/start_backend.sh` to `python3 -m venv`.

Dashboard in dev mode: http://localhost:5173

---

## First login

The operator account is created once from `OPERATOR_EMAIL` and `OPERATOR_PASSWORD_HASH`.

**Ubuntu / VPS / Git Bash / Windows CMD**
```bash
curl -X POST http://localhost:8000/api/v1/auth/bootstrap
```
(On CMD use `curl.exe`.)

**Windows PowerShell**
```powershell
Invoke-RestMethod -Method Post http://localhost:8000/api/v1/auth/bootstrap
```

Then log in to the dashboard with `OPERATOR_EMAIL` and the plain password you hashed. If bootstrap says an account already exists, just log in.

---

## VPS (Ubuntu 22.04 / 24.04)

```bash
# 1. Update and firewall
sudo apt update && sudo apt upgrade -y
sudo apt install -y git curl ufw
sudo ufw allow OpenSSH
sudo ufw allow 3000/tcp
sudo ufw enable

# 2. Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker

# 3. Code and config
git clone https://github.com/mainnetwallet/airdropos.git
cd airdropos
cp .env.example .env
nano .env        # set SECRET_KEY, WALLET_VAULT_MASTER_KEY, LLM_API_KEY, OPERATOR_PASSWORD_HASH
                 # keep AUTH_ENABLED=true, set ENVIRONMENT=production

# 4. Start
docker compose up -d --build
docker compose exec backend python -m backend.app.services.init_db
curl -X POST http://localhost:8000/api/v1/auth/bootstrap
```

Open `http://YOUR_VPS_IP:3000`.

Notes for a VPS:

- Postgres (5432), Redis (6379) and the API (8000) are bound to `127.0.0.1` in `docker-compose.yml`, so they are not reachable from the internet. Only the dashboard on port 3000 is.
- Docker publishes ports around `ufw`, so do not change those `127.0.0.1:` bindings to open ports.
- Services have `restart: unless-stopped`, so they come back after a reboot.
- Plain HTTP on port 3000 sends your login over the network unencrypted. Put a domain with HTTPS (Caddy or nginx + Let's Encrypt) in front before real use, or reach it through an SSH tunnel: `ssh -L 3000:localhost:3000 user@YOUR_VPS_IP`, then open http://localhost:3000.
- Back up `.env` (especially `WALLET_VAULT_MASTER_KEY`) and the Postgres volume. See `docs/DATABASE_BACKUP.md`.

---

## Run the tests

```bash
pip install -r backend/requirements.txt
python -m pytest backend/tests/ -q
```

## Common problems

| Problem | Fix |
|---|---|
| `ModuleNotFoundError: backend` | Run commands from the repo root, not from inside `backend/` |
| `WALLET_VAULT_MASTER_KEY must be exactly 64 hex characters` | Generate with `openssl rand -hex 32` |
| Bootstrap returns 503 | `OPERATOR_PASSWORD_HASH` is empty in `.env` |
| Backend cannot reach database (Manual) | `docker compose up -d postgres redis`, then check `docker compose ps` |
| Port already in use | Stop the other program or change the port in `docker-compose.yml` |

More: `docs/TROUBLESHOOTING.md`, `docs/DEPLOYMENT.md`.
