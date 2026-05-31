"""DijkFood conversational agent API. ALB path prefix: /agent"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from agent import agent_functions
from agent.bedrock_runner import run_chat
from agent.sessions import delete_session, load_messages, new_conversation_id, save_messages
from agent.usage import get_usage_summary, record_chat_usage

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="DijkFood agent")
agent = FastAPI(title="DijkFood agent routes")

_cors_origins = [
    o.strip()
    for o in (os.environ.get("AGENT_CORS_ORIGINS") or "").split(",")
    if o.strip()
]
if _cors_origins:
    agent.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
    conversation_id: str | None = Field(default=None, max_length=64)


class RequestUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    bedrock_rounds: int = 0
    tool_calls: int = 0


class ChatResponse(BaseModel):
    conversation_id: str
    reply: str
    tools_used: list[dict] = Field(default_factory=list)
    usage: RequestUsage = Field(default_factory=RequestUsage)


class UsageBudget(BaseModel):
    total_tokens: int | None = None
    daily_tokens: int | None = None
    total_used_pct: float | None = None
    daily_used_pct: float | None = None
    today_tokens: int = 0


class UsageTotals(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    request_count: int = 0
    bedrock_rounds: int = 0
    tool_calls: int = 0


class DailyUsage(UsageTotals):
    date: str


class UsageSummaryResponse(BaseModel):
    totals: UsageTotals
    daily: list[DailyUsage] = Field(default_factory=list)
    budget: UsageBudget
    updated_at: str | None = None
    model_id: str | None = None
    max_tool_rounds: int = 5
    max_output_tokens: int = 2048


@agent.get("/health")
def agent_health() -> dict[str, str]:
    return {"status": "ok", "service": "agent"}


@agent.get("/v1/tools")
def list_tools() -> dict:
    return {"tools": agent_functions.list_tools_metadata()}


@agent.get("/v1/usage", response_model=UsageSummaryResponse)
def usage_summary() -> UsageSummaryResponse:
    try:
        data = get_usage_summary()
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return UsageSummaryResponse(**data)


@agent.post("/v1/chat", response_model=ChatResponse)
def chat(body: ChatRequest) -> ChatResponse:
    conversation_id = (body.conversation_id or "").strip() or new_conversation_id()
    try:
        history = load_messages(conversation_id) if body.conversation_id else []
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    try:
        reply, updated_messages, tools_used, req_usage = run_chat(
            history, user_message=body.message.strip()
        )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    try:
        save_messages(conversation_id, updated_messages)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    try:
        record_chat_usage(
            input_tokens=req_usage.get("input_tokens", 0),
            output_tokens=req_usage.get("output_tokens", 0),
            total_tokens=req_usage.get("total_tokens", 0),
            bedrock_rounds=req_usage.get("bedrock_rounds", 0),
            tool_calls=req_usage.get("tool_calls", 0),
        )
    except Exception:
        log.exception("failed to record usage metrics")

    return ChatResponse(
        conversation_id=conversation_id,
        reply=reply,
        tools_used=tools_used,
        usage=RequestUsage(
            input_tokens=req_usage.get("input_tokens", 0),
            output_tokens=req_usage.get("output_tokens", 0),
            total_tokens=req_usage.get("total_tokens", 0),
            bedrock_rounds=req_usage.get("bedrock_rounds", 0),
            tool_calls=req_usage.get("tool_calls", 0),
        ),
    )


@agent.delete("/v1/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def clear_conversation(conversation_id: str) -> None:
    if not delete_session(conversation_id):
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="delete failed")


@app.get("/health")
def root_health() -> dict[str, str]:
    return {"status": "ok"}


app.mount("/agent", agent)
