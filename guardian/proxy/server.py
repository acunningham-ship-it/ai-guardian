"""
AI Guardian — Core Proxy

Sits between clients (Claude Code, Cursor, custom apps) and AI providers
(OpenAI, Anthropic, etc.). Intercepts requests, applies cost/quality/agent
guardrails, then forwards to the actual provider.

Compatible with the OpenAI Chat Completions API format, so any tool that
supports a custom base_url (Cursor, Continue, LiteLLM, etc.) can use this
by pointing base_url at Guardian instead of directly at the provider.
"""
import asyncio
import datetime
import json
import time
import uuid
from typing import Any, AsyncGenerator, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from guardian.cost.router import route_model
from guardian.cost.tracker import estimate_cost, check_budget
from guardian.models import settings as app_settings
from guardian.models.database import init_db, async_session, UsageLog, get_spent_since
from guardian.models.schemas import (
    ChatMessage, ProxyRequest, ProxyResponse, QualityReport,
)
from guardian.quality.checker import check_quality

app = FastAPI(title="AI Guardian", version="0.1.0")

# ── Provider Base URLs ─────────────────────────────────────────────

PROVIDER_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "deepseek": "https://api.deepseek.com/v1",
}

# Map model prefixes to providers
MODEL_PROVIDER_PREFIXES = {
    "openai/": "openai",
    "anthropic/": "anthropic",
    "google/": "google",
    "deepseek/": "deepseek",
    "gpt-": "openai",
    "claude-": "anthropic",
    "gemini-": "google",
    "o1": "openai",
    "o3-": "openai",
    "deepseek-": "deepseek",
}


def resolve_provider(model: str) -> str:
    """Resolve model string to provider name."""
    model_lower = model.lower()
    for prefix, provider in MODEL_PROVIDER_PREFIXES.items():
        if model_lower.startswith(prefix):
            return provider
    # Default based on common patterns
    if "claude" in model_lower:
        return "anthropic"
    if "gpt" in model_lower or "o1" in model_lower or "o3" in model_lower:
        return "openai"
    if "gemini" in model_lower:
        return "google"
    if "deepseek" in model_lower:
        return "deepseek"
    return "openai"  # fallback


def get_api_key(provider: str, request: Request) -> str:
    """Extract API key from request header. Users pass their own keys."""
    # Support: Authorization: Bearer <key> or x-api-key: <key>
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.headers.get("x-api-key", "")


# ── Middleware: Request ID & Timing ─────────────────────────────────

@app.middleware("http")
async def add_request_metadata(request: Request, call_next):
    request.state.request_id = str(uuid.uuid4())[:8]
    request.state.start_time = time.time()
    response = await call_next(request)
    elapsed = time.time() - request.state.start_time
    response.headers["X-Guardian-Request-Id"] = request.state.request_id
    response.headers["X-Guardian-Time"] = f"{elapsed:.3f}s"
    return response


# ── Startup ────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    await init_db()


# ── Health Check ───────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "ai-guardian", "version": "0.1.0"}


# ── Dashboard Stats ────────────────────────────────────────────────

@app.get("/guardian/stats/{user_id}")
async def get_stats(user_id: str):
    """Get usage statistics for a user."""
    async with async_session() as session:
        from sqlalchemy import func, select

        # Total requests and cost
        q = select(
            func.count(UsageLog.id),
            func.sum(UsageLog.cost_usd),
            func.sum(UsageLog.total_tokens),
            func.avg(UsageLog.quality_score),
        ).where(UsageLog.user_id == user_id)
        result = await session.execute(q)
        row = result.one()

        # Top models
        q2 = select(
            UsageLog.model,
            func.count(UsageLog.id).label("count"),
            func.sum(UsageLog.cost_usd).label("cost"),
        ).where(
            UsageLog.user_id == user_id
        ).group_by(UsageLog.model).order_by(func.count(UsageLog.id).desc()).limit(5)
        models_result = await session.execute(q2)
        top_models = [
            {"model": r[0], "requests": r[1], "cost": round(r[2] or 0, 4)}
            for r in models_result.all()
        ]

        # Budget remaining
        now = datetime.datetime.utcnow()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        from guardian.models.database import get_spent_since
        spent = await get_spent_since(session, user_id, month_start)

        return {
            "total_requests": row[0] or 0,
            "total_cost_usd": round(row[1] or 0, 4),
            "total_tokens": row[2] or 0,
            "avg_quality_score": round(row[3] or 0, 1),
            "monthly_budget_remaining": round(max(0, app_settings.default_monthly_budget - spent), 4),
            "top_models": top_models,
        }


