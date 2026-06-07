"""Savings tracker — measures and reports cost savings from Guardian features.

Every response includes savings headers. Dashboard shows cumulative savings.
"""
import datetime
from typing import Optional

from guardian.models.database import Base, async_session, engine
from sqlalchemy import Column, String, Float, DateTime, Integer, Index, func, select


class SavingsRecord(Base):
    __tablename__ = "savings_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(128), nullable=False, index=True)
    project_id = Column(String(128), nullable=True)
    # What would it have cost without Guardian
    original_cost_usd = Column(Float, default=0.0)
    # What it actually cost
    actual_cost_usd = Column(Float, default=0.0)
    # Savings breakdown
    saved_routing_usd = Column(Float, default=0.0)
    saved_cache_usd = Column(Float, default=0.0)
    saved_tokens_usd = Column(Float, default=0.0)
    # What happened
    original_model = Column(String(128), nullable=True)
    routed_model = Column(String(128), nullable=True)
    cache_hit = Column(Integer, default=0)  # 0 or 1
    task_type = Column(String(32), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_savings_user_date", "user_id", "created_at"),
    )


async def init_savings_db():
    """Create savings table if it doesn't exist."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def record_savings(
    user_id: str,
    original_cost: float,
    actual_cost: float,
    original_model: str,
    routed_model: str,
    cache_hit: bool = False,
    task_type: Optional[str] = None,
    project_id: Optional[str] = None,
    token_savings_usd: float = 0.0,
) -> dict:
    """Record a savings event. Returns the savings summary.
    
    This is called after every request to track:
    - How much the request WOULD have cost (original model, original max_tokens)
    - How much it ACTUALLY cost (routed model, cached, smart max_tokens)
    - Where the savings came from (routing, cache, token optimization)
    """
    # Calculate routing savings
    if cache_hit:
        saved_routing = 0.0
        saved_cache = actual_cost  # Full cost saved when cache hit
        saved_tokens = 0.0
    else:
        # Routing savings = original model cost - routed model cost
        saved_routing = max(0, original_cost - actual_cost - token_savings_usd)
        saved_cache = 0.0
        saved_tokens = token_savings_usd

    total_saved = saved_routing + saved_cache + saved_tokens

    async with async_session() as session:
        record = SavingsRecord(
            user_id=user_id,
            project_id=project_id,
            original_cost_usd=original_cost,
            actual_cost_usd=actual_cost,
            saved_routing_usd=saved_routing,
            saved_cache_usd=saved_cache,
            saved_tokens_usd=saved_tokens,
            original_model=original_model,
            routed_model=routed_model,
            cache_hit=1 if cache_hit else 0,
            task_type=task_type,
        )
        session.add(record)
        await session.commit()

    return {
        "original_cost_usd": round(original_cost, 6),
        "actual_cost_usd": round(actual_cost, 6),
        "total_saved_usd": round(total_saved, 6),
        "saved_routing_usd": round(saved_routing, 6),
        "saved_cache_usd": round(saved_cache, 6),
        "saved_tokens_usd": round(saved_tokens, 6),
        "savings_pct": round((total_saved / original_cost * 100) if original_cost > 0 else 0, 1),
    }


async def get_savings_summary(user_id: str, days: int = 30) -> dict:
    """Get cumulative savings for a user over the last N days.
    
    This is THE dashboard number: "Guardian saved you $X this month."
    """
    since = datetime.datetime.utcnow() - datetime.timedelta(days=days)

    async with async_session() as session:
        q = select(
            func.count(SavingsRecord.id),
            func.sum(SavingsRecord.original_cost_usd),
            func.sum(SavingsRecord.actual_cost_usd),
            func.sum(SavingsRecord.saved_routing_usd),
            func.sum(SavingsRecord.saved_cache_usd),
            func.sum(SavingsRecord.saved_tokens_usd),
            func.sum(SavingsRecord.cache_hit),
        ).where(
            SavingsRecord.user_id == user_id,
            SavingsRecord.created_at >= since,
        )
        result = await session.execute(q)
        row = result.one()

        total_requests = row[0] or 0
        original_total = row[1] or 0.0
        actual_total = row[2] or 0.0
        routing_saved = row[3] or 0.0
        cache_saved = row[4] or 0.0
        tokens_saved = row[5] or 0.0
        cache_hits = row[6] or 0

        total_saved = routing_saved + cache_saved + tokens_saved
        savings_pct = (total_saved / original_total * 100) if original_total > 0 else 0.0

        return {
            "period_days": days,
            "total_requests": total_requests,
            "original_total_usd": round(original_total, 4),
            "actual_total_usd": round(actual_total, 4),
            "total_saved_usd": round(total_saved, 4),
            "savings_pct": round(savings_pct, 1),
            "breakdown": {
                "routing_saved_usd": round(routing_saved, 4),
                "cache_saved_usd": round(cache_saved, 4),
                "tokens_saved_usd": round(tokens_saved, 4),
            },
            "cache_hits": int(cache_hits),
            "cache_hit_rate": round((cache_hits / total_requests * 100) if total_requests > 0 else 0, 1),
        }
