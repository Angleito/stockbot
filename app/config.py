"""Configuration: loads .env, fails fast on missing keys.

edgartools identity configuration lives behind the edgar boundary in
``app/edgar_client.py``; this module only validates the env values it needs.
"""

import logging
import os
from typing import Optional

from dotenv import load_dotenv

from .log_stream import LogStreamHandler
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
EXA_API_BASE = "https://api.exa.ai"

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
    """Validate env vars required by the research/chat tool path.

    Raises ValueError if OPENROUTER_API_KEY or SEC_EDGAR_IDENTITY is missing.
    """
    _require_env("SEC_EDGAR_IDENTITY")
    _require_env("OPENROUTER_API_KEY")


def get_sec_edgar_identity() -> str:
    """The SEC EDGAR identity string (validated, placeholder-rejected)."""
    return _require_env("SEC_EDGAR_IDENTITY")


def get_default_model() -> str:
    return os.getenv("DEFAULT_MODEL", FALLBACK_MODEL)


def _positive_env(name: str, default: float, *, integer: bool = False) -> int | float:
    """Read a positive numeric setting without silently accepting bad limits."""
    label = "integer" if integer else "number"
    value = (os.getenv(name) or "").strip()
    if not value:
        return default
    try:
        parsed = int(value) if integer else float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive {label}") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive {label}")
    return parsed


def _env_bool(name: str) -> bool:
    """True when the env var is set to a truthy value (1/true/yes)."""
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(*, stream_url: str | None = None) -> None:
    """Configure root logging: WARNING on stderr by default; when stream_url
    is given, attach a LogStreamHandler and log everything (DEBUG)."""
    level = logging.DEBUG if stream_url else logging.WARNING
    logging.basicConfig(force=True, level=level, format=LOG_FORMAT)
    if stream_url:
        handler = LogStreamHandler(stream_url)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logging.getLogger().addHandler(handler)


def get_local_chat_policy() -> ChatPolicy:
    """Build the single-principal runtime chat policy from local config."""
    configured = (os.getenv("CHAT_ALLOWED_MODELS") or "").split(",")
    return ChatPolicy(
        allowed_models=frozenset({
            get_default_model(),
            *(item.strip() for item in configured if item.strip()),
        }),
        max_messages=_positive_env("CHAT_MAX_MESSAGES", 20, integer=True),
        max_message_chars=_positive_env("CHAT_MAX_CONTENT_CHARS", 12_000, integer=True),
        upstream_timeout_seconds=_positive_env("OPENROUTER_TIMEOUT_SECONDS", 60.0),
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
    return _env_bool("FINRA_USE_MOCK")


def get_robinhood_mcp_url() -> str:
    # Validate here as well as in OAuthConfig so environment and --server-url
    # paths cannot accidentally direct Robinhood credentials to another host.
    from .robinhood.auth import validate_robinhood_server_url

    return validate_robinhood_server_url(os.getenv(
        "ROBINHOOD_MCP_URL", "https://agent.robinhood.com/mcp/trading"
    ))


def broker_enabled() -> bool:
    if os.getenv("BROKER_ENABLED") is not None:
        return _env_bool("BROKER_ENABLED")
    return _env_bool("ROBINHOOD_ENABLED")


def exa_enabled() -> bool:
    """True when EXA_ENABLED is set to a truthy value."""
    return _env_bool("EXA_ENABLED")


def get_exa_api_key() -> Optional[str]:
    """Exa API key, or None when unset (integration is optional)."""
    value = (os.getenv("EXA_API_KEY") or "").strip()
    return value or None