@app.get("/guardian/budget/{user_id}")
async def get_budget(user_id: str):
    """Get current budget status."""
    budget = await check_budget(user_id)
    return budget


# ── Dashboard ──────────────────────────────────────────────────────

@app.get("/dashboard/{user_id}", response_class=HTMLResponse)
async def dashboard(user_id: str, request: Request):
    """Simple HTML dashboard for a user."""
    from sqlalchemy import select, desc

    stats = await _get_user_stats(user_id)
    budget = await check_budget(user_id)

    async with async_session() as session:
        q = select(UsageLog).where(
            UsageLog.user_id == user_id
        ).order_by(desc(UsageLog.created_at)).limit(20)
        result = await session.execute(q)
        recent_logs = result.scalars().all()

    # Build simple HTML inline
    html = _render_dashboard(user_id, stats, budget, recent_logs)
    return HTMLResponse(content=html)


async def _get_user_stats(user_id: str) -> dict:
    from sqlalchemy import func, select
    async with async_session() as session:
        q = select(
            func.count(UsageLog.id),
            func.sum(UsageLog.cost_usd),
            func.sum(UsageLog.total_tokens),
            func.avg(UsageLog.quality_score),
        ).where(UsageLog.user_id == user_id)
        result = await session.execute(q)
        row = result.one()

        q2 = select(
            UsageLog.model,
            func.count(UsageLog.id).label("count"),
            func.sum(UsageLog.cost_usd).label("cost"),
        ).where(
            UsageLog.user_id == user_id
        ).group_by(UsageLog.model).order_by(func.count(UsageLog.id).desc()).limit(5)
        models_result = await session.execute(q2)
        top_models = [
            {"model": r[0], "requests": r[1], "cost": round(r[2] or 0, 4)}
            for r in models_result.all()
        ]

        now = datetime.datetime.utcnow()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        spent = await get_spent_since(session, user_id, month_start)

        return {
            "total_requests": row[0] or 0,
            "total_cost_usd": round(row[1] or 0, 4),
            "total_tokens": row[2] or 0,
            "avg_quality_score": round(row[3] or 0, 1),
            "top_models": top_models,
            "monthly_budget_remaining": round(max(0, app_settings.default_monthly_budget - spent), 4),
        }


