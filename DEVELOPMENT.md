# Running AirdropOS

Pick your system and follow that section from top to bottom:

1. [Windows PowerShell](#1-windows-powershell) (full guide)
2. [Windows CMD](#2-windows-cmd) (full guide)
3. [Ubuntu](#3-ubuntu-pc--laptop--home-server) (full guide)
4. [VPS](#4-vps-ubuntu-2204--2404-docker) (full guide, Docker)
5. [Reference: `.env` values, `run.py` options, problems](#5-reference)

Every guide has the same order:

| Step | When |
|---|---|
| **Install tools** | once per computer |
| **First time: clone** | once, when the folder does not exist yet |
| **Create and edit `.env`** | once |
| **Set password, start server** | first start |
| **Next time: `cd airdropos` → `git pull origin main` → start server** | every later day |

Repository: `https://github.com/mainnetwallet/airdropos.git`

> **Private repository?** On `git clone` / `git pull` Git asks you to sign in to GitHub
> (a browser window opens, or it asks for username + a Personal Access Token as the password).
> Never paste a token into a command, a URL, a file in this repo, or a chat. If a token was ever shared, revoke it on GitHub
> (Settings → Developer settings → Personal access tokens) and create a new one.

---

## 1. Windows PowerShell

Open **PowerShell** (Start menu → type `PowerShell`). Normal user is fine, no Administrator needed.

### 1.1 Install tools (once)

```powershell
winget install -e --id Git.Git
winget install -e --id Python.Python.3.11
winget install -e --id OpenJS.NodeJS.LTS
```

**Close PowerShell and open a new one**, then check:

```powershell
git --version
py --version        # 3.10 or newer
node --version      # 20 or newer
```

Optional: **Docker Desktop** (`winget install -e --id Docker.DockerDesktop`, then restart the PC and start Docker Desktop).
You only need it if you want PostgreSQL. Without Docker use `--sqlite` (below), which is the easiest choice for one PC.

If PowerShell says "running scripts is disabled":

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

### 1.2 First time: clone

Choose a folder (example: your user folder) and clone:

```powershell
cd $HOME
git clone https://github.com/mainnetwallet/airdropos.git
cd airdropos
```

### 1.3 Create `.env`

**Easy way (recommended).** This creates `.env` for you, with fresh random `SECRET_KEY` and `WALLET_VAULT_MASTER_KEY`,
installs Python packages, and asks for your dashboard password (at least 4 characters):

```powershell
py run.py --set-password
```

**Manual way (copy the example yourself).** Use this only if you prefer to fill everything by hand:

```powershell
Copy-Item .env.example .env
py -c "import secrets; print(secrets.token_hex(32))"     # run twice: one for SECRET_KEY, one for WALLET_VAULT_MASTER_KEY
```

Then do the edit below, and set the password with `py run.py --set-password`.

### 1.4 Edit `.env` with Notepad

```powershell
notepad .env
```

Set at least `LLM_API_KEY` (and `LLM_BASE_URL`, `LLM_MODEL_ID` if you do not use OpenAI). Save with Ctrl+S, close Notepad.
All variables are explained in [5.1](#51-env-values). If you used the manual way, also remove the `#` in front of
`SECRET_KEY=` and `WALLET_VAULT_MASTER_KEY=` and paste the two random values.

> **Back up `.env`** (copy it somewhere safe). If `WALLET_VAULT_MASTER_KEY` is lost, stored wallet keys can never be decrypted.
> Never change it after wallets were saved.

### 1.5 Start the server (first time)

Without Docker (SQLite, saved in `.env`, so you pass `--sqlite` only once):

```powershell
py run.py --sqlite
```

With Docker Desktop running (PostgreSQL):

```powershell
py run.py
```

The first start takes several minutes (Python packages, Chromium for Playwright, npm packages). When you see
`Dashboard : http://localhost:5173` it is ready. Windows Firewall may ask for permission: click **Allow**.

### 1.6 First login (once)

Leave the server window running, open a **second** PowerShell window:

```powershell
Invoke-RestMethod -Method Post http://localhost:8000/api/v1/auth/bootstrap
```

Open http://localhost:5173 and log in with `OPERATOR_EMAIL` (default `operator@airdropos.local`) and the password from step 1.3.
If it says the account already exists, just log in. API docs: http://localhost:8000/docs

### 1.7 Next time (every later day)

```powershell
cd $HOME\airdropos
git pull origin main
py run.py
```

If the update changed frontend or Python packages and something fails to start, use this instead, which also refreshes
pip and npm packages:

```powershell
py run.py --update
```

`.env` and your data are never touched by an update. If `git pull` refuses because you edited a tracked file, run `git stash` and pull again.

**Stop:** press **Ctrl+C** once in the server window (stops backend, worker and dashboard).

### 1.8 Extras

| Task | Command |
|---|---|
| Telegram user login (one time) | `.\.venv\Scripts\python.exe scripts\telegram_login.py`, then set `TELEGRAM_USER_ENABLED=true` in `.env` |
| Change dashboard password | `py run.py --set-password` |
| Check for missing DB columns (SQLite; startup already fixes them) | stop the server, then `.\.venv\Scripts\python.exe scripts\sqlite_add_missing_columns.py` (dry run), then add `--apply` |
| Run without the dashboard | `py run.py --no-frontend` |
| Back up | copy `.env`, the `data` folder, and `airdropos.db` (if SQLite) |

---

## 2. Windows CMD

Open **Command Prompt** (Start menu → type `cmd`).

### 2.1 Install tools (once)

```cmd
winget install -e --id Git.Git
winget install -e --id Python.Python.3.11
winget install -e --id OpenJS.NodeJS.LTS
```

**Close CMD and open a new one**, then check:

```cmd
git --version
py --version
node --version
```

Optional: Docker Desktop for PostgreSQL (`winget install -e --id Docker.DockerDesktop`). Without it use `--sqlite`.

### 2.2 First time: clone

```cmd
cd %USERPROFILE%
git clone https://github.com/mainnetwallet/airdropos.git
cd airdropos
```

### 2.3 Create `.env`

**Easy way (recommended)** creates `.env` with random keys, installs packages and asks for your password (at least 4 characters):

```cmd
py run.py --set-password
```

**Manual way:**

```cmd
copy .env.example .env
py -c "import secrets; print(secrets.token_hex(32))"
```

(run the last line twice: one value for `SECRET_KEY`, one for `WALLET_VAULT_MASTER_KEY`), then edit `.env` and run `py run.py --set-password`.

### 2.4 Edit `.env` with Notepad

```cmd
notepad .env
```

Set at least `LLM_API_KEY` (plus `LLM_BASE_URL` and `LLM_MODEL_ID` if not OpenAI). Save with Ctrl+S.
All variables: [5.1](#51-env-values). For the manual way, remove the `#` before `SECRET_KEY=` and
`WALLET_VAULT_MASTER_KEY=` and paste the random values. **Back up `.env`.**

### 2.5 Start the server (first time)

No Docker (SQLite):

```cmd
py run.py --sqlite
```

With Docker Desktop running (PostgreSQL):

```cmd
py run.py
```

Wait for `Dashboard : http://localhost:5173`. Click **Allow** if Windows Firewall asks.

### 2.6 First login (once)

In a **second** CMD window:

```cmd
curl.exe -X POST http://localhost:8000/api/v1/auth/bootstrap
```

Open http://localhost:5173, log in with `operator@airdropos.local` (or your `OPERATOR_EMAIL`) and your password.

### 2.7 Next time (every later day)

```cmd
cd %USERPROFILE%\airdropos
git pull origin main
py run.py
```

Or, to also refresh pip/npm packages: `py run.py --update`.
If `git pull` refuses because of local changes: `git stash`, then pull again.

**Stop:** **Ctrl+C** once.

### 2.8 Extras

| Task | Command |
|---|---|
| Telegram user login (one time) | `.venv\Scripts\python.exe scripts\telegram_login.py` |
| Change dashboard password | `py run.py --set-password` |
| Check for missing DB columns (SQLite; startup already fixes them) | stop the server, then `.venv\Scripts\python.exe scripts\sqlite_add_missing_columns.py` (dry run), then add `--apply` |
| Backup | copy `.env`, the `data` folder, and `airdropos.db` (if SQLite) |

---

## 3. Ubuntu (PC / laptop / home server)

For a public VPS use [section 4](#4-vps-ubuntu-2204--2404-docker) (Docker). This section runs `python3 run.py` (development server).

### 3.1 Install tools (once)

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git curl python3 python3-venv python3-pip nano
```

Node.js **20 or newer** (the Ubuntu default `nodejs` package is too old):

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
node --version
```

Check: `git --version`, `python3 --version` (3.10+).

PostgreSQL through Docker is optional. Without Docker use `--sqlite`. To install Docker:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker
```

### 3.2 First time: clone

```bash
cd ~
git clone https://github.com/mainnetwallet/airdropos.git
cd airdropos
```

### 3.3 Create `.env`

**Easy way (recommended)** creates `.env` with random keys, installs packages, asks for your password (at least 4 characters):

```bash
python3 run.py --set-password
```

**Manual way:**

```bash
cp .env.example .env
openssl rand -hex 32        # run twice: SECRET_KEY and WALLET_VAULT_MASTER_KEY
```

Then edit `.env` and run `python3 run.py --set-password`.

### 3.4 Edit `.env` with nano

```bash
nano .env
```

Set at least `LLM_API_KEY` (plus `LLM_BASE_URL`, `LLM_MODEL_ID` if not OpenAI).
Save: **Ctrl+O**, Enter. Exit: **Ctrl+X**. Search inside nano: **Ctrl+W**. All variables: [5.1](#51-env-values).
For the manual way, remove the `#` before `SECRET_KEY=` and `WALLET_VAULT_MASTER_KEY=` and paste the random values. **Back up `.env`.**

### 3.5 Browser tools need system libraries (once)

`run.py` downloads Chromium, but Linux also needs its system libraries:

```bash
sudo .venv/bin/python -m playwright install-deps chromium
```

### 3.6 Start the server (first time)

No Docker (SQLite):

```bash
python3 run.py --sqlite
```

With Docker (PostgreSQL):

```bash
python3 run.py
```

### 3.7 First login (once)

In a second terminal:

```bash
curl -X POST http://localhost:8000/api/v1/auth/bootstrap
```

Open http://localhost:5173 and log in with `operator@airdropos.local` (or your `OPERATOR_EMAIL`) and your password.

### 3.8 Next time (every later day)

```bash
cd ~/airdropos
git pull origin main
python3 run.py
```

Or, to also refresh pip/npm packages: `python3 run.py --update`.
If `git pull` refuses because of local changes: `git stash`, then pull again.

**Stop:** **Ctrl+C** once.

### 3.9 Keep it running after you close the terminal (tmux)

```bash
sudo apt install -y tmux
tmux new -s airdrop
python3 run.py                  # inside tmux
# detach: press Ctrl+B, then D
tmux attach -t airdrop          # come back later
```

On a remote Ubuntu machine, open the dashboard from your own PC through an SSH tunnel (nothing is exposed to the internet):

```bash
ssh -L 5173:localhost:5173 -L 8000:localhost:8000 USER@SERVER_IP
```

Then open http://localhost:5173 on your PC.

### 3.10 Extras

| Task | Command |
|---|---|
| Telegram user login (one time) | `.venv/bin/python scripts/telegram_login.py` |
| Change dashboard password | `python3 run.py --set-password` |
| Check for missing DB columns (SQLite; startup already fixes them) | stop the server, then `.venv/bin/python scripts/sqlite_add_missing_columns.py` (dry run), then add `--apply` |
| Backup | copy `.env`, the `data/` folder, and `airdropos.db` (if SQLite) |

### 3.11 Phone (Termux + proot-distro Ubuntu)

Start (every time):

```bash
proot-distro login ubuntu
cd ~/airdropos
git pull origin main
python3 run.py
```

Open http://localhost:5173 in Chrome on the same phone.

- `run.py` starts the dashboard with `npm run dev:mobile` (Vite with its WebSocket off). Without this, Chrome drops the socket
  when you minimize it and Vite **reloads the page** when you come back. `server.hmr: false` alone is not enough; it needs `server.ws: false`.
- Trade-off: in this mode a code change needs a manual refresh in Chrome. For live updates on a PC use `python3 run.py --hmr`.
- In development mode the page shows an orange badge for 10 seconds after a reload, saying whether Chrome discarded the tab (low memory) or something else reloaded it.

**Faster dashboard (production build).** The Vite dev server loads hundreds of small files, which is slow on a phone.
A production build is one small bundle. Use two proot sessions (or `tmux`):

```bash
# session 1: backend + worker only
cd ~/airdropos
python3 run.py --no-frontend

# session 2: build once, then serve the built files (same port 5173, same /api proxy)
cd ~/airdropos/frontend
npm run build
npm run preview -- --host --port 5173 --strictPort
```

Open http://localhost:5173 as usual. After every `git pull` that changes `frontend/`, run `npm run build` again and restart the preview.
Keep the port at 5173: the backend only allows that origin by default (`CORS_ORIGINS` in `backend/app/core/config.py`).

---

## 4. VPS (Ubuntu 22.04 / 24.04, Docker)

Use Docker on a VPS. It starts PostgreSQL, Redis, backend, worker and dashboard, and restarts them after a reboot.
Recommended size: 2 vCPU, 4 GB RAM or more (Chromium is heavy).

### 4.1 Log in and prepare the server (once)

From your PC (Windows PowerShell / CMD and Linux/macOS all have `ssh`):

```bash
ssh root@YOUR_VPS_IP
```

```bash
apt update && apt upgrade -y
apt install -y git curl ufw nano
ufw allow OpenSSH
ufw allow 3000/tcp
ufw enable
```

Docker:

```bash
curl -fsSL https://get.docker.com | sh
```

(Using a non-root user instead? Run `sudo` in front, then `sudo usermod -aG docker $USER && newgrp docker`.)

### 4.2 First time: clone

```bash
cd ~
git clone https://github.com/mainnetwallet/airdropos.git
cd airdropos
```

### 4.3 Create `.env`

```bash
cp .env.example .env
```

Generate the random values (each command prints one; copy them):

```bash
openssl rand -hex 32      # -> SECRET_KEY
openssl rand -hex 32      # -> WALLET_VAULT_MASTER_KEY (exactly 64 hex chars)
openssl rand -hex 24      # -> POSTGRES_PASSWORD
```

### 4.4 Edit `.env` with nano

```bash
nano .env
```

| Variable | Set to |
|---|---|
| `ENVIRONMENT` | `production` |
| `SECRET_KEY` | remove `#`, paste the first random value |
| `WALLET_VAULT_MASTER_KEY` | remove `#`, paste the second random value. **Back it up. Never change it later.** |
| `POSTGRES_PASSWORD` | remove `#`, paste the third random value |
| `LLM_API_KEY` | your LLM key (plus `LLM_BASE_URL`, `LLM_MODEL_ID` if not OpenAI) |
| `OPERATOR_PASSWORD_HASH` | the hash from step 4.5, **in single quotes** |
| `AUTH_ENABLED` | keep `true` |

Leave `DATABASE_URL` and `REDIS_URL` as they are, Docker overrides them. Save: Ctrl+O, Enter, Ctrl+X.

### 4.5 Make the password hash

```bash
docker compose run --rm backend python -c "from backend.app.security.auth import hash_password; print(hash_password('YOUR-PASSWORD'))"
```

Put the printed line into `.env`, with single quotes (it contains `$`):

```
OPERATOR_PASSWORD_HASH='pbkdf2$390000$....$....'
```

(`POSTGRES_PASSWORD` must already be set, or `docker compose` refuses to start.) Use a strong password on a VPS.

### 4.6 Start the server (first time)

```bash
docker compose up -d --build
docker compose exec backend python -m backend.app.services.init_db
curl -X POST http://localhost:8000/api/v1/auth/bootstrap
```

Open `http://YOUR_VPS_IP:3000` and log in with `OPERATOR_EMAIL` (default `operator@airdropos.local`) and your password.

### 4.7 Next time (every later day)

```bash
cd ~/airdropos
git pull origin main
docker compose up -d --build
docker compose exec backend python -m backend.app.services.init_db
```

If `git pull` refuses because of local changes: `git stash`, then pull again. `.env` is not tracked, so it is safe.

### 4.8 Daily commands

```bash
docker compose ps                        # status
docker compose logs -f backend worker    # live logs (Ctrl+C leaves the logs, not the app)
docker compose restart                   # restart everything
docker compose down                      # stop (data is kept)
```

### 4.9 Security (do this before real use)

- Postgres (5432), Redis (6379) and the API (8000) are bound to `127.0.0.1`, so they are not reachable from the internet.
  Docker bypasses `ufw`, so do not remove those `127.0.0.1:` bindings.
- Port 3000 is plain **HTTP**, so your login travels unencrypted. Choose one:
  - **SSH tunnel** (simplest): `ssh -L 3000:localhost:3000 root@YOUR_VPS_IP`, then open http://localhost:3000.
    To make this the only way in, set `FRONTEND_BIND=127.0.0.1` in `.env`, close port 3000 with `ufw delete allow 3000/tcp`, and run `docker compose up -d`.
  - **HTTPS** with Caddy or nginx + Let's Encrypt in front. See `docs/DEPLOYMENT.md`.
- Use SSH keys and disable password SSH login if you can.
- Back up `.env` and the Postgres volume. See `docs/DATABASE_BACKUP.md`.
- In production the app refuses to start on a default or placeholder `SECRET_KEY` or `WALLET_VAULT_MASTER_KEY`, with `AUTH_ENABLED=false`,
  or without `OPERATOR_PASSWORD_HASH`. It never creates or replaces your vault key.

### 4.10 Upgrading an older Docker install

`docker-compose.yml` has no database password inside. Add `POSTGRES_PASSWORD=` to `.env`. Postgres only reads it when its data volume is
first created, so for an existing volume set it to the password that database already has (older versions used the one in `DATABASE_URL`).
To rotate it, run `ALTER USER airdropos PASSWORD '...'` inside Postgres, then update `.env`.

### 4.11 Telegram user login on the VPS (optional)

```bash
docker compose exec -it worker python scripts/telegram_login.py
```

If that path is not found in the container, run the script on a PC and copy `data/telegram/` to the VPS. Then set `TELEGRAM_USER_ENABLED=true` in `.env` and `docker compose up -d`.
Details: `docs/TELEGRAM_SETUP.md`.

---

## 5. Reference

### 5.1 `.env` values

| Variable | What to do |
|---|---|
| `SECRET_KEY` | Random string, 32+ chars. `run.py` creates it for you. Required in production. |
| `WALLET_VAULT_MASTER_KEY` | Exactly 64 hex chars. `run.py` creates it for you. **Back it up. Never change it once wallets exist.** |
| `POSTGRES_PASSWORD` | Docker/PostgreSQL only. Random value (`openssl rand -hex 24`). |
| `DATABASE_URL` | PostgreSQL URL, or `sqlite+aiosqlite:///./airdropos.db` (set automatically by `--sqlite`). |
| `OPERATOR_EMAIL` | Login email. Default `operator@airdropos.local`. |
| `OPERATOR_PASSWORD_HASH` | Created by `run.py --set-password` (Docker: step 4.5). Keep the single quotes. |
| `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL_ID` | Your LLM provider. `LLM_BASE_URL` must include the version path, e.g. `https://api.openai.com/v1`. |
| `RPC_*` | Blockchain RPC URLs (Alchemy etc.). Replace `your-api-key` for the chains you use. |
| `MAX_TRANSACTION_VALUE_USD`, `MAX_DAILY_GAS_SPEND_USD`, `REQUIRE_CONFIRMATION_ABOVE_USD` | Spending limits. Review them before enabling real transactions. |
| `ALLOW_VALUE_MOVING` | `true` lets the agent sign and broadcast at all. **Default `false`.** Even when on, every broadcast still needs an approval bound to that one exact transfer, from you or from the autonomous policy below. |
| `AUTONOMOUS_POLICY_FILE` | Path to a JSON policy deciding what the agent may do without asking. Unset means **everything is refused** — the deny-by-default posture. See `docs/AUTONOMOUS_EXECUTION.md`. |
| `AUTONOMOUS_POLICY` | Inline JSON, if you would rather not use a file. A file is preferred on Windows. |
| `TOKEN_CONTRACTS` | Optional. Lets you send a token that is not built in, e.g. `{"ethereum": {"MYTOKEN": "0x..."}}`. A symbol is never looked up by searching: a token that is neither built in nor listed here is refused rather than guessed. |
| `APPROVAL_TTL_MINUTES` | How long a quoted transfer stays approvable. Default 15. |
| `TELEGRAM_USER_DAILY_ACTION_LIMIT` | Most actions (join, send, click) the Telegram user client may take per day. Default `1000`. It acts through your own Telegram account, so a high value raises the risk of Telegram limiting it; keep `TELEGRAM_USER_MIN_ACTION_DELAY_SECONDS` (default 3) so actions are spaced out. A value in `.env` overrides the default. |
| `TELEGRAM_*`, `DISCORD_*`, `TWITTER_*` | Optional integrations (see `docs/TELEGRAM_SETUP.md`, `docs/DISCORD_SETUP.md`, `docs/X_INTEGRATION.md`). |
| `BROWSER_HEADLESS` | `true` on servers. |
| `ENVIRONMENT` | `development` locally, `production` on a VPS. |
| `FRONTEND_BIND` | Docker only. `127.0.0.1` keeps the dashboard private (use with SSH tunnel or a TLS proxy). |
| `CORS_ORIGINS` | Allowed dashboard addresses. Add yours if you open the dashboard from another address and the browser shows a CORS error. |

After editing `.env`, stop the server (Ctrl+C) and start it again. On Docker: `docker compose up -d`.

### 5.2 `run.py` options

Windows: `py run.py ...`. Ubuntu: `python3 run.py ...`.

| Option | Meaning |
|---|---|
| `--set-password` | Create `.env` if missing, set the login password, then exit (add `--hide-password` to hide typing) |
| `--update` | `git pull --ff-only`, refresh pip/npm packages, then start |
| `--sqlite` | Use a local SQLite file instead of PostgreSQL (no Docker). Saved in `.env`. Fine for one person on one PC. |
| `--no-worker` | Do not start the background worker |
| `--no-frontend` | Backend + worker only (no Node.js needed) |
| `--no-dashboard` | Stream service logs to the terminal instead of the live terminal dashboard |
| `--no-infra` | Do not check/start PostgreSQL (use your own) |
| `--skip-install` | Skip pip install |
| `--reload` | Auto-reload backend on code changes (development) |
| `--host 0.0.0.0` | Bind the backend to all interfaces (default `127.0.0.1`) |

Addresses: Dashboard http://localhost:5173 (Docker: 3000), API docs http://localhost:8000/docs.
If one process crashes, the others are stopped and the reason is shown.

### 5.2a The terminal dashboard

`py run.py` opens one live terminal dashboard instead of a wall of log lines: system
status, agent configuration, task counts, scheduler jobs, the tool registry, recent
activity and current warnings. It is redrawn in place, so nothing scrolls away.

- It only reads. Every value comes from a source the platform already keeps: the API's
  own health endpoint, a socket connect for the frontend and the live gateway, the
  worker's heartbeat row, the task and schedule tables, the shared emergency-stop state
  file, and a structured event stream every process appends to. A service that is down
  or unreachable is shown as down; nothing is reported healthy because the dashboard is
  running.
- Full diagnostics keep going to `logs/airdropos.log` (rotating) and
  `logs/airdropos-events.jsonl` (structured, one run per file). Nothing is filtered out
  of them; every message is redacted before it is written.
- `--no-dashboard` prints the same logs directly, which is also what happens
  automatically when stdout is not an interactive terminal. Redirecting stdout
  (`py run.py > session.txt`) captures the run that way.
- Layout: the full three-column layout needs about 140x40. Below that it rearranges into
  two columns, and the activity feed is the only panel that shrinks. The logo is drawn
  from the same monochrome glyphs as the rest of the UI because a terminal cannot scale
  a glyph; nothing else is approximated.

### 5.3 Run the tests

```bash
pip install -r backend/requirements.txt
python -m pytest backend/tests/ -q
```

### 5.4 Common problems

| Problem | Fix |
|---|---|
| `git`, `py`, `node` "not recognized" (Windows) | Close the terminal and open a new one after installing. Still missing: reinstall, and tick "Add to PATH". |
| `Node.js/npm not found` or Node older than 20 | Install Node.js 20+ from https://nodejs.org (Ubuntu: step 3.1), or use `--no-frontend`. |
| `Docker could not start PostgreSQL` / `Cannot reach the database` | Start Docker Desktop and wait for "Engine running", or use `--sqlite`. |
| Cannot log in / bootstrap returns 503 | Run `run.py --set-password` (Docker: set `OPERATOR_PASSWORD_HASH` in `.env`). |
| Task stuck in `RETRYING`/`FAILED`, worker log says `no such column: ...` | Your SQLite file was made by an older version. `py run.py` now adds missing columns automatically at startup (it backs up `airdropos.db` first and only adds columns), so `git pull` then `py run.py` is enough. Then create the task again or press Retry (a task that already hit its retry limit is `FAILED`). PostgreSQL: `alembic upgrade head`. To only check, run `python scripts/sqlite_add_missing_columns.py`. |
| Chat says `Blocked: The configured model is unavailable (EMPTY_CONTENT)` | The model spent its whole reply on reasoning and returned no answer (common with routers and reasoning models). The app now resends up to `LLM_EMPTY_CONTENT_MAX_RETRIES` times (default 2). If it still happens, send the message again, shorten it, or set `LLM_MAX_TOKENS` (e.g. `8000`) so the reply has room, or pick a non-reasoning model on your router. |
| Agent Inbox keeps answering `still blocked ... NOT_CHECKED` | Inbox replies are rule-based, not an AI chat (the LLM only runs on the worker). When the blocker cannot be checked automatically (no live session), reply with the actual information, e.g. the wallet address `0x...`, a link, or a full sentence saying what you did. It is saved as your information, the task is requeued, and the worker's planner revises the plan with it. One-word replies such as `98`, `re-check` or `done` are not treated as information. Better still: put the address in the task text when you create it. |
| `ModuleNotFoundError: backend` | Run from the repository root (the folder with `run.py`). |
| `WALLET_VAULT_MASTER_KEY must be exactly 64 hex characters` | Use `openssl rand -hex 32` or `py -c "import secrets; print(secrets.token_hex(32))"`. |
| Port 8000 / 5173 / 3000 already in use | Stop the other program, or change the port. |
| PowerShell: running scripts is disabled | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| `git pull` says local changes would be overwritten | `git stash`, then `git pull origin main`. |
| `git pull` asks for a password | Private repo: use your GitHub username and a Personal Access Token as the password. |
| Playwright / Chromium fails on Ubuntu | `sudo .venv/bin/python -m playwright install-deps chromium` |
| `docker compose` says `POSTGRES_PASSWORD` is not set | Add `POSTGRES_PASSWORD=...` to `.env`. |
| `permission denied` on Docker (Ubuntu) | `sudo usermod -aG docker $USER && newgrp docker` |

More: `docs/TROUBLESHOOTING.md`, `docs/DEPLOYMENT.md`.
