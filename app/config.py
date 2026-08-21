"""Configuration: loads .env, fails fast on missing keys, configures edgartools."""

import os

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

load_dotenv()


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if (
        not value
        or value.startswith("YourName")
        or value.startswith("sk-or-...")
        or value.startswith("your_finra_")
    ):
        raise ValueError(
            f"{name} is not properly set in your environment or .env file. "
            "Phase 1 requires it to run. See .env.example."
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