def _render_dashboard(user_id: str, stats: dict, budget, recent_logs) -> str:
    """Render a simple HTML dashboard."""
    quality_class = ""
    if stats["avg_quality_score"] < 50:
        quality_class = "style='color:#ff4444'"
    elif stats["avg_quality_score"] < 70:
        quality_class = "style='color:#ffaa00'"

    budget_class = ""
    if stats["monthly_budget_remaining"] < 5:
        budget_class = "style='color:#ff4444'"
    elif stats["monthly_budget_remaining"] < 20:
        budget_class = "style='color:#ffaa00'"

    models_rows = ""
    for m in stats["top_models"]:
        models_rows += f"<tr><td><code>{m['model']}</code></td><td>{m['requests']}</td><td>${m['cost']:.4f}</td></tr>"

    logs_rows = ""
    for log in recent_logs:
        q = f"{log.quality_score:.1f}" if log.quality_score else "—"
        w = json.dumps(json.loads(log.warnings)) if log.warnings else "—"
        logs_rows += f"<tr><td>{log.created_at.strftime('%H:%M:%S') if log.created_at else '—'}</td><td><code>{log.model[:30]}</code></td><td>{log.provider}</td><td>{log.total_tokens}</td><td>${log.cost_usd:.6f}</td><td>{q}</td><td title='{w}'>⚠️</td></tr>"

    return f"""<!DOCTYPE html>
<html><head><title>AI Guardian — {user_id}</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0a0f; color: #e0e0e0; padding: 2rem; }}
h1 {{ color: #00ff88; margin-bottom: 1.5rem; }}
h2 {{ color: #88ccff; margin: 1.5rem 0 0.8rem; font-size: 1.1rem; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem; margin-bottom: 2rem; }}
.card {{ background: #151520; border: 1px solid #2a2a3a; border-radius: 8px; padding: 1rem; }}
.card .label {{ font-size: 0.75rem; color: #888; text-transform: uppercase; }}
.card .value {{ font-size: 1.6rem; font-weight: bold; color: #00ff88; margin-top: 0.3rem; }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
th, td {{ padding: 0.5rem; text-align: left; border-bottom: 1px solid #2a2a3a; }}
th {{ color: #888; font-size: 0.75rem; text-transform: uppercase; }}
code {{ background: #1a1a2a; padding: 0.1rem 0.3rem; border-radius: 3px; font-size: 0.8rem; }}
.refresh {{ float: right; background: #2a2a3a; color: #88ccff; border: 1px solid #3a3a5a; padding: 0.3rem 0.8rem; border-radius: 4px; cursor: pointer; }}
</style></head>
<body>
<button class="refresh" onclick="location.reload()">Refresh</button>
<h1>🛡️ AI Guardian — {user_id}</h1>
<div class="grid">
    <div class="card"><div class="label">Requests</div><div class="value">{stats['total_requests']}</div></div>
    <div class="card"><div class="label">Total Cost</div><div class="value">${stats['total_cost_usd']:.4f}</div></div>
    <div class="card"><div class="label">Tokens</div><div class="value">{stats['total_tokens']:,}</div></div>
    <div class="card"><div class="label">Avg Quality</div><div class="value" {quality_class}>{stats['avg_quality_score']:.1f}</div></div>
    <div class="card"><div class="label">Budget Left</div><div class="value" {budget_class}>${stats['monthly_budget_remaining']:.2f}</div></div>
</div>
<h2>Top Models</h2>
<table><tr><th>Model</th><th>Requests</th><th>Cost</th></tr>{models_rows}</table>
<h2>Recent Requests</h2>
<table><tr><th>Time</th><th>Model</th><th>Provider</th><th>Tokens</th><th>Cost</th><th>Quality</th><th>Info</th></tr>{logs_rows}</table>
</body></html>"""


