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
from guardian.cost.smart_tokens import compute_smart_max_tokens
from guardian.cost.savings import record_savings, get_savings_summary, init_savings_db
from guardian.cache.semantic import (
    compute_cache_key, get_cached_response, store_cached_response,
    get_cache_stats, init_cache_db,
)
from guardian.billing.stripe_ import (
    check_request_allowed, increment_request_count, get_subscription,
    create_checkout_session, create_customer_portal_session,
    handle_webhook, init_billing_db, Tier, PRICE_IDS,
)
from guardian.models import settings as app_settings
from guardian.models.database import init_db, async_session, UsageLog, get_spent_since
from guardian.models.schemas import (
    ChatMessage, ProxyRequest, ProxyResponse, QualityReport,
)
from pydantic import BaseModel
from guardian.quality.checker import check_quality
from guardian.api.users import create_user, get_user_key, list_user_keys, delete_user_key, update_budget
from guardian.api.alerts import check_and_alert

app = FastAPI(title="AI Guardian", version="0.1.0")

# ── Provider Base URLs ─────────────────────────────────────────────

PROVIDER_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "deepseek": "https://api.deepseek.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}

# Map model prefixes to providers
MODEL_PROVIDER_PREFIXES = {
    "openai/": "openai",
    "anthropic/": "anthropic",
    "google/": "google",
    "deepseek/": "deepseek",
    "openrouter/": "openrouter",
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
    await init_cache_db()
    await init_savings_db()
    await init_billing_db()
    from guardian.api.waitlist import init_waitlist_table
    await init_waitlist_table()


# ── Health Check ───────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "ai-guardian", "version": "0.1.0"}


# ── Waitlist ───────────────────────────────────────────────────────

from guardian.api.waitlist import WaitlistSignup, add_to_waitlist

@app.post("/api/v1/waitlist")
async def join_waitlist(signup: WaitlistSignup):
    result = await add_to_waitlist(signup.email)
    return result


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


# ── User Management API ────────────────────────────────────────────

class RegisterUserRequest(BaseModel):
    user_id: str
    provider: str  # openai, anthropic, google, deepseek
    api_key: str
    monthly_budget: Optional[float] = None
    daily_budget: Optional[float] = None
    webhook_url: Optional[str] = None


class UpdateBudgetRequest(BaseModel):
    monthly_budget: Optional[float] = None
    daily_budget: Optional[float] = None
    hard_cap: Optional[bool] = None
    alert_at_pct: Optional[float] = None
    webhook_url: Optional[str] = None


@app.post("/guardian/users")
async def register_user(req: RegisterUserRequest):
    """Register a user with their provider API key (encrypted storage)."""
    result = await create_user(
        user_id=req.user_id,
        provider=req.provider,
        api_key=req.api_key,
        monthly_budget=req.monthly_budget,
        daily_budget=req.daily_budget,
    )
    return result


@app.get("/guardian/users/{user_id}/keys")
async def get_user_keys(user_id: str):
    """List which providers a user has stored keys for."""
    keys = await list_user_keys(user_id)
    return {"user_id": user_id, "providers": keys}


@app.delete("/guardian/users/{user_id}/keys/{provider}")
async def remove_user_key(user_id: str, provider: str):
    """Delete a stored API key."""
    deleted = await delete_user_key(user_id, provider)
    if deleted:
        return {"status": "deleted", "user_id": user_id, "provider": provider}
    raise HTTPException(status_code=404, detail="Key not found")


@app.put("/guardian/users/{user_id}/budget")
async def set_budget(user_id: str, req: UpdateBudgetRequest):
    """Update budget configuration for a user."""
    result = await update_budget(
        user_id=user_id,
        monthly_budget=req.monthly_budget,
        daily_budget=req.daily_budget,
        hard_cap=req.hard_cap,
        alert_at_pct=req.alert_at_pct,
    )
    return result


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
# ── Proxy: Chat Completions ────────────────────────────────────────

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    Main proxy endpoint — compatible with OpenAI Chat Completions API.
    Supports both streaming (SSE) and non-streaming responses.
    
    v2 features: semantic cache, smart max_tokens, savings tracking.
    """
    body = await request.json()
    req_id = request.state.request_id

    model = body.get("model", "openai/gpt-4o")
    messages = body.get("messages", [])
    max_tokens = body.get("max_tokens", app_settings.max_tokens_per_request)
    temperature = body.get("temperature", 1.0)
    stream = body.get("stream", False)

    user_id = request.headers.get("x-guardian-user", "default")
    project_id = request.headers.get("x-guardian-project")
    agent_id = request.headers.get("x-guardian-agent")
    session_id = request.headers.get("x-guardian-session")

    provider = resolve_provider(model)

    # ── LAYER 0: Subscription Check ────────────────────────────────
    sub_check = await check_request_allowed(user_id)
    if not sub_check["allowed"]:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "subscription_limit",
                "message": sub_check.get("reason", "Request limit reached. Upgrade your plan."),
                "tier": sub_check["tier"],
                "requests_used": sub_check["requests_used"],
                "requests_limit": sub_check["requests_limit"],
            },
        )

    # ── LAYER 1: Smart Max Tokens ─────────────────────────────────
    # Dynamically set max_tokens based on task type to reduce output token costs
    token_info = compute_smart_max_tokens(messages, max_tokens, app_settings.max_tokens_per_request)
    effective_max_tokens = token_info["max_tokens"]

    # ── LAYER 1: Budget Check ──────────────────────────────────────
    # Estimate cost for budget check using max_tokens from the request
    _est_prompt_tokens = sum(len(m.get("content", "").split()) * 2 for m in messages)
    _est_completion_tokens = effective_max_tokens or app_settings.max_tokens_per_request
    _estimated_cost = estimate_cost(model, _est_prompt_tokens, _est_completion_tokens)

    budget = await check_budget(
        user_id,
        estimated_cost=_estimated_cost,
        project_id=project_id,
    )

    if budget.status.value == "exceeded":
        raise HTTPException(
            status_code=422,
            detail={
                "error": "budget_exceeded",
                "message": f"Budget would be exceeded. Spent ${budget.monthly_spent:.2f} of ${budget.monthly_budget:.2f}. Estimated cost: ${_estimated_cost:.4f}",
                "budget": budget.dict(),
            },
        )

    # ── LAYER 2: Semantic Cache ───────────────────────────────────
    # Check if we've seen this exact request before
    cache_key = compute_cache_key(messages, model, temperature, effective_max_tokens)
    cached = await get_cached_response(cache_key, user_id, max_age_hours=24)

    if cached and not stream:
        # Cache HIT — return immediately, zero API cost
        # Record savings (full cost saved)
        original_cost = estimate_cost(model, cached["prompt_tokens"], cached["completion_tokens"])
        savings = await record_savings(
            user_id=user_id,
            original_cost=original_cost,
            actual_cost=0.0,  # Cache hit = free
            original_model=model,
            routed_model=model,
            cache_hit=True,
            task_type=token_info.get("task_type"),
            project_id=project_id,
        )

        # Log the cache hit
        async with async_session() as db_session:
            log_entry = UsageLog(
                user_id=user_id, project_id=project_id, agent_id=agent_id,
                model=model, provider=provider,
                prompt_tokens=cached["prompt_tokens"],
                completion_tokens=cached["completion_tokens"],
                total_tokens=cached["total_tokens"],
                cost_usd=0.0,
                cached=True,
                warnings=json.dumps(["cache_hit"]) ,
            )
            db_session.add(log_entry)
            await db_session.commit()

        return {
            "id": f"guardian-{req_id}",
            "object": "chat.completion",
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": cached["content"]},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": cached["prompt_tokens"],
                "completion_tokens": cached["completion_tokens"],
                "total_tokens": cached["total_tokens"],
            },
            "guardian": {
                "cost_usd": 0.0,
                "cache_hit": True,
                "savings": savings,
                "task_type": token_info.get("task_type"),
                "smart_max_tokens": token_info,
                "budget_status": budget.status.value,
            },
        }

    # ── LAYER 3: Model Routing ────────────────────────────────────
    budget_remaining = max(0, budget.monthly_budget - budget.monthly_spent)
    routing = route_model(
        model, messages,
        budget_remaining=budget_remaining,
        prefer_cheap=app_settings.prefer_cheap_models,
    )
    routed_model = routing.routed_model

    # Calculate the "would have cost" for savings tracking
    original_cost_estimate = estimate_cost(model, _est_prompt_tokens, _est_completion_tokens)

    # ── Get API Key ────────────────────────────────────────────────
    api_key = get_api_key(provider, request)
    if not api_key:
        # Try to get stored key for this user
        stored_key = await get_user_key(user_id, provider)
        if stored_key:
            api_key = stored_key
        else:
            raise HTTPException(
                status_code=401,
                detail=f"No API key for {provider}. Pass via Authorization header or register with /guardian/users",
            )

    # ── Forward to Provider ───────────────────────────────────────
    # Increment request count AFTER subscription check, BEFORE forwarding
    await increment_request_count(user_id)

    if stream:
        return await _streaming_proxy(
            req_id, provider, routed_model, api_key, messages,
            effective_max_tokens, temperature, user_id, project_id, agent_id,
            session_id, model, routing, budget, cache_key, token_info,
            original_cost_estimate,
        )
    else:
        return await _non_streaming_proxy(
            req_id, provider, routed_model, api_key, messages,
            effective_max_tokens, temperature, user_id, project_id, agent_id,
            session_id, model, routing, budget, cache_key, token_info,
            original_cost_estimate,
        )


async def _non_streaming_proxy(
    req_id, provider, routed_model, api_key, messages,
    max_tokens, temperature, user_id, project_id, agent_id,
    session_id, original_model, routing, budget, cache_key, token_info,
    original_cost_estimate,
):
    """Handle non-streaming requests."""
    provider_url, headers, request_body = _build_provider_request(
        provider, routed_model, api_key, messages, max_tokens, temperature, stream=False,
    )

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(provider_url, headers=headers, json=request_body)

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    usage_data, content = _parse_provider_response(provider, resp)

    # Store in cache for future hits
    cost = estimate_cost(
        routed_model,
        usage_data.get("prompt_tokens", 0),
        usage_data.get("completion_tokens", 0),
    )
    await store_cached_response(
        cache_key=cache_key,
        user_id=user_id,
        model=routed_model,
        request_model=original_model,
        response_content=content,
        prompt_tokens=usage_data.get("prompt_tokens", 0),
        completion_tokens=usage_data.get("completion_tokens", 0),
        cost_usd=cost,
        project_id=project_id,
    )

    # Record savings
    savings = await record_savings(
        user_id=user_id,
        original_cost=original_cost_estimate,
        actual_cost=cost,
        original_model=original_model,
        routed_model=routed_model,
        cache_hit=False,
        task_type=token_info.get("task_type"),
        project_id=project_id,
        token_savings_usd=0.0,  # Token savings tracked separately if needed
    )

    return await _build_response(
        req_id, provider, routed_model, original_model, routing, budget,
        usage_data, content, messages, user_id, project_id, agent_id, session_id,
        cache_key, token_info, savings,
    )


async def _streaming_proxy(
    req_id, provider, routed_model, api_key, messages,
    max_tokens, temperature, user_id, project_id, agent_id,
    session_id, original_model, routing, budget, cache_key, token_info,
    original_cost_estimate,
):
    """
    Handle streaming (SSE) requests.
    Passes through the stream in real-time while buffering for post-hoc analysis.
    """
    provider_url, headers, request_body = _build_provider_request(
        provider, routed_model, api_key, messages, max_tokens, temperature, stream=True,
    )

    async def generate():
        """Stream chunks to client, buffer for post-hoc analysis."""
        full_content = ""
        usage_data = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", provider_url, headers=headers, json=request_body) as resp:
                if resp.status_code >= 400:
                    error_body = await resp.aread()
                    yield f"data: {json.dumps({'error': error_body.decode()})}\n\n"
                    return

                async for line in resp.aiter_lines():
                    if not line:
                        continue

                    # Pass through the SSE line to the client
                    yield f"{line}\n\n"

                    # Parse and buffer
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                            # Extract content from chunk
                            if provider == "anthropic":
                                delta = chunk.get("delta", {})
                                if delta.get("type") == "text_delta":
                                    full_content += delta.get("text", "")
                                # Anthropic sends usage in message_start
                                if chunk.get("type") == "message_start":
                                    msg_usage = chunk.get("message", {}).get("usage", {})
                                    usage_data["prompt_tokens"] = msg_usage.get("input_tokens", 0)
                                if chunk.get("type") == "message_delta":
                                    msg_usage = chunk.get("usage", {})
                                    usage_data["completion_tokens"] = msg_usage.get("output_tokens", 0)
                            else:
                                # OpenAI format
                                choices = chunk.get("choices", [])
                                for c in choices:
                                    delta = c.get("delta", {})
                                    full_content += delta.get("content", "")
                                # Usage is typically in the last chunk or separate
                                if chunk.get("usage"):
                                    usage_data = chunk["usage"]
                        except json.JSONDecodeError:
                            pass

        # Post-stream: apply guardrails and log
        # Calculate cost
        cost = estimate_cost(
            routed_model,
            usage_data.get("prompt_tokens", 0),
            usage_data.get("completion_tokens", 0),
        )

        # Quality check
        quality = None
        if app_settings.enable_code_validation and full_content:
            quality = check_quality(
                full_content, messages,
                enable_security=app_settings.enable_security_scan,
                enable_performance=app_settings.enable_performance_check,
            )

        # Agent monitoring
        if agent_id and session_id:
            await record_iteration(
                session_id,
                usage_data.get("total_tokens", 0),
                cost,
            )

        # Log usage
        warnings_list = []
        if routing.reason != "none":
            warnings_list.append(f"Model routed: {original_model} -> {routed_model} ({routing.reason})")
        if quality and quality.verdict.value != "pass":
            warnings_list.append(f"Quality: {quality.verdict.value} ({quality.score})")
        if budget.status.value != "ok":
            warnings_list.append(f"Budget: {budget.status.status}")

        # Send alerts
        webhook_url = None  # Could be retrieved from user config
        await check_and_alert(
            user_id=user_id,
            budget_status=budget.status,
            daily_spent=budget.daily_spent,
            daily_budget=budget.daily_budget,
            monthly_spent=budget.monthly_spent,
            monthly_budget=budget.monthly_budget,
            webhook_url=webhook_url,
            quality_score=quality.score if quality else None,
        )

        async with async_session() as db_session:
            log_entry = UsageLog(
                user_id=user_id,
                project_id=project_id,
                agent_id=agent_id,
                model=routed_model,
                provider=provider,
                prompt_tokens=usage_data.get("prompt_tokens", 0),
                completion_tokens=usage_data.get("completion_tokens", 0),
                total_tokens=usage_data.get("total_tokens", 0),
                cost_usd=cost,
                quality_score=quality.score if quality else None,
                warnings=json.dumps(warnings_list) if warnings_list else None,
            )
            db_session.add(log_entry)
            await db_session.commit()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "X-Guardian-Request-Id": req_id,
            "Cache-Control": "no-cache",
        },
    )


def _build_provider_request(provider, routed_model, api_key, messages, max_tokens, temperature, stream):
    """Build the appropriate request for each provider."""
    base_url = PROVIDER_URLS[provider]
    headers = {"Content-Type": "application/json"}

    if provider == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        body = _to_anthropic_format(routed_model, messages, max_tokens, temperature, stream)
        return f"{base_url}/messages", headers, body

    elif provider == "google":
        headers["x-goog-api-key"] = api_key
        body = _to_google_format(routed_model, messages, max_tokens, temperature)
        url = f"{base_url}/models/{routed_model}:generateContent"
        return url, headers, body

    else:
        headers["Authorization"] = f"Bearer {api_key}"
        body = {
            "model": routed_model,
            "messages": messages,
            "max_tokens": min(max_tokens, app_settings.max_tokens_per_request),
            "temperature": temperature,
            "stream": stream,
        }
        return f"{base_url}/chat/completions", headers, body


async def _build_response(
    req_id, provider, routed_model, original_model, routing, budget,
    usage_data, content, messages, user_id, project_id, agent_id, session_id,
    cache_key=None, token_info=None, savings=None,
):
    """Build and return a non-streaming response with all guardrails applied."""
    cost = estimate_cost(
        routed_model,
        usage_data.get("prompt_tokens", 0),
        usage_data.get("completion_tokens", 0),
    )

    # Quality check
    quality = None
    if app_settings.enable_code_validation and content:
        quality = check_quality(
            content, messages,
            enable_security=app_settings.enable_security_scan,
            enable_performance=app_settings.enable_performance_check,
        )

    # Agent monitoring
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

    # Alert dispatch
    webhook_url = None
    agent_capped_flag = False
    if agent_id and session_id:
        from guardian.agent.monitor import is_session_capped as _isc
        agent_capped_flag = _isc(session_id)

    await check_and_alert(
        user_id=user_id,
        budget_status=budget.status,
        daily_spent=budget.daily_spent,
        daily_budget=budget.daily_budget,
        monthly_spent=budget.monthly_spent,
        monthly_budget=budget.monthly_budget,
        webhook_url=webhook_url,
        quality_score=quality.score if quality else None,
        agent_capped=agent_capped_flag,
    )

    # Log usage
    warnings_list = []
    if routing.reason != "none":
        warnings_list.append(f"Model routed: {original_model} -> {routed_model} ({routing.reason}, saved ~{routing.estimated_savings_pct}%)")
    if quality and quality.verdict.value != "pass":
        warnings_list.append(f"Quality check: {quality.verdict.value} (score: {quality.score})")
    if budget.status.value != "ok":
        warnings_list.append(f"Budget: {budget.status.value} (${budget.monthly_spent:.2f}/${budget.monthly_budget:.2f})")

    async with async_session() as db_session:
        log_entry = UsageLog(
            user_id=user_id,
            project_id=project_id,
            agent_id=agent_id,
            model=routed_model,
            provider=provider,
            prompt_tokens=usage_data.get("prompt_tokens", 0),
            completion_tokens=usage_data.get("completion_tokens", 0),
            total_tokens=usage_data.get("total_tokens", 0),
            cost_usd=cost,
            quality_score=quality.score if quality else None,
            warnings=json.dumps(warnings_list) if warnings_list else None,
        )
        db_session.add(log_entry)
        await db_session.commit()

    return {
        "id": f"guardian-{req_id}",
        "object": "chat.completion",
        "model": routed_model,
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
            "warnings": warnings_list,
            "savings": savings,
            "task_type": token_info.get("task_type") if token_info else None,
            "smart_max_tokens": token_info,
        },
    }


# ── Savings API ─────────────────────────────────────────────────────

@app.get("/guardian/savings/{user_id}")
async def get_user_savings(user_id: str, days: int = 30):
    """Get cumulative savings for a user. 

    This is THE number: 'Guardian saved you $X this month.'
    """
    summary = await get_savings_summary(user_id, days=days)
    cache_stats = await get_cache_stats(user_id)
    return {
        "savings": summary,
        "cache": cache_stats,
    }


# ── Landing Page ────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def landing_page():
    """Serve the marketing landing page."""
    import pathlib
    landing = pathlib.Path(__file__).parent.parent / "landing" / "index.html"
    if landing.exists():
        return HTMLResponse(content=landing.read_text())
    return HTMLResponse(content="<h1>AI Guardian</h1><p>Swap your base_url, save 40-80%.</p>")


# ── Billing API ─────────────────────────────────────────────────────

@app.get("/guardian/subscription/{user_id}")
async def get_user_subscription(user_id: str):
    """Get subscription info for a user."""
    return await get_subscription(user_id)


class CheckoutRequest(BaseModel):
    user_id: str
    tier: str  # personal, team, scale
    success_url: str = "https://ai-guardian.dev/dashboard?upgraded=true"
    cancel_url: str = "https://ai-guardian.dev/dashboard?canceled=true"


@app.post("/guardian/checkout")
async def create_checkout(req: CheckoutRequest):
    """Create a Stripe checkout session for upgrading."""
    if req.tier not in [Tier.PERSONAL.value, Tier.TEAM.value, Tier.SCALE.value]:
        raise HTTPException(status_code=400, detail=f"Invalid tier: {req.tier}")
    result = create_checkout_session(req.user_id, req.tier, req.success_url, req.cancel_url)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


class PortalRequest(BaseModel):
    user_id: str
    return_url: str = "https://ai-guardian.dev/dashboard"


@app.post("/guardian/portal")
async def create_portal(req: PortalRequest):
    """Create a Stripe customer portal session."""
    sub = await get_subscription(req.user_id)
    if not sub or not sub.get("stripe_customer_id"):
        raise HTTPException(status_code=400, detail="No Stripe customer ID. Subscribe first.")
    result = create_customer_portal_session(req.user_id, sub["stripe_customer_id"], req.return_url)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/guardian/webhook/stripe")
async def stripe_webhook(request: Request):
    """Handle Stripe webhook events."""
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    result = await handle_webhook(payload, sig)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


def _parse_provider_response(provider: str, resp: httpx.Response) -> tuple[dict, str]:
    """Parse response from any provider into (usage_dict, content_string)."""
    if provider == "anthropic":
        return _parse_anthropic_response(resp)
    elif provider == "google":
        return _parse_google_response(resp)
    else:
        return _parse_openai_response(resp)


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
