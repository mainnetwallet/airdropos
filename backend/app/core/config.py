"""AirdropOS Core Configuration

Loads and validates environment variables using Pydantic Settings.
Enforces strict single-LLM configuration, spending bounds, and security parameters.
"""

import re
from typing import List, Optional
from pydantic import AnyHttpUrl, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Publicly known development defaults. They are fine on a laptop and fatal in production,
# so ``Settings`` refuses to start in production while any of them is still in use.
_DEV_SECRET_KEY = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
_DEV_VAULT_KEY = _DEV_SECRET_KEY
_DEV_DB_PASSWORD = "airdropos_password"
_VAULT_KEY_RE = re.compile(r"[0-9a-fA-F]{64}")
_PLACEHOLDER_PREFIXES = ("replace-with", "replace_with", "change-me", "changeme", "change_me", "your-")
_PRODUCTION_NAMES = {"production": "production", "prod": "production"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # Environment & Server
    ENVIRONMENT: str = Field(default="development", description="Current environment")
    LOG_LEVEL: str = Field(default="INFO", description="Logging verbosity")
    SECRET_KEY: str = Field(
        default=_DEV_SECRET_KEY,
        description="Application secret key for signing tokens",
    )
    API_V1_PREFIX: str = Field(default="/api/v1")

    # =========================================================================
    # AUTHENTICATION (single-operator VPS deployment)
    # =========================================================================
    AUTH_ENABLED: bool = Field(
        default=True,
        description=(
            "When false, REST endpoints are unauthenticated. NEVER set this to false "
            "while the application is reachable from any network other than localhost."
        ),
    )
    OPERATOR_EMAIL: str = Field(
        default="operator@airdropos.local",
        description="Email for the bootstrap operator account",
    )
    OPERATOR_PASSWORD_HASH: Optional[str] = Field(
        default=None,
        description=(
            "PBKDF2 hash of the operator password, format pbkdf2$rounds$salt$hash. "
            "Generate with: python -c \"from backend.app.security.auth import hash_password; "
            "print(hash_password('your-password-here'))\". Never store the plaintext."
        ),
    )
    SESSION_TTL_HOURS: int = Field(default=24, description="Operator session lifetime")
    TELEGRAM_WEBHOOK_SECRET: Optional[str] = Field(
        default=None,
        description="Shared secret for authenticating Telegram webhook requests",
    )
    DISCORD_BOT_TOKEN: Optional[str] = None
    DISCORD_AUTHORIZED_USER_IDS: List[int] = Field(default=[])
    TWITTER_BEARER_TOKEN: Optional[str] = None
    TWITTER_API_KEY: Optional[str] = None
    TWITTER_API_SECRET: Optional[str] = None
    TWITTER_ACCESS_TOKEN: Optional[str] = None
    TWITTER_ACCESS_SECRET: Optional[str] = None
    TWITTER_AUTHORIZED_USER_IDS: List[int] = Field(default=[])
    BACKEND_HOST: str = Field(default="0.0.0.0")
    BACKEND_PORT: int = Field(default=8000)
    CORS_ORIGINS: List[str] = Field(
        default=["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"]
    )

    # Database & Redis
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://airdropos:airdropos_password@localhost:5432/airdropos_db"
    )
    DB_POOL_SIZE: int = Field(default=20)
    DB_MAX_OVERFLOW: int = Field(default=10)
    REDIS_URL: str = Field(default="redis://localhost:6379/0")

    # =========================================================================
    # SINGLE FIXED LLM ARCHITECTURE (NO MODEL SWITCHING OR ROUTING)
    # =========================================================================
    LLM_BASE_URL: str = Field(default="https://api.openai.com/v1")
    LLM_API_KEY: str = Field(default="sk-mock-placeholder-key")
    LLM_MODEL_ID: str = Field(
        default="gpt-4o",
        description="The SINGLE authoritative model ID used for all AI operations",
    )
    LLM_TEMPERATURE: float = Field(default=0.2)
    LLM_MAX_TOKENS: int = Field(default=4096)
    LLM_TIMEOUT_SECONDS: int = Field(default=60)
    LLM_MAX_RETRIES: int = Field(default=3)

    # =========================================================================
    # DETERMINISTIC SECURITY & AUTONOMOUS SPENDING LIMITS
    # =========================================================================
    EMERGENCY_STOP_ENABLED: bool = Field(
        default=False,
        description="Global kill-switch stopping all execution and transactions",
    )
    EMERGENCY_STOP_STATE_FILE: str = Field(
        default="./data/emergency_stop.json",
        description=(
            "Shared, persistent Emergency Stop state. The API and the worker read the same "
            "file (docker-compose mounts ./data into both), so a stop engaged in one process "
            "is honoured by the other and survives restarts."
        ),
    )
    SPEND_LEDGER_PATH: str = Field(
        default="./data/spend_ledger.db",
        description=(
            "Persistent, cross-process spend ledger used to enforce the rolling 24-hour "
            "spending limit atomically."
        ),
    )
    MAX_TRANSACTION_VALUE_USD: float = Field(
        default=50.0,
        description="Hard spending ceiling for any individual on-chain transaction",
    )
    MAX_DAILY_GAS_SPEND_USD: float = Field(
        default=25.0,
        description="Cumulative 24-hour limit on gas fees",
    )
    MAX_CONCURRENT_TASKS: int = Field(
        default=5,
        description="Maximum parallel task executions",
    )
    MAX_TASK_RETRIES: int = Field(
        default=3,
        description="Maximum automatic retries before marking FAILED or NEEDS_USER_ACTION",
    )
    MAX_EXECUTION_LOOP_ITERATIONS: int = Field(
        default=20,
        description="Prevent runaway Observe-Plan-Act-Verify loops",
    )
    REQUIRE_CONFIRMATION_ABOVE_USD: float = Field(
        default=100.0,
        description="Threshold where user interaction is mandatory",
    )

    # Cryptographic Vault (AES-256-GCM)
    WALLET_VAULT_MASTER_KEY: str = Field(
        default=_DEV_VAULT_KEY,
        description=(
            "Exactly 64 hex characters (a 256-bit key), optionally prefixed with 0x. "
            "Generate with: openssl rand -hex 32"
        ),
    )

    # =========================================================================
    # BLOCKCHAIN RPC PROVIDERS
    # =========================================================================
    RPC_ETHEREUM_MAINNET: Optional[str] = "https://eth.llamarpc.com"
    RPC_ARBITRUM_ONE: Optional[str] = "https://arb1.arbitrum.io/rpc"
    RPC_OPTIMISM: Optional[str] = "https://mainnet.optimism.io"
    RPC_BASE: Optional[str] = "https://mainnet.base.org"
    RPC_POLYGON: Optional[str] = "https://polygon-rpc.com"
    RPC_SEPOLIA: Optional[str] = "https://rpc.sepolia.org"

    # =========================================================================
    # INTEGRATIONS
    # =========================================================================
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    TELEGRAM_AUTHORIZED_USER_IDS: List[int] = Field(default=[])
    TELEGRAM_WEBHOOK_URL: Optional[str] = None

    # Telegram USER client (the agent acts through the operator's own Telegram
    # account: joins channels, starts bots, clicks buttons). Off by default.
    # Create the session once with:  python scripts/telegram_login.py
    TELEGRAM_USER_ENABLED: bool = False
    TELEGRAM_API_ID: Optional[int] = None
    TELEGRAM_API_HASH: Optional[str] = None
    TELEGRAM_SESSION_PATH: str = "./data/telegram/agent"
    TELEGRAM_USER_MIN_ACTION_DELAY_SECONDS: float = 3.0
    TELEGRAM_USER_DAILY_ACTION_LIMIT: int = 60

    DISCORD_BOT_TOKEN: Optional[str] = None
    DISCORD_AUTHORIZED_USER_IDS: List[int] = Field(default=[])
    DISCORD_MONITORED_CHANNELS: List[str] = Field(default=[])

    TWITTER_BEARER_TOKEN: Optional[str] = None
    TWITTER_API_KEY: Optional[str] = None
    TWITTER_API_SECRET: Optional[str] = None
    TWITTER_ACCESS_TOKEN: Optional[str] = None
    TWITTER_ACCESS_SECRET: Optional[str] = None
    TWITTER_AUTHORIZED_USER_IDS: List[int] = Field(default=[])

    # =========================================================================
    # BROWSER AUTOMATION (PLAYWRIGHT)
    # =========================================================================
    BROWSER_HEADLESS: bool = True
    BROWSER_USER_DATA_DIR: str = "./data/browser_sessions"
    BROWSER_NAVIGATION_TIMEOUT_MS: int = 30000
    BROWSER_ACTION_TIMEOUT_MS: int = 15000
    BROWSER_VIEWPORT_WIDTH: int = 1280
    BROWSER_VIEWPORT_HEIGHT: int = 800
    # Loopback, link-local and cloud-metadata targets are always refused. Private LAN ranges
    # (10.x, 172.16-31.x, 192.168.x) are refused too unless this is true.
    BROWSER_ALLOW_PRIVATE_NETWORK: bool = False

    # =========================================================================
    # LIVE BROADCAST (session gateway inside the worker process)
    # =========================================================================
    # The browser and Telegram clients live in the worker, not in the API. The
    # worker therefore serves a small internal HTTP gateway that ONLY the API
    # talks to (shared secret). Browsers never reach it directly.
    LIVE_GATEWAY_ENABLED: bool = True
    LIVE_GATEWAY_HOST: str = "127.0.0.1"
    LIVE_GATEWAY_PORT: int = 8765
    LIVE_GATEWAY_URL: str = "http://127.0.0.1:8765"
    LIVE_GATEWAY_SECRET: Optional[str] = Field(
        default=None,
        description="Shared secret between API and worker gateway. Derived from SECRET_KEY when unset.",
    )
    # Bounded so a typo cannot disable lock expiry or exhaust the host.
    LIVE_CONTROL_LOCK_TTL_SECONDS: int = Field(default=45, ge=10, le=600)
    LIVE_HUMAN_WAIT_TIMEOUT_SECONDS: int = Field(default=1800, ge=60, le=86400)
    LIVE_FRAME_JPEG_QUALITY_FULL: int = Field(default=72, ge=20, le=95)
    LIVE_FRAME_JPEG_QUALITY_THUMB: int = Field(default=40, ge=20, le=95)
    # Each running browser session is a Chromium context in the worker.
    LIVE_MAX_BROWSER_SESSIONS: int = Field(default=6, ge=1, le=50)

    # =========================================================================
    # 24/7 SCHEDULER & RECURRING CYCLES
    # =========================================================================
    SCHEDULER_HEARTBEAT_SECONDS: int = 30
    DISCOVERY_INTERVAL_MINUTES: int = 60
    MONITORING_INTERVAL_MINUTES: int = 15
    CLAIM_CHECK_INTERVAL_MINUTES: int = 30
    DAILY_REPORT_HOUR_UTC: int = 8

    @field_validator("ENVIRONMENT")
    @classmethod
    def normalize_environment(cls, v: str) -> str:
        # Every ``ENVIRONMENT == "production"`` check in the code base is exact, so "Production"
        # or "prod" would silently switch all of them off. Normalise once, here.
        name = (v or "").strip().lower()
        return _PRODUCTION_NAMES.get(name, name or "development")

    @field_validator("WALLET_VAULT_MASTER_KEY")
    @classmethod
    def validate_master_key(cls, v: str) -> str:
        raw = (v or "").strip()
        if raw[:2].lower() == "0x":
            raw = raw[2:]
        # The value itself is never echoed into the error: it is the wallet encryption key.
        if not _VAULT_KEY_RE.fullmatch(raw):
            raise ValueError(
                "WALLET_VAULT_MASTER_KEY must be exactly 64 hexadecimal characters "
                "(a 256-bit key, optionally prefixed with 0x)"
            )
        return v.strip()

    @model_validator(mode="after")
    def reject_insecure_production_config(self) -> "Settings":
        """Production refuses to start on default, placeholder or missing secrets.

        This only ever rejects. It never generates, replaces or rotates a key: an existing vault
        key must stay exactly as the operator set it, or stored wallets become undecryptable.
        """
        if self.ENVIRONMENT != "production":
            return self
        problems: List[str] = []

        secret = (self.SECRET_KEY or "").strip()
        if secret == _DEV_SECRET_KEY or secret.lower().startswith(_PLACEHOLDER_PREFIXES):
            problems.append("SECRET_KEY is the default or a placeholder value")
        elif len(secret) < 32:
            problems.append("SECRET_KEY must be at least 32 characters")

        vault_key = self.WALLET_VAULT_MASTER_KEY.strip()
        vault_key = vault_key[2:] if vault_key[:2].lower() == "0x" else vault_key
        if vault_key.lower() == _DEV_VAULT_KEY:
            problems.append("WALLET_VAULT_MASTER_KEY is the publicly known default")

        if self.AUTH_ENABLED is not True:
            problems.append("AUTH_ENABLED must be true")
        elif not (self.OPERATOR_PASSWORD_HASH or "").strip():
            problems.append("OPERATOR_PASSWORD_HASH is required while AUTH_ENABLED is true")

        if _DEV_DB_PASSWORD in (self.DATABASE_URL or ""):
            problems.append("DATABASE_URL still contains the default database password")

        if problems:
            raise ValueError(
                "Refusing to start with ENVIRONMENT=production: " + "; ".join(problems) + "."
            )
        return self


settings = Settings()
