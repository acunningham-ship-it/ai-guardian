"""Cost tracking, budget enforcement, and token pricing."""
import datetime
import json
from typing import Optional

from guardian.models.database import (
    BudgetConfig, UsageLog, async_session, get_spent_since,
)
from guardian.models.schemas import BudgetCheck, BudgetStatus

# ── Token Pricing (USD per 1M tokens) ──────────────────────────────
# Updated May 2026 — source: official provider pricing pages
MODEL_PRICING: dict[str, dict[str, float]] = {
    # Anthropic
    "anthropic/claude-opus-4":     {"in": 15.00, "out": 75.00},
    "anthropic/claude-sonnet-4":   {"in": 3.00,  "out": 15.00},
    "anthropic/claude-haiku-4":    {"in": 0.25,  "out": 1.25},
    "claude-opus-4-20250514":     {"in": 15.00, "out": 75.00},
    "claude-sonnet-4-20250514":   {"in": 3.00,  "out": 15.00},
    "claude-haiku-4-20250513":    {"in": 0.25,  "out": 1.25},
    # OpenAI
    "openai/gpt-4o":              {"in": 2.50,  "out": 10.00},
    "openai/gpt-4o-mini":         {"in": 0.15,  "out": 0.60},
    "openai/gpt-4-turbo":         {"in": 10.00, "out": 30.00},
    "openai/gpt-3.5-turbo":       {"in": 0.50,  "out": 1.50},
    "openai/o1":                  {"in": 15.00, "out": 60.00},
    "openai/o3-mini":             {"in": 1.10,  "out": 4.40},
    "gpt-4o":                     {"in": 2.50,  "out": 10.00},
    "gpt-4o-mini":                {"in": 0.15,  "out": 0.60},
    "gpt-4-turbo":                {"in": 10.00, "out": 30.00},
    "gpt-3.5-turbo":              {"in": 0.50,  "out": 1.50},
    "o1":                         {"in": 15.00, "out": 60.00},
    "o3-mini":                    {"in": 1.10,  "out": 4.40},
    # Google
    "google/gemini-2.5-pro":      {"in": 1.25,  "out": 10.00},
    "google/gemini-2.5-flash":    {"in": 0.15,  "out": 0.60},
    "google/gemini-2.0-flash":    {"in": 0.10,  "out": 0.40},
    "gemini-2.5-pro":             {"in": 1.25,  "out": 10.00},
    "gemini-2.5-flash":           {"in": 0.15,  "out": 0.60},
    "gemini-2.0-flash":           {"in": 0.10,  "out": 0.40},
    # Meta (via Ollama / local — compute cost only)
    "meta/llama-3.1-70b":         {"in": 0.00,  "out": 0.00},
    "meta/llama-3.1-8b":          {"in": 0.00,  "out": 0.00},
    "llama-3.1-70b":              {"in": 0.00,  "out": 0.00},
    "llama-3.1-8b":               {"in": 0.00,  "out": 0.00},
    # Qwen (via Ollama / local)
    "qwen/qwen3-235b":            {"in": 0.00,  "out": 0.00},
    "qwen3-35b":                  {"in": 0.00,  "out": 0.00},
    "qwen3-9b":                   {"in": 0.00,  "out": 0.00},
    # DeepSeek
    "deepseek/deepseek-v3":       {"in": 0.27,  "out": 1.10},
    "deepseek/deepseek-r1":       {"in": 0.55,  "out": 2.19},
    "deepseek-v3":                {"in": 0.27,  "out": 1.10},
    "deepseek-r1":                {"in": 0.55,  "out": 2.19},
}


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Estimate USD cost for a given model and token counts."""
    pricing = MODEL_PRICING.get(model.lower())
    if not pricing:
        # Default to gpt-4o pricing for unknown models
        pricing = MODEL_PRICING["openai/gpt-4o"]
    in_cost = (prompt_tokens / 1_000_000) * pricing["in"]
    out_cost = (completion_tokens / 1_000_000) * pricing["out"]
    return round(in_cost + out_cost, 6)


def get_cheaper_alternatives(model: str) -> list[str]:
    """Return a list of cheaper models that could handle similar tasks."""
    current_price = MODEL_PRICING.get(model.lower())
    if not current_price:
        return []

    current_avg = (current_price["in"] + current_price["out"]) / 2
    alternatives = []
    for m, p in MODEL_PRICING.items():
        avg = (p["in"] + p["out"]) / 2
        if avg < current_avg and m != model.lower():
            alternatives.append((m, avg))

    alternatives.sort(key=lambda x: x[1])
    return [m for m, _ in alternatives[:5]]


# ── Budget Enforcement ─────────────────────────────────────────────

async def check_budget(
    user_id: str,
    estimated_cost: float = 0.0,
    project_id: Optional[str] = None,
) -> BudgetCheck:
    """Check if a request would exceed the user's budget."""
    async with async_session() as session:
        # Get or create budget config
        from sqlalchemy import select
        q = select(BudgetConfig).where(BudgetConfig.user_id == user_id)
        if project_id:
            q = q.where(BudgetConfig.project_id == project_id)
        result = await session.execute(q)
        config = result.scalar_one_or_none()

        if not config:
            from guardian.models import settings as s
            config = BudgetConfig(
                user_id=user_id,
                project_id=project_id,
                daily_budget=s.default_daily_budget,
                monthly_budget=s.default_monthly_budget,
                hard_cap=s.default_hard_cap,
            )
            session.add(config)
            await session.commit()

        # Calculate spent
        now = datetime.datetime.utcnow()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = day_start.replace(day=1)

        daily_spent = await get_spent_since(session, user_id, day_start, project_id)
        monthly_spent = await get_spent_since(session, user_id, month_start, project_id)

        # Determine status
        daily_pct = (daily_spent / config.daily_budget * 100) if config.daily_budget else 0
        monthly_pct = (monthly_spent / config.monthly_budget * 100) if config.monthly_budget else 0

        would_exceed = (
            daily_spent + estimated_cost > config.daily_budget
            or monthly_spent + estimated_cost > config.monthly_budget
        )

        if daily_pct >= 100 or monthly_pct >= 100:
            status = BudgetStatus.EXCEEDED
        elif daily_pct >= config.alert_at_pct or monthly_pct >= config.alert_at_pct:
            status = BudgetStatus.WARNING
        else:
            status = BudgetStatus.OK

        return BudgetCheck(
            user_id=user_id,
            project_id=project_id,
            daily_spent=round(daily_spent, 4),
            daily_budget=config.daily_budget,
            monthly_spent=round(monthly_spent, 4),
            monthly_budget=config.monthly_budget,
            status=status,
            would_exceed=would_exceed,
        )
