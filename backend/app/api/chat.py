"""AI research chat endpoints (server-sent events). The OpenAI key never leaves the backend."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.common import api_error, current_mode, iso
from app.config import get_settings
from app.db import get_db, session_factory
from app.models import ChatConversation, ChatSourceReference, ChatToolExecution, Company
from app.services.analysis import normalize_ticker
from app.services.chat.context import latest_analysis
from app.services.chat.service import ChatError, ChatService, budget_status, get_conversation, messages_of

router = APIRouter(prefix="/api/stocks", tags=["chat"])

SUGGESTED_PROMPTS = [
    "Why is this stock a good investment?",
    "Why might this stock be a bad investment?",
    "Explain the 12-month base-case forecast.",
    "How is this stock rated, and why?",
    "Which financial assumptions drive the target price?",
    "How much upside comes from earnings growth versus P/E expansion?",
    "What could invalidate the bullish investment thesis?",
    "What happens if revenue growth slows?",
    "How does the DCF differ from the Forward P/E valuation?",
    "What changed since the previous forecast?",
    "What are the upcoming catalysts?",
    "Why did EventAlpha rank this company where it did?",
]


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    conversation_id: str | None = Field(None, max_length=64)


def _sse(ev: dict[str, Any]) -> str:
    return f"event: {ev['type']}\ndata: {json.dumps(ev, default=str)}\n\n"


@router.post("/{ticker}/chat", summary="Ask the AI research analyst about this stock (server-sent events)")
def chat(ticker: str, body: ChatRequest, mode: str = Depends(current_mode)) -> StreamingResponse:
    t = normalize_ticker(ticker)
    session = session_factory()()
    try:
        events = ChatService(session).ask(mode, t, body.message, body.conversation_id)
    except ChatError as exc:
        session.close()
        raise api_error(exc.status, exc.code, exc.args[0]) from exc
    except Exception:
        session.close()
        raise

    def stream():
        try:
            for ev in events:
                yield _sse(ev)
        except ChatError as exc:
            yield _sse({"type": "error", "code": exc.code, "message": exc.args[0]})
        except Exception:  # never leak internals to the client
            session.rollback()
            yield _sse({"type": "error", "code": "internal_error", "message": "The chat request failed."})
        finally:
            session.close()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/{ticker}/chat/history", summary="Chat conversation for this stock")
def chat_history(ticker: str, conversation_id: str | None = Query(None, max_length=64), db: Session = Depends(get_db),
                 mode: str = Depends(current_mode)) -> dict[str, Any]:
    t = normalize_ticker(ticker)
    s = get_settings()
    company = db.scalar(select(Company).where(Company.ticker == t, Company.data_mode == mode))
    latest = latest_analysis(db, company.id) if company else None
    try:
        conv = get_conversation(db, mode, t, conversation_id)
    except ChatError as exc:
        raise api_error(exc.status, exc.code, exc.args[0]) from exc
    msgs = messages_of(db, conv) if conv else []
    refs: dict[int, list[dict[str, Any]]] = {}
    tools: dict[int, list[dict[str, Any]]] = {}
    ids = [m.id for m in msgs]
    if ids:
        for r in db.scalars(select(ChatSourceReference).where(ChatSourceReference.message_id.in_(ids))):
            refs.setdefault(r.message_id, []).append({"label": r.label, "type": r.source_type, "title": r.title,
                                                      "url": r.url, "published_at": r.published_at})
        for e in db.scalars(select(ChatToolExecution).where(ChatToolExecution.message_id.in_(ids))):
            tools.setdefault(e.message_id, []).append({"name": e.tool_name, "status": e.status})
    return {
        "data_mode": mode, "ticker": t, "configured": s.openai_configured, "model": s.openai_chat_model,
        "conversation_id": str(conv.id) if conv else None,
        "conversation_analysis_id": conv.analysis_id if conv else None,
        "latest_analysis_id": latest.id if latest else None,
        "forecast_changed": bool(conv and latest and conv.analysis_id and conv.analysis_id != latest.id),
        "messages": [{"id": m.id, "role": m.role, "content": m.content, "created_at": iso(m.created_at),
                      "status": m.status, "analysis_id": m.analysis_id, "forecast_model_version": m.forecast_model_version,
                      "ai_model": m.ai_model, "cost_usd": m.cost_usd, "citations": refs.get(m.id, []),
                      "scenario": m.scenario, "follow_ups": m.follow_ups or [], "tools": tools.get(m.id, []),
                      "freshness": (m.context_meta or {}).get("freshness")} for m in msgs],
        "suggested_prompts": SUGGESTED_PROMPTS,
        "budget": budget_status(db, s),
    }


@router.delete("/{ticker}/chat/history", summary="Clear this stock's conversation (spend records are kept)")
def clear_chat_history(ticker: str, db: Session = Depends(get_db), mode: str = Depends(current_mode)) -> dict[str, Any]:
    t = normalize_ticker(ticker)
    convs = db.scalars(select(ChatConversation).where(ChatConversation.data_mode == mode, ChatConversation.ticker == t,
                                                      ChatConversation.cleared_at.is_(None))).all()
    for c in convs:
        c.cleared_at = datetime.now(UTC)
    db.commit()
    return {"ticker": t, "cleared": len(convs)}
