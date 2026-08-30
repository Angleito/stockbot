"""Configuration: loads .env, fails fast on missing keys, configures edgartools."""

import os
from typing import Optional

from dotenv import load_dotenv
from edgar import set_identity

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


def get_api_tokens() -> dict[str, str]:
    """Return configured API principals from ``API_AUTH_TOKENS``.

    The format is ``user=secret,user2=secret2``.  A malformed setting is
    rejected rather than accidentally creating an anonymous API.  The legacy
    single-token setting is supported only as the ``local`` principal.
    """
    raw = (os.getenv("API_AUTH_TOKENS") or "").strip()
    if not raw:
        legacy = (os.getenv("API_AUTH_TOKEN") or "").strip()
        return {"local": legacy} if legacy else {}
    tokens: dict[str, str] = {}
    for entry in raw.split(","):
        user, separator, token = entry.strip().partition("=")
        if not separator or not user or not token or user in tokens:
            raise ValueError(
                "API_AUTH_TOKENS must be comma-separated user=secret entries"
            )
        tokens[user] = token
    return tokens


def get_portfolio_api_users() -> frozenset[str]:
    """Users explicitly allowed to expose Robinhood portfolio data via chat."""
    return frozenset(
        user.strip() for user in (os.getenv("API_PORTFOLIO_USERS") or "").split(",")
        if user.strip()
    )


def get_chat_max_messages() -> int:
    return _positive_int_env("CHAT_MAX_MESSAGES", 20)


def get_chat_max_content_chars() -> int:
    return _positive_int_env("CHAT_MAX_CONTENT_CHARS", 12_000)


def get_chat_max_request_bytes() -> int:
    return _positive_int_env("CHAT_MAX_REQUEST_BYTES", 64_000)


def get_chat_concurrency_limit() -> int:
    return _positive_int_env("CHAT_CONCURRENCY_LIMIT", 4)


def get_chat_rate_limit_requests() -> int:
    return _positive_int_env("CHAT_RATE_LIMIT_REQUESTS", 20)


def get_chat_rate_limit_window_seconds() -> float:
    return _positive_float_env("CHAT_RATE_LIMIT_WINDOW_SECONDS", 60.0)


def get_openrouter_timeout_seconds() -> float:
    """Per-upstream-request timeout; bounds each model completion."""
    return _positive_float_env("OPENROUTER_TIMEOUT_SECONDS", 60.0)


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
