"""Database layer for AI Guardian using SQLAlchemy + aiosqlite."""
import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Integer, String, Text,
    create_engine,
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from guardian.models import settings

Base = declarative_base()
engine = create_async_engine(settings.database_url, echo=False)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)  # type: ignore[arg-type]


# ── Tables ─────────────────────────────────────────────────────────

class UsageLog(Base):
    __tablename__ = "usage_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(128), nullable=False, index=True)
    project_id = Column(String(128), nullable=True, index=True)
    agent_id = Column(String(128), nullable=True, index=True)
    model = Column(String(128), nullable=False)
    provider = Column(String(64), nullable=False)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    cost_usd = Column(Float, default=0.0)
    cached = Column(Boolean, default=False)
    quality_score = Column(Float, nullable=True)
    warnings = Column(Text, nullable=True)  # JSON list
    request_body = Column(Text, nullable=True)  # truncated for debugging
    response_body = Column(Text, nullable=True)  # truncated
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)


class BudgetConfig(Base):
    __tablename__ = "budget_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(128), nullable=False, unique=True, index=True)
    project_id = Column(String(128), nullable=True, index=True)
    daily_budget = Column(Float, default=10.0)
    monthly_budget = Column(Float, default=100.0)
    hard_cap = Column(Boolean, default=True)
    alert_at_pct = Column(Float, default=80.0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow)


class AgentSessionRecord(Base):
    __tablename__ = "agent_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(128), nullable=False, unique=True, index=True)
    agent_id = Column(String(128), nullable=False, index=True)
    user_id = Column(String(128), nullable=False, index=True)
    model = Column(String(128), nullable=False)
    iteration_count = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    total_cost_usd = Column(Float, default=0.0)
    status = Column(String(32), default="active")
    started_at = Column(DateTime, default=datetime.datetime.utcnow)
    last_activity = Column(DateTime, default=datetime.datetime.utcnow)
    ended_at = Column(DateTime, nullable=True)


class APIKeyRecord(Base):
    """Stores encrypted provider API keys per user."""
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(128), nullable=False, index=True)
    provider = Column(String(64), nullable=False)  # openai, anthropic, etc.
    key_encrypted = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


# ── Helpers ────────────────────────────────────────────────────────

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    async with async_session() as sess:
        yield sess


async def log_usage(session: AsyncSession, **kwargs) -> UsageLog:
    record = UsageLog(**kwargs)
    session.add(record)
    await session.commit()
    return record


async def get_spent_since(
    session: AsyncSession,
    user_id: str,
    since: datetime.datetime,
    project_id: Optional[str] = None,
) -> float:
    from sqlalchemy import func, select
    q = select(func.sum(UsageLog.cost_usd)).where(
        UsageLog.user_id == user_id,
        UsageLog.created_at >= since,
    )
    if project_id:
        q = q.where(UsageLog.project_id == project_id)
    result = await session.execute(q)
    return result.scalar() or 0.0
