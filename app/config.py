"""Configuration: loads .env, fails fast on missing keys.

edgartools identity configuration lives behind the edgar boundary in
``app/edgar_client.py``; this module only validates the env values it needs.
"""

import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from .log_stream import LogStreamHandler

REPO_ROOT = Path(__file__).resolve().parent.parent


def get_data_root() -> Path:
    """Single durable-data root: STOCKBOT_DATA_DIR else <repo>/data."""
    raw = (os.getenv("STOCKBOT_DATA_DIR") or "").strip()
    if not raw:
        return REPO_ROOT / "data"
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = REPO_ROOT / p
    return p

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


def init_config() -> None:
    """Validate env vars required by the SEC research path.

    Raises ValueError if SEC_EDGAR_IDENTITY is missing.
    """
    _require_env("SEC_EDGAR_IDENTITY")


def get_sec_edgar_identity() -> str:
    """The SEC EDGAR identity string (validated, placeholder-rejected)."""
    return _require_env("SEC_EDGAR_IDENTITY")


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
