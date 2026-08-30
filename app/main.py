"""FastAPI app exposing the /chat endpoint. UI-agnostic: same agent as the CLI."""

from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.agent import run_chat
from app.config import get_default_model

app = FastAPI(title="Stockbot", version="0.1.0")


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(..., min_length=1)
    model: Optional[str] = None


class ChatResponse(BaseModel):
    response: str
    model: str


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    model = req.model or get_default_model()
    try:
        text = run_chat([m.model_dump() for m in req.messages], model)
    except ValueError as e:
        # Config problems (missing API key etc.)
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {e}")
    return ChatResponse(response=text, model=model)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "default_model": get_default_model()}
