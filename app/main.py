"""Local-first FastAPI boundary: health/observability (chat moved to Pi)."""

from __future__ import annotations

import os

from fastapi import FastAPI

from app.config import configure_logging, get_default_model
from app.policy import LOCAL_CONTEXT, RequestContext

configure_logging(stream_url=os.getenv("STOCKBOT_LOG_SERVER") or None)

app = FastAPI(title="Stockbot", version="0.1.0")


def get_request_context() -> RequestContext:
    """Single local principal; hosted deployments replace this dependency."""
    return LOCAL_CONTEXT


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "default_model": get_default_model()}
