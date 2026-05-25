"""Smart model routing — picks the best model for cost/quality tradeoff."""
from typing import Optional

from guardian.cost.tracker import MODEL_PRICING, estimate_cost, get_cheaper_alternatives
from guardian.models.schemas import RoutingDecision

# ── Model Capability Tiers ──────────────────────────────────────────
# Higher = more capable but more expensive
MODEL_TIERS: dict[str, int] = {
    # Tier 4: Best reasoning (expensive)
    "anthropic/claude-opus-4": 4,
    "claude-opus-4-20250514": 4,
    "openai/o1": 4,
    "openai/gpt-4-turbo": 4,
    "gpt-4-turbo": 4,
    "o1": 4,
    # Tier 3: Great for most tasks
    "anthropic/claude-sonnet-4": 3,
    "claude-sonnet-4-20250514": 3,
    "openai/gpt-4o": 3,
    "gpt-4o": 3,
    "google/gemini-2.5-pro": 3,
    "gemini-2.5-pro": 3,
    "deepseek/deepseek-r1": 3,
    "deepseek-r1": 3,
    # Tier 2: Good for simple/medium tasks
    "openai/gpt-4o-mini": 2,
    "gpt-4o-mini": 2,
    "openai/o3-mini": 2,
    "o3-mini": 2,
    "google/gemini-2.5-flash": 2,
    "gemini-2.5-flash": 2,
    "deepseek/deepseek-v3": 2,
    "deepseek-v3": 2,
    # Tier 1: Fast and cheap
    "anthropic/claude-haiku-4": 1,
    "claude-haiku-4-20250513": 1,
    "openai/gpt-3.5-turbo": 1,
    "gpt-3.5-turbo": 1,
    "google/gemini-2.0-flash": 1,
    "gemini-2.0-flash": 1,
    # Tier 0: Local (free compute)
    "meta/llama-3.1-70b": 0,
    "meta/llama-3.1-8b": 0,
    "llama-3.1-70b": 0,
    "llama-3.1-8b": 0,
    "qwen/qwen3-235b": 0,
    "qwen3-35b": 0,
    "qwen3-9b": 0,
}


def detect_task_complexity(messages: list[dict]) -> int:
    """
    Heuristic to estimate task complexity from the conversation.
    Returns 0-4 tier recommendation.
    """
    # Combine all message content
    text = " ".join(m.get("content", "") for m in messages).lower()
    token_count = len(text.split())

    # Simple indicators
    simple_keywords = ["hello", "hi ", "thanks", "yes", "no", "ok", "summarize", "translate"]
    complex_keywords = [
        "architect", "design a system", "implement", "refactor", "debug",
        "optimize", "security", "database schema", "api design", "microservice",
        "distributed", "algorithm", "complex", "analyze", "review this code",
    ]

    simple_score = sum(1 for kw in simple_keywords if kw in text)
    complex_score = sum(1 for kw in complex_keywords if kw in text)

    # Code-heavy requests are usually more complex
    has_code = "```" in text or "def " in text or "class " in text or "import " in text

    if complex_score >= 3 or (has_code and token_count > 500):
        return 3  # Needs a strong model
    elif complex_score >= 1 or has_code or token_count > 200:
        return 2  # Mid-tier is fine
    elif simple_score >= 1 or token_count < 50:
        return 1  # Cheap model is fine
    else:
        return 2  # Default to mid-tier


def route_model(
    requested_model: str,
    messages: list[dict],
    budget_remaining: Optional[float] = None,
    prefer_cheap: bool = True,
) -> RoutingDecision:
    """
    Decide which model to actually use based on task complexity,
    cost, and remaining budget.
    """
    requested_model = requested_model.lower()
    task_tier = detect_task_complexity(messages)
    requested_tier = MODEL_TIERS.get(requested_model, 2)

    # If budget is tight, downgrade
    if budget_remaining is not None and budget_remaining < 1.0:
        # Find cheapest model that can handle the task
        alternatives = get_cheaper_alternatives(requested_model)
        for alt in alternatives:
            alt_tier = MODEL_TIERS.get(alt, 2)
            if alt_tier >= task_tier - 1:  # Allow one tier lower
                current_price = MODEL_PRICING.get(requested_model, {}).get("out", 10)
                alt_price = MODEL_PRICING.get(alt, {}).get("out", 10)
                savings = ((current_price - alt_price) / current_price * 100) if current_price else 0
                return RoutingDecision(
                    original_model=requested_model,
                    routed_model=alt,
                    reason="budget",
                    estimated_savings_pct=round(savings, 1),
                )

    # If prefer_cheap and task doesn't need the requested tier, downgrade
    if prefer_cheap and requested_tier > task_tier:
        alternatives = get_cheaper_alternatives(requested_model)
        for alt in alternatives:
            alt_tier = MODEL_TIERS.get(alt, 2)
            if alt_tier >= task_tier:
                current_price = MODEL_PRICING.get(requested_model, {}).get("out", 10)
                alt_price = MODEL_PRICING.get(alt, {}).get("out", 10)
                savings = ((current_price - alt_price) / current_price * 100) if current_price else 0
                return RoutingDecision(
                    original_model=requested_model,
                    routed_model=alt,
                    reason="cost",
                    estimated_savings_pct=round(savings, 1),
                )

    # If task is more complex than requested model can handle, UPGRADE
    if task_tier > requested_tier:
        # Find a better model
        better = [
            (m, t) for m, t in MODEL_TIERS.items()
            if t >= task_tier and m != requested_model
        ]
        if better:
            # Pick the cheapest one that meets the tier
            better.sort(key=lambda x: MODEL_PRICING.get(x[0], {}).get("out", 100))
            upgrade_model = better[0][0]
            return RoutingDecision(
                original_model=requested_model,
                routed_model=upgrade_model,
                reason="quality",
                estimated_savings_pct=0.0,
            )

    # No routing needed
    return RoutingDecision(
        original_model=requested_model,
        routed_model=requested_model,
        reason="none",
        estimated_savings_pct=0.0,
    )
