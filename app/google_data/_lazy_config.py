"""Typed access to app.config with env fallback; never raises on ImportError."""
from __future__ import annotations
import os
from pathlib import Path

def google_data_enabled() -> bool:
    try:
        from .. import config as _cfg
    except ImportError:
        _cfg = None
    if _cfg is not None:
        try:
            return bool(_cfg.google_data_enabled())
        except Exception:
            return False
    return os.getenv("GOOGLE_DATA_ENABLED", "").strip().lower() in ("1", "true", "yes")

def get_datacommons_api_key() -> str | None:
    try:
        from .. import config as _cfg
    except ImportError:
        _cfg = None
    if _cfg is not None:
        try:
            value = _cfg.get_datacommons_api_key()
            if value:
                return str(value)
        except Exception:
            pass
    return (os.getenv("DATACOMMONS_API_KEY") or "").strip() or None

def get_data_root_or_cwd() -> Path:
    try:
        from .. import config as _cfg
    except ImportError:
        _cfg = None
    if _cfg is not None:
        try:
            return Path(_cfg.get_data_root())
        except Exception:
            pass
    return Path(os.getenv("STOCKBOT_DATA_DIR", "data"))
