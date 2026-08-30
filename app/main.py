"""Authenticated, bounded FastAPI boundary for the Stockbot chat agent."""

from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict, deque
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent import run_chat
from app.config import (
    get_allowed_chat_models,
    get_api_tokens,
    get_chat_concurrency_limit,
    get_chat_max_content_chars,
    get_chat_max_messages,
    get_chat_max_request_bytes,
    get_chat_rate_limit_requests,
    get_chat_rate_limit_window_seconds,
    get_default_model,
    get_portfolio_api_users,
)
from app.tools import PORTFOLIO_AUTHORIZED_TOOLS, TOOLS

app = FastAPI(title="Stockbot", version="0.1.0")


class ChatMessage(BaseModel):
    """Only user-authored messages are accepted from HTTP clients."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    role: Literal["user"]
    content: str = Field(..., min_length=1)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    messages: list[ChatMessage] = Field(..., min_length=1)
    model: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def enforce_conversation_bounds(self) -> "ChatRequest":
        if len(self.messages) > get_chat_max_messages():
            raise ValueError("too many messages")
        if any(len(message.content) > get_chat_max_content_chars() for message in self.messages):
            raise ValueError("message content is too long")
        return self


class ChatResponse(BaseModel):
    response: str
    model: str


class _PerUserRateLimiter:
    """Small in-memory fixed-window quota; use a shared store before scaling."""

    def __init__(self) -> None:
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, user: str, *, limit: int, window_seconds: float) -> bool:
        now = time.monotonic()
        with self._lock:
            requests = self._requests[user]
            cutoff = now - window_seconds
            while requests and requests[0] <= cutoff:
                requests.popleft()
            if len(requests) >= limit:
                return False
            requests.append(now)
            return True


_rate_limiter = _PerUserRateLimiter()
_chat_slots = threading.BoundedSemaphore(get_chat_concurrency_limit())
_ALL_TOOL_NAMES = frozenset(tool["function"]["name"] for tool in TOOLS)


async def _reject_oversized_request(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > get_chat_max_request_bytes():
                return JSONResponse(status_code=413, content={"detail": "request body is too large"})
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})
    # Content-Length is optional (for example, chunked HTTP requests). Reading
    # through Starlette's cached request body also makes that path obey the
    # same cap before Pydantic parses the JSON.
    if request.method in {"POST", "PUT", "PATCH"}:
        body = await request.body()
        if len(body) > get_chat_max_request_bytes():
            return JSONResponse(status_code=413, content={"detail": "request body is too large"})
    return await call_next(request)


app.middleware("http")(_reject_oversized_request)


def authenticate_user(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """Authenticate a configured bearer token without exposing token values."""
    configured = get_api_tokens()
    if not configured:
        raise HTTPException(status_code=503, detail="chat authentication is not configured")
    scheme, _, presented = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not presented:
        raise HTTPException(
            status_code=401,
            detail="Bearer authentication is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    for user, token in configured.items():
        if secrets.compare_digest(presented, token):
            return user
    raise HTTPException(
        status_code=401,
        detail="invalid authentication credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, user: Annotated[str, Depends(authenticate_user)]) -> ChatResponse:
    if not _rate_limiter.allow(
        user,
        limit=get_chat_rate_limit_requests(),
        window_seconds=get_chat_rate_limit_window_seconds(),
    ):
        raise HTTPException(status_code=429, detail="chat quota exceeded; retry later")

    model = req.model or get_default_model()
    if model not in get_allowed_chat_models():
        raise HTTPException(status_code=403, detail="requested model is not allowed")

    # Account holdings and saved scans are separately authorized. Other users
    # do not even see these schemas, and fabricated calls are rejected in the
    # agent loop before any tool is executed.
    allowed_tools = _ALL_TOOL_NAMES
    if user not in get_portfolio_api_users():
        allowed_tools = _ALL_TOOL_NAMES - PORTFOLIO_AUTHORIZED_TOOLS

    if not _chat_slots.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="chat server is at concurrency capacity")
    try:
        text = run_chat(
            [message.model_dump() for message in req.messages],
            model,
            allowed_tool_names=allowed_tools,
        )
    except ValueError as exc:
        # Configuration problems (missing API key etc.) are not caller errors.
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception:
        # Avoid reflecting upstream responses, which can include implementation
        # details or credentials, to an untrusted HTTP client.
        raise HTTPException(status_code=502, detail="upstream chat request failed") from None
    finally:
        _chat_slots.release()
    return ChatResponse(response=text, model=model)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "default_model": get_default_model()}
