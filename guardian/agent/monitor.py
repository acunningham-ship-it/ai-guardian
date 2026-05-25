"""Agent session monitoring — track and control autonomous AI agents."""
import asyncio
import datetime
import uuid
from typing import Optional

from guardian.models.database import AgentSessionRecord, async_session
from guardian.models.schemas import AgentSession
from guardian.models import settings


# ── In-Memory Session Cache ────────────────────────────────────────
_active_sessions: dict[str, AgentSession] = {}
_session_lock = asyncio.Lock()


def create_session(agent_id: str, user_id: str, model: str) -> AgentSession:
    """Create a new agent session."""
    session = AgentSession(
        session_id=str(uuid.uuid4())[:12],
        agent_id=agent_id,
        user_id=user_id,
        model=model,
    )
    _active_sessions[session.session_id] = session
    return session


async def record_iteration(
    session_id: str,
    tokens: int,
    cost: float,
) -> Optional[AgentSession]:
    """Record an agent iteration. Returns session or None if capped."""
    async with _session_lock:
        session = _active_sessions.get(session_id)
        if not session:
            return None

        session.iteration_count += 1
        session.total_tokens += tokens
        session.total_cost_usd += cost
        session.last_activity = datetime.datetime.utcnow()

        # Check iteration cap
        if session.iteration_count >= settings.max_agent_iterations:
            session.status = "capped"
            await _persist_session(session)
            return session

        # Check timeout
        elapsed = (datetime.datetime.utcnow() - session.started_at).total_seconds()
        if elapsed > settings.agent_timeout_seconds:
            session.status = "timed_out"
            await _persist_session(session)
            return session

        return session


async def complete_session(session_id: str) -> None:
    """Mark a session as completed."""
    async with _session_lock:
        session = _active_sessions.get(session_id)
        if session:
            session.status = "completed"
            await _persist_session(session)
            del _active_sessions[session_id]


async def _persist_session(session: AgentSession) -> None:
    """Persist session to database."""
    async with async_session() as db:
        record = AgentSessionRecord(
            session_id=session.session_id,
            agent_id=session.agent_id,
            user_id=session.user_id,
            model=session.model,
            iteration_count=session.iteration_count,
            total_tokens=session.total_tokens,
            total_cost_usd=session.total_cost_usd,
            status=session.status,
            started_at=session.started_at,
            last_activity=session.last_activity,
            ended_at=datetime.datetime.utcnow() if session.status != "active" else None,
        )
        db.add(record)
        await db.commit()


def get_session(session_id: str) -> Optional[AgentSession]:
    return _active_sessions.get(session_id)


def get_active_sessions(user_id: Optional[str] = None) -> list[AgentSession]:
    sessions = list(_active_sessions.values())
    if user_id:
        sessions = [s for s in sessions if s.user_id == user_id]
    return sessions


def is_session_capped(session_id: str) -> bool:
    session = _active_sessions.get(session_id)
    if not session:
        return True  # Unknown session = capped for safety
    return session.status in ("capped", "timed_out", "completed")
