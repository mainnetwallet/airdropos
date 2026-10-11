"""AirdropOS Core Configuration

Loads and validates environment variables using Pydantic Settings.
Enforces strict single-LLM configuration, spending bounds, and security parameters.
"""

from typing import List, Optional
from pydantic import AnyHttpUrl, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
        default="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
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
        default="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        description="64-character hex string representing a 256-bit key",
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

    # =========================================================================
    # 24/7 SCHEDULER & RECURRING CYCLES
    # =========================================================================
    SCHEDULER_HEARTBEAT_SECONDS: int = 30
    DISCOVERY_INTERVAL_MINUTES: int = 60
    MONITORING_INTERVAL_MINUTES: int = 15
    CLAIM_CHECK_INTERVAL_MINUTES: int = 30
    DAILY_REPORT_HOUR_UTC: int = 8

    @field_validator("WALLET_VAULT_MASTER_KEY")
    @classmethod
    def validate_master_key(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError("WALLET_VAULT_MASTER_KEY must be at least 32 characters long")
        return v


settings = Settings()
