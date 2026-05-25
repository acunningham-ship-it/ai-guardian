"""Pydantic models for AI Guardian."""
from datetime import datetime
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


# ── Request / Response ──────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str
    content: str


class ProxyRequest(BaseModel):
    """Incoming request to be proxied to an AI provider."""
    model: str = Field(..., description="Model ID, e.g. 'anthropic/claude-sonnet-4'")
    messages: list[ChatMessage]
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    stream: bool = False
    # Guardian-specific metadata
    project_id: Optional[str] = None
    agent_id: Optional[str] = None
    user_id: Optional[str] = Field(..., description="End user / dev identifier")


class ProxyResponse(BaseModel):
    """Unified response from any provider."""
    model: str
    content: str
    usage: dict[str, int]  # prompt_tokens, completion_tokens, total_tokens
    cost_usd: float
    provider: str
    cached: bool = False
    quality_score: Optional[float] = None  # 0-100
    warnings: list[str] = Field(default_factory=list)


# ── Cost / Budget ──────────────────────────────────────────────────

class BudgetStatus(str, Enum):
    OK = "ok"
    WARNING = "warning"
    EXCEEDED = "exceeded"


class BudgetCheck(BaseModel):
    user_id: str
    project_id: Optional[str] = None
    daily_spent: float
    daily_budget: float
    monthly_spent: float
    monthly_budget: float
    status: BudgetStatus
    would_exceed: bool


class UsageRecord(BaseModel):
    id: Optional[int] = None
    user_id: str
    project_id: Optional[str] = None
    agent_id: Optional[str] = None
    model: str
    provider: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    cached: bool = False
    quality_score: Optional[float] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ── Model Routing ──────────────────────────────────────────────────

class RoutingDecision(BaseModel):
    original_model: str
    routed_model: str
    reason: str  # cost, quality, fallback, budget
    estimated_savings_pct: float = 0.0


# ── Quality ────────────────────────────────────────────────────────

class QualityVerdict(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class QualityReport(BaseModel):
    verdict: QualityVerdict
    score: float  # 0-100
    issues: list[dict[str, Any]] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    # Specific checks
    security_issues: list[str] = Field(default_factory=list)
    performance_flags: list[str] = Field(default_factory=list)
    hallucination_risk: Optional[str] = None


# ── Agent Monitoring ───────────────────────────────────────────────

class AgentSession(BaseModel):
    session_id: str
    agent_id: str
    user_id: str
    model: str
    iteration_count: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    started_at: datetime = Field(default_factory=datetime.utcnow)
    last_activity: datetime = Field(default_factory=datetime.utcnow)
    status: str = "active"  # active, capped, timed_out, completed


# ── Dashboard ──────────────────────────────────────────────────────

class DashboardStats(BaseModel):
    total_requests: int
    total_cost_usd: float
    total_tokens: int
    avg_quality_score: float
    active_agents: int
    budget_remaining: float
    top_models: list[dict[str, Any]]
    recent_alerts: list[str]
