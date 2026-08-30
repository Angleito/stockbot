"""Configuration: loads .env, fails fast on missing keys, configures edgartools."""

import os
from typing import Optional

from dotenv import load_dotenv
from edgar import set_identity

from .policy import ChatPolicy

# Default fallback only when the env var is unset. Keep env/CLI overrides
# authoritative; this value must be valid on the target OpenRouter account.
FALLBACK_MODEL = "google/gemini-2.5-flash"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
FINRA_TOKEN_URL = (
    "https://ews.fip.finra.org/fip/rest/ews/oauth2/access_token"
    "?grant_type=client_credentials"
)
FINRA_API_BASE = "https://api.finra.org"

# Analyst consensus (unofficial Yahoo endpoint) + index-weight data sources.
YAHOO_QUERY_BASE = "https://query2.finance.yahoo.com"
YAHOO_CRUMB_URL = "https://fc.yahoo.com"
SLICKCHARTS_SP500_URL = "https://www.slickcharts.com/sp500"

load_dotenv()


def _require_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    placeholders = ("Your Name", "YourName", "sk-or-...", "your_finra_")
    if not value or value.startswith(placeholders):
        raise ValueError(
            f"{name} is not properly set in your environment or .env file. "
            "See .env.example and configure this value before using the "
            "integration that requires it."
        )
    return value


def init_config() -> None:
    """Validate env vars and configure edgartools identity.

    Raises ValueError if OPENROUTER_API_KEY or SEC_EDGAR_IDENTITY is missing.
    """
    identity = _require_env("SEC_EDGAR_IDENTITY")
    _require_env("OPENROUTER_API_KEY")
    set_identity(identity)


def get_default_model() -> str:
    return os.getenv("DEFAULT_MODEL", FALLBACK_MODEL)


def _positive_int_env(name: str, default: int) -> int:
    """Read a positive integer setting without silently accepting bad limits."""
    value = (os.getenv(name) or "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _positive_float_env(name: str, default: float) -> float:
    value = (os.getenv(name) or "").strip()
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive number")
    return parsed


def get_allowed_chat_models() -> frozenset[str]:
    """Return the server-controlled OpenRouter model allowlist.

    The default model is always included.  Additional models must be named by
    the operator in ``CHAT_ALLOWED_MODELS``; an HTTP caller cannot select an
    arbitrary model (and its price/security characteristics).
    """
    configured = (os.getenv("CHAT_ALLOWED_MODELS") or "").split(",")
    return frozenset({get_default_model(), *(item.strip() for item in configured if item.strip())})


def get_chat_max_messages() -> int:
    return _positive_int_env("CHAT_MAX_MESSAGES", 20)


def get_chat_max_content_chars() -> int:
    return _positive_int_env("CHAT_MAX_CONTENT_CHARS", 12_000)


def get_openrouter_timeout_seconds() -> float:
    """Per-upstream-request timeout; bounds each model completion."""
    return _positive_float_env("OPENROUTER_TIMEOUT_SECONDS", 60.0)


def get_local_chat_policy() -> ChatPolicy:
    """Build the single-principal runtime chat policy from local config."""
    return ChatPolicy(
        allowed_models=get_allowed_chat_models(),
        max_messages=get_chat_max_messages(),
        max_message_chars=get_chat_max_content_chars(),
        upstream_timeout_seconds=get_openrouter_timeout_seconds(),
    )


def get_finra_analysis_model() -> Optional[str]:
    """Secondary low-cost OpenRouter model used to phrase FINRA briefings.

    Optional: when unset (or blank), FINRA analysis is deterministic-only.
    """
    value = (os.getenv("FINRA_ANALYSIS_MODEL") or "").strip()
    return value or None


def get_openrouter_api_key() -> str:
    return _require_env("OPENROUTER_API_KEY")


def get_finra_client_id() -> str:
    return _require_env("FINRA_CLIENT_ID")


def get_finra_client_secret() -> str:
    return _require_env("FINRA_CLIENT_SECRET")


def finra_use_mock() -> bool:
    return os.getenv("FINRA_USE_MOCK", "").strip().lower() in ("1", "true", "yes")


def get_robinhood_mcp_url() -> str:
    # Validate here as well as in OAuthConfig so environment and --server-url
    # paths cannot accidentally direct Robinhood credentials to another host.
    from .robinhood.auth import validate_robinhood_server_url

    return validate_robinhood_server_url(os.getenv(
        "ROBINHOOD_MCP_URL", "https://agent.robinhood.com/mcp/trading"
    ))


def robinhood_enabled() -> bool:
    return os.getenv("ROBINHOOD_ENABLED", "false").strip().lower() in (
        "1", "true", "yes"
    )
