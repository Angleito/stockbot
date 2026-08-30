"""Untrusted, local-first FastAPI boundary for the Stockbot chat agent."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.agent import run_chat
from app.config import get_default_model, get_local_chat_policy
from app.policy import ChatInputError, LOCAL_CONTEXT, RequestContext

app = FastAPI(title="Stockbot", version="0.1.0")


class ChatMessage(BaseModel):
    """Only public conversation history is accepted from HTTP clients."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    messages: list[ChatMessage] = Field(..., min_length=1)
    model: str | None = Field(default=None, max_length=200)

class ChatResponse(BaseModel):
    response: str
    model: str


def get_request_context() -> RequestContext:
    """Single local principal; hosted deployments replace this dependency."""
    return LOCAL_CONTEXT


@app.post("/chat", response_model=ChatResponse)
def chat(
    req: ChatRequest,
    context: Annotated[RequestContext, Depends(get_request_context)],
) -> ChatResponse:
    model = req.model or get_default_model()
    try:
        policy = get_local_chat_policy()
    except ValueError:
        raise HTTPException(status_code=500, detail="chat configuration is invalid") from None
    if model not in policy.allowed_models:
        raise HTTPException(status_code=403, detail="requested model is not allowed")
    try:
        text = run_chat(
            [message.model_dump() for message in req.messages],
            model,
            context=context,
            policy=policy,
        )
    except ChatInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError:
        raise HTTPException(status_code=500, detail="chat configuration is invalid") from None
    except Exception:
        # Avoid reflecting upstream responses, which can include implementation
        # details or credentials, to an untrusted HTTP client.
        raise HTTPException(status_code=502, detail="upstream chat request failed") from None
    return ChatResponse(response=text, model=model)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "default_model": get_default_model()}