# ── Proxy: Chat Completions ────────────────────────────────────────

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    Main proxy endpoint — compatible with OpenAI Chat Completions API.
    Clients send requests here; Guardian applies guardrails then forwards
    to the actual provider.
    """
    body = await request.json()
    req_id = request.state.request_id

    # Parse request
    model = body.get("model", "openai/gpt-4o")
    messages = body.get("messages", [])
    max_tokens = body.get("max_tokens", app_settings.max_tokens_per_request)
    temperature = body.get("temperature", 1.0)
    stream = body.get("stream", False)

    # Extract user/agent metadata from custom headers
    user_id = request.headers.get("x-guardian-user", "default")
    project_id = request.headers.get("x-guardian-project")
    agent_id = request.headers.get("x-guardian-agent")
    session_id = request.headers.get("x-guardian-session")

    provider = resolve_provider(model)

    # ── LAYER 1: Budget Check ──────────────────────────────────────
    budget = await check_budget(user_id, project_id=project_id)
    hard_cap = budget.would_exceed and budget.status.value == "exceeded"

    if hard_cap:
        # Still allow if it's a very small request (< $0.01)
        estimated = estimate_cost(model, 1000, 500)
        if estimated > 0.01:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "budget_exceeded",
                    "message": f"Monthly budget exhausted. Spent ${budget.monthly_spent:.2f} of ${budget.monthly_budget:.2f}",
                    "budget": budget.dict(),
                },
            )

    # ── LAYER 2: Model Routing ────────────────────────────────────
    budget_remaining = max(0, budget.monthly_budget - budget.monthly_spent)
    routing = route_model(
        model, messages,
        budget_remaining=budget_remaining,
        prefer_cheap=app_settings.prefer_cheap_models,
    )
    routed_model = routing.routed_model

    # ── Forward Request to Provider ───────────────────────────────
    api_key = get_api_key(provider, request)
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail=f"No API key for {provider}. Pass via Authorization: Bearer <key> or x-api-key header.",
        )

    # Build provider-specific request
    provider_url = PROVIDER_URLS[provider]
    headers = {"Content-Type": "application/json"}
    anthropic_body = None
    google_body = None
    provider_body = None

    if provider == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        anthropic_body = _to_anthropic_format(routed_model, messages, max_tokens, temperature, stream)
    elif provider == "google":
        headers["x-goog-api-key"] = api_key
        google_body = _to_google_format(routed_model, messages, max_tokens, temperature)
        provider_url = f"{provider_url}/models/{routed_model}:generateContent"
    else:
        headers["Authorization"] = f"Bearer {api_key}"
        provider_body = {
            "model": routed_model,
            "messages": messages,
            "max_tokens": min(max_tokens, app_settings.max_tokens_per_request),
            "temperature": temperature,
            "stream": stream,
        }

    # Make the actual API call
    async with httpx.AsyncClient(timeout=120) as client:
        if provider == "anthropic":
            if stream:
                resp = await client.post(
                    f"{provider_url}/messages",
                    headers=headers,
                    json={**anthropic_body, "stream": True},
                )
            else:
                resp = await client.post(
                    f"{provider_url}/messages",
                    headers=headers,
                    json=anthropic_body,
                )
        elif provider == "google":
            resp = await client.post(
                provider_url,
                headers=headers,
                json=google_body,
            )
        else:
            resp = await client.post(
                f"{provider_url}/chat/completions",
                headers=headers,
                json=provider_body,
            )

    if resp.status_code >= 400:
        raise HTTPException(
            status_code=resp.status_code,
            detail=resp.text,
        )

    # Parse response
    if provider == "anthropic":
        usage_data, content = _parse_anthropic_response(resp)
    elif provider == "google":
        usage_data, content = _parse_google_response(resp)
    else:
        usage_data, content = _parse_openai_response(resp)

    # Calculate cost
    actual_model = routed_model  # Use the actually-used model for pricing
    cost = estimate_cost(
        actual_model,
        usage_data.get("prompt_tokens", 0),
        usage_data.get("completion_tokens", 0),
    )

    # ── LAYER 3: Quality Check ────────────────────────────────────
    quality: Optional[QualityReport] = None
    if app_settings.enable_code_validation and content:
        quality = check_quality(
            content, messages,
            enable_security=app_settings.enable_security_scan,
            enable_performance=app_settings.enable_performance_check,
        )

    # ── LAYER 4: Agent Monitoring ─────────────────────────────────
    if agent_id and session_id:
        from guardian.agent.monitor import record_iteration, is_session_capped
        if is_session_capped(session_id):
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "agent_capped",
                    "message": f"Agent session {session_id} has been capped (too many iterations or timed out)",
                },
            )
        await record_iteration(session_id, usage_data.get("total_tokens", 0), cost)

    # ── Log Usage ─────────────────────────────────────────────────
    warnings = []
    if routing.reason != "none":
        warnings.append(f"Model routed: {model} -> {routed_model} ({routing.reason}, saved ~{routing.estimated_savings_pct}%)")
    if quality and quality.verdict.value != "pass":
        warnings.append(f"Quality check: {quality.verdict.value} (score: {quality.score})")
    if budget.status.value != "ok":
        warnings.append(f"Budget: {budget.status.value} (${budget.monthly_spent:.2f}/${budget.monthly_budget:.2f})")

    async with async_session() as db_session:
        await db_session.execute(
            UsageLog.__table__.insert().values(
                user_id=user_id,
                project_id=project_id,
                agent_id=agent_id,
                model=actual_model,
                provider=provider,
                prompt_tokens=usage_data.get("prompt_tokens", 0),
                completion_tokens=usage_data.get("completion_tokens", 0),
                total_tokens=usage_data.get("total_tokens", 0),
                cost_usd=cost,
                quality_score=quality.score if quality else None,
                warnings=json.dumps(warnings) if warnings else None,
            )
        )
        await db_session.commit()

    # Build response (OpenAI format for compatibility)
    response_body = {
        "id": f"guardian-{req_id}",
        "object": "chat.completion",
        "model": actual_model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": usage_data.get("prompt_tokens", 0),
            "completion_tokens": usage_data.get("completion_tokens", 0),
            "total_tokens": usage_data.get("total_tokens", 0),
        },
        "guardian": {
            "cost_usd": cost,
            "routed": routing.dict() if routing.reason != "none" else None,
            "quality": quality.dict() if quality else None,
            "budget_status": budget.status.value,
            "warnings": warnings,
        },
    }

    return response_body


# ── Format Converters ──────────────────────────────────────────────

def _to_anthropic_format(model, messages, max_tokens, temperature, stream=False):
    """Convert OpenAI message format to Anthropic format."""
    system_msgs = [m for m in messages if m.get("role") == "system"]
    other_msgs = [m for m in messages if m.get("role") != "system"]

    system_text = " ".join(m.get("content", "") for m in system_msgs)

    anthropic_messages = []
    for m in other_msgs:
        role = "assistant" if m.get("role") == "assistant" else "user"
        anthropic_messages.append({"role": role, "content": m.get("content", "")})

    return {
        "model": model.replace("anthropic/", ""),
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system_text or None,
        "messages": anthropic_messages,
    }


def _parse_anthropic_response(resp) -> tuple[dict, str]:
    data = resp.json()
    content_blocks = data.get("content", [])
    text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
    usage = data.get("usage", {})
    return {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
    }, text


def _to_google_format(model, messages, max_tokens, temperature):
    contents = []
    for m in messages:
        role = "model" if m.get("role") == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": m.get("content", "")}]})
    return {
        "contents": contents,
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": temperature,
        },
    }


def _parse_google_response(resp) -> tuple[dict, str]:
    data = resp.json()
    candidates = data.get("candidates", [])
    text = ""
    for c in candidates:
        parts = c.get("content", {}).get("parts", [])
        for p in parts:
            text += p.get("text", "")
    usage = data.get("usageMetadata", {})
    return {
        "prompt_tokens": usage.get("promptTokenCount", 0),
        "completion_tokens": usage.get("candidatesTokenCount", 0),
        "total_tokens": usage.get("totalTokenCount", 0),
    }, text


def _parse_openai_response(resp) -> tuple[dict, str]:
    data = resp.json()
    choices = data.get("choices", [])
    text = ""
    for c in choices:
        text += c.get("message", {}).get("content", "")
    usage = data.get("usage", {})
    return {
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }, text


# ── Model Listing (OpenAI-compatible) ──────────────────────────────

@app.get("/v1/models")
async def list_models():
    """List available models (for client compatibility)."""
    models = []
    seen = set()
    from guardian.cost import tracker as cost_tracker
    for model_name in sorted(cost_tracker.MODEL_PRICING.keys()):
        if model_name not in seen:
            seen.add(model_name)
            models.append({
                "id": model_name,
                "object": "model",
                "owned_by": resolve_provider(model_name),
            })
    return {"object": "list", "data": models}
