"""AirdropOS Core Configuration

Loads and validates environment variables using Pydantic Settings.
Enforces strict single-LLM configuration, spending bounds, and security parameters.
"""

import re
from typing import Any, Dict, List, Optional
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
    # One line per HTTP request. Off by default: the dashboard polls several endpoints
    # every few seconds, so the access log buried warnings and errors in the terminal.
    # This only ever removed the per-request line. Warnings, errors and tracebacks are
    # unaffected, and security-relevant history does not depend on it: every login,
    # revocation and emergency-stop change is written to the `audit_logs` table by
    # `security.auth.write_audit` and read back through `GET /api/v1/audit`.
    ACCESS_LOG: bool = Field(
        default=False,
        description="Print one access-log line per HTTP request. Off by default.",
    )
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
    TRUSTED_PROXIES: str = Field(
        default="",
        description=(
            "Comma-separated IPs/CIDRs of reverse proxies allowed to set X-Forwarded-For (e.g. "
            "the frontend nginx container's network). Empty (default) trusts no forwarded header, "
            "so rate limits are keyed on the connecting address. Never list a range that clients "
            "can connect from directly."
        ),
    )
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
    LLM_MAX_TOKENS: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "Optional OUTPUT-token limit. Unset (the default) means AirdropOS imposes no "
            "limit and omits max_tokens from every request, leaving the provider's own "
            "limit in force. Set it only if your provider requires one."
        ),
    )
    LLM_TIMEOUT_SECONDS: int = Field(default=60)
    LLM_MAX_RETRIES: int = Field(default=3)

    # ------------------------------------------------------------------
    # INPUT CONTEXT BUDGET (never an output limit)
    # ------------------------------------------------------------------
    # These govern INPUT size only. They have nothing to do with LLM_MAX_TOKENS
    # above, which governs OUTPUT. They do not cut content: a request that fits
    # the budget is sent byte-for-byte as built. They exist so that a request
    # which cannot possibly fit is reduced *before* submission instead of
    # failing at the provider.
    #
    # The default below is deliberately CONSERVATIVE and is NOT the 300k figure
    # that might be assumed from the configured model id. Measured against the
    # configured endpoint:
    #   * a 160,017-token prompt was accepted (HTTP 200, usage reported);
    #   * the configured id is not in the provider's model catalog, and the
    #     provider re-routes per request, so the served model is not stable.
    # 128000 is therefore used: it is below anything observed to fail, and an
    # operator who has verified a larger window for THEIR endpoint can raise it.
    LLM_CONTEXT_WINDOW_TOKENS: int = Field(
        default=128000,
        ge=4096,
        description=(
            "The model's total context window in tokens, counting input and output "
            "together. Default 128000 is a conservative verified floor, not a claim "
            "that the configured model supports 300k."
        ),
    )
    LLM_CONTEXT_RESERVED_OUTPUT_TOKENS: int = Field(
        default=8000,
        ge=0,
        description=(
            "Tokens held back from the input budget for the model's reply. Set this "
            "when LLM_MAX_TOKENS is set so the reply always has room."
        ),
    )
    LLM_CONTEXT_SAFETY_MARGIN_TOKENS: int = Field(
        default=4000,
        ge=0,
        description=(
            "Slack for token-count estimation error and provider message framing. "
            "The estimator deliberately over-counts, so this is a second line of "
            "defence rather than the only one."
        ),
    )
    LLM_CONTEXT_COMPACTION_TRIGGER_TOKENS: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "Input size at which compaction starts. Unset = 80% of the usable "
            "input budget."
        ),
    )
    LLM_CONTEXT_COMPACTION_TARGET_TOKENS: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "Input size compaction aims for. Unset = 62% of the usable input "
            "budget, which leaves room for the next operation to add content "
            "without immediately re-triggering compaction."
        ),
    )
    LLM_CONTEXT_OVERFLOW_MAX_RETRIES: int = Field(
        default=1,
        ge=0,
        le=3,
        description=(
            "How many times a genuine context-overflow error may trigger a further, "
            "harder reduction and a retry. Bounded on purpose: retrying an unchanged "
            "oversized payload can never succeed."
        ),
    )
    LLM_CONTEXT_COMPACTION_MAX_ROUNDS: int = Field(
        default=4,
        ge=1,
        le=10,
        description=(
            "Maximum number of LLM summarisation rounds in a single compaction. Each "
            "round asks the SAME fixed model for a tighter summary and re-estimates the "
            "request. A ceiling, not the only termination condition: an unusable summary "
            "or insufficient progress stops the loop sooner."
        ),
    )
    LLM_CONTEXT_COMPACTION_MIN_PROGRESS_FRACTION: float = Field(
        default=0.05,
        ge=0.0,
        le=1.0,
        description=(
            "Fraction of the request a compaction round must remove for another round "
            "to run. Below it the round made no meaningful progress, so compaction stops "
            "rather than re-summarising effectively the same context forever."
        ),
    )
    LLM_CONTEXT_ESTIMATOR_CHARS_PER_TOKEN: float = Field(
        default=3.0,
        ge=1.5,
        le=8.0,
        description=(
            "Characters per token assumed by the estimator. Lower = more tokens "
            "counted = more conservative. Only one measurement exists: 4.5 "
            "chars/token for English prose on this endpoint. Code, JSON and hex "
            "are denser and were NOT measured, so the default sits well below "
            "the prose figure rather than at it. Never raise this to fit more "
            "content: an under-count is the one failure mode that matters."
        ),
    )
    LLM_REQUIRE_SERVED_MODEL_MATCH: bool = Field(
        default=False,
        description=(
            "Refuse a response whose reported model is not the configured model "
            "(or a dated variant of it). Off by default because a provider may "
            "legitimately answer with a versioned id such as "
            "'gpt-4o-2024-08-06'. Turn this on where routing must fail closed. "
            "A mismatch is always logged at WARNING either way."
        ),
    )

    # =========================================================================
    # DETERMINISTIC SECURITY & AUTONOMOUS SPENDING LIMITS
    # =========================================================================
    EMERGENCY_STOP_ENABLED: bool = Field(
        default=False,
        description="Global kill-switch stopping all execution and transactions",
    )
    EMERGENCY_STOP_REQUIRE_STATE: bool = Field(
        default=False,
        description=(
            "When true, a missing emergency stop state file at startup means 'stopped' until an "
            "operator resumes, even on first boot. A file that existed before is always detected "
            "via a marker next to it; this flag additionally covers the case where the whole data "
            "directory was lost."
        ),
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
    MAX_TRANSACTION_VALUE_USD: Optional[float] = Field(
        default=None,
        description=(
            "Optional operator-set ceiling on any individual on-chain transaction, in USD. "
            "Unset (the default) means no application-imposed transaction amount cap: a "
            "legitimate autonomous transfer is never refused for being large. Set a number "
            "to have the spend ledger enforce it atomically across processes. Value-moving "
            "authority itself is governed by the autonomous policy, not by this number."
        ),
    )
    MAX_DAILY_GAS_SPEND_USD: Optional[float] = Field(
        default=None,
        description=(
            "Optional operator-set ceiling on cumulative 24-hour spend, in USD. Unset (the "
            "default) means no application-imposed daily spend cap. Spend is still recorded "
            "in the ledger for accounting and for reconciling an ambiguous broadcast, so a "
            "ceiling can be turned on later without losing history. There is no daily "
            "transaction-count cap anywhere in the platform."
        ),
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
    MAX_PLAN_STEPS: int = Field(
        default=24,
        ge=6,
        le=80,
        description=(
            "Maximum steps in one execution plan. A Telegram or Mini App airdrop is "
            "genuinely multi-stage (open bot, start, read, join, verify membership, "
            "return to bot, read, act, verify, report), so this must be large enough "
            "that a legitimate workflow is not rejected outright."
        ),
    )
    MAX_PROSE_EXTRACTIONS_PER_RUN: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "Optional operator-set ceiling on how many times one task run may ask the "
            "model to summarise requirements out of untrusted external prose. Unset (the "
            "default) means no application-imposed LLM-call cap; runs stay bounded by "
            "MAX_EXECUTION_LOOP_ITERATIONS, cancellation and timeouts. Zero disables "
            "prose extraction entirely; structured requirements are still read."
        ),
    )
    MAX_DISCOVERY_REPLANS_PER_RUN: int = Field(
        default=3,
        ge=0,
        le=12,
        description=(
            "How many times a run may revise its plan because executing it discovered "
            "something new. Separate from the failure-replan budget: discovering that a "
            "bot wants a channel joined is progress, not a failure, and must not exhaust "
            "the retry allowance."
        ),
    )
    REQUIRE_CONFIRMATION_ABOVE_USD: float = Field(
        default=100.0,
        description="Threshold where user interaction is mandatory",
    )

    # Value moving stays off unless an operator turns it on for this deployment.
    # Even when enabled, every broadcast still requires a named human's server-side
    # approval of one specific transaction; this flag is not that approval.
    ALLOW_VALUE_MOVING: bool = Field(
        default=False,
        description=(
            "Allow the agent to hold wallet.sign / transaction.submit permissions and "
            "therefore to sign and broadcast. Off by default. Individual broadcasts "
            "still require an explicit, transaction-bound human approval."
        ),
    )
    # How long a quoted transfer stays approvable.
    APPROVAL_TTL_MINUTES: int = Field(default=15, ge=1, le=120)

    # ------------------------------------------------------------------
    # AUTONOMOUS EXECUTION POLICY
    # ------------------------------------------------------------------
    # What the agent may do without asking the owner first. Deny-by-default: with
    # neither of these set, every autonomous category is refused and the agent
    # behaves exactly as before.
    #
    # AUTONOMOUS_POLICY_FILE points at a JSON file and is preferred on Windows, where
    # embedding multi-line JSON in .env is a reliable source of escaping mistakes.
    # AUTONOMOUS_POLICY accepts the same JSON inline.
    #
    # The policy authorizes; the model never does. It decides which operation classes
    # run unattended and within which ceilings, and every decision is recorded against
    # the rule that made it. See docs/AUTONOMOUS_EXECUTION.md.
    AUTONOMOUS_POLICY_FILE: str = Field(
        default="",
        description="Path to a JSON file describing which operations may run unattended.",
    )
    AUTONOMOUS_POLICY: Optional[str] = Field(
        default=None,
        description="Inline JSON alternative to AUTONOMOUS_POLICY_FILE.",
    )
    # Refuse a broadcast whose receipt cannot be read, so "sent" is never claimed
    # without proof. Defaults on; turning it off means PENDING is reported as done.
    AUTONOMOUS_REQUIRE_RECEIPT_DEFAULT: bool = True

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

    # Operator-supplied ERC-20 contracts, keyed by network then symbol:
    #   TOKEN_CONTRACTS={"ethereum": {"MYTOKEN": "0x..."}}
    # A symbol is NEVER resolved by searching. An address that is neither in the
    # built-in catalog nor here is refused rather than guessed, because sending to
    # the wrong contract sends real money to whoever deployed it.
    TOKEN_CONTRACTS: Dict[str, Dict[str, str]] = Field(default_factory=dict)

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
    TELEGRAM_USER_DAILY_ACTION_LIMIT: int = 1000

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

    # ------------------------------------------------------------------
    # MANAGED PERSISTENT CHROME PROFILES
    # ------------------------------------------------------------------
    # A profile is a real Chrome user-data directory, so cookies, storage, installed
    # extensions and extension settings survive an AirdropOS restart. Chrome refuses to
    # open a user-data directory that another Chrome already owns, which is what makes
    # profile locking real rather than advisory.
    BROWSER_PERSISTENT_PROFILES: bool = Field(
        default=True,
        description=(
            "Launch Google Chrome with a dedicated persistent user-data directory per "
            "browser profile instead of an ephemeral Playwright context. Off = every task "
            "gets a throwaway profile (no extensions, no persistence)."
        ),
    )
    BROWSER_CHROME_EXECUTABLE: str = Field(
        default="",
        description=(
            "Absolute path to the Google Chrome executable. Empty = auto-detect the usual "
            "install locations, then fall back to the Playwright-managed Chromium."
        ),
    )
    BROWSER_PROFILE_ROOT: str = Field(
        default="",
        description=(
            "Root directory for managed Chrome profile directories. Empty = "
            "%LOCALAPPDATA%\\AirdropOS\\ChromeProfiles on Windows, "
            "~/.airdropos/chrome-profiles elsewhere. A profile id is appended to it."
        ),
    )
    BROWSER_HEADLESS_EXTENSIONS: bool = Field(
        default=False,
        description=(
            "Allow launching a managed Chrome profile headless. Off by default because "
            "extension support in Chrome's old headless mode was incomplete; the new "
            "headless mode supports them, so this only controls whether we rely on it."
        ),
    )
    BROWSER_ALLOW_INTERNAL_PAGES: bool = Field(
        default=True,
        description=(
            "Allow the agent to open a small allowlist of chrome:// pages (chrome://extensions, "
            "chrome://version) for extension management. Never chrome://settings, chrome://net-internals "
            "or any page that could change security-relevant browser state."
        ),
    )
    BROWSER_ALLOW_ATTACH: bool = Field(
        default=True,
        description=(
            "Allow attaching to an already-running Chrome over a loopback DevTools endpoint that "
            "the operator explicitly configured. Off = only managed profiles may be opened."
        ),
    )
    BROWSER_ATTACH_HOST: str = Field(
        default="127.0.0.1",
        description="Default DevTools host for 'connect to existing Chrome'. Loopback only.",
    )
    BROWSER_DOWNLOAD_DIR: str = Field(
        default="",
        description="Directory downloads are saved to. Empty = <BROWSER_USER_DATA_DIR>/downloads.",
    )
    BROWSER_MAX_UPLOAD_BYTES: int = Field(
        default=25 * 1024 * 1024, ge=0, le=512 * 1024 * 1024,
        description="Largest file the agent may upload through a page file chooser.",
    )
    BROWSER_MAX_TABS: int = Field(
        default=20, ge=2, le=100,
        description="Tab ceiling per browser session, so a runaway task cannot open hundreds.",
    )

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

    @field_validator("MAX_PROSE_EXTRACTIONS_PER_RUN", mode="before")
    @classmethod
    def _blank_prose_cap_means_unset(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip().lower() in ("", "none", "null"):
            return None
        return value

    @field_validator("MAX_TRANSACTION_VALUE_USD", "MAX_DAILY_GAS_SPEND_USD", mode="before")
    @classmethod
    def _blank_spend_ceiling_means_unset(cls, value: Any) -> Any:
        """`MAX_TRANSACTION_VALUE_USD=` (blank) or `none` means no ceiling.

        These are optional operator ceilings, not platform policy. An operator who
        leaves one blank must not silently inherit a built-in cap they never set.
        """
        if isinstance(value, str) and value.strip().lower() in ("", "none", "null", "unlimited"):
            return None
        return value

    @field_validator("LLM_MAX_TOKENS", mode="before")
    @classmethod
    def _blank_llm_max_tokens_means_unset(cls, value: Any) -> Any:
        """`LLM_MAX_TOKENS=` (blank) or 0 means no limit, not a parse error."""
        if value is None:
            return None
        if isinstance(value, str) and value.strip().lower() in ("", "0", "none", "null"):
            return None
        if value == 0:
            return None
        return value

    @field_validator(
        "LLM_CONTEXT_COMPACTION_TRIGGER_TOKENS",
        "LLM_CONTEXT_COMPACTION_TARGET_TOKENS",
        mode="before",
    )
    @classmethod
    def _blank_context_budget_means_derived(cls, value: Any) -> Any:
        """A blank threshold means "derive it from the usable budget"."""
        if isinstance(value, str) and value.strip().lower() in ("", "none", "null"):
            return None
        return value

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
    def validate_context_budget(self) -> "Settings":
        """Reject a context budget that could not possibly be honoured.

        A trigger above the usable budget would silently disable compaction; a
        target above the trigger would make compaction useless. Both are far
        cheaper to catch at startup than to discover during a live task.
        """
        usable = (
            self.LLM_CONTEXT_WINDOW_TOKENS
            - self.LLM_CONTEXT_RESERVED_OUTPUT_TOKENS
            - self.LLM_CONTEXT_SAFETY_MARGIN_TOKENS
        )
        if usable <= 0:
            raise ValueError(
                "LLM_CONTEXT_RESERVED_OUTPUT_TOKENS plus LLM_CONTEXT_SAFETY_MARGIN_TOKENS "
                f"({self.LLM_CONTEXT_RESERVED_OUTPUT_TOKENS + self.LLM_CONTEXT_SAFETY_MARGIN_TOKENS}) "
                "must be smaller than LLM_CONTEXT_WINDOW_TOKENS "
                f"({self.LLM_CONTEXT_WINDOW_TOKENS}). There is no room for any input."
            )

        trigger = self.LLM_CONTEXT_COMPACTION_TRIGGER_TOKENS or int(usable * 0.80)
        target = self.LLM_CONTEXT_COMPACTION_TARGET_TOKENS or int(usable * 0.62)

        if target > trigger:
            raise ValueError(
                f"LLM_CONTEXT_COMPACTION_TARGET_TOKENS ({target}) must not exceed "
                f"LLM_CONTEXT_COMPACTION_TRIGGER_TOKENS ({trigger}); compaction could "
                "never bring a request below the trigger."
            )
        if trigger > usable:
            raise ValueError(
                f"LLM_CONTEXT_COMPACTION_TRIGGER_TOKENS ({trigger}) exceeds the usable "
                f"input budget ({usable}). Compaction would never run before the "
                "request was submitted."
            )
        if target > usable:
            raise ValueError(
                f"LLM_CONTEXT_COMPACTION_TARGET_TOKENS ({target}) exceeds the usable "
                f"input budget ({usable})."
            )
        return self

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
