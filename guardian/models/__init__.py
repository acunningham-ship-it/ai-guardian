"""Core configuration and database setup for AI Guardian."""
import os
from dataclasses import dataclass, field
from typing import Optional

import dotenv

dotenv.load_dotenv()


@dataclass
class Settings:
    port: int = int(os.getenv("GUARDIAN_PORT", "9191"))
    host: str = os.getenv("GUARDIAN_HOST", "0.0.0.0")
    secret: str = os.getenv("GUARDIAN_SECRET", "change-me-in-production")
    database_url: str = os.getenv(
        "DATABASE_URL", "sqlite+aiosqlite:///./guardian.db"
    )

    # Budget defaults
    default_monthly_budget: float = float(os.getenv("DEFAULT_MONTHLY_BUDGET", "100"))
    default_daily_budget: float = float(os.getenv("DEFAULT_DAILY_BUDGET", "10"))
    default_hard_cap: bool = os.getenv("DEFAULT_HARD_CAP", "true").lower() == "true"

    # Model routing
    prefer_cheap_models: bool = (
        os.getenv("PREFER_CHEAP_MODELS", "true").lower() == "true"
    )
    fallback_order: list[str] = field(default_factory=lambda: [
        m.strip()
        for m in os.getenv(
            "FALLBACK_ORDER",
            "anthropic/claude-sonnet-4,openai/gpt-4o-mini,openai/gpt-4o,anthropic/claude-opus-4",
        ).split(",")
    ])

    # Quality guardrails
    enable_code_validation: bool = (
        os.getenv("ENABLE_CODE_VALIDATION", "true").lower() == "true"
    )
    enable_security_scan: bool = (
        os.getenv("ENABLE_SECURITY_SCAN", "true").lower() == "true"
    )
    enable_performance_check: bool = (
        os.getenv("ENABLE_PERFORMANCE_CHECK", "true").lower() == "true"
    )
    max_tokens_per_request: int = int(os.getenv("MAX_TOKENS_PER_REQUEST", "4096"))

    # Agent monitoring
    max_agent_iterations: int = int(os.getenv("MAX_AGENT_ITERATIONS", "20"))
    agent_timeout_seconds: int = int(os.getenv("AGENT_TIMEOUT_SECONDS", "300"))


settings = Settings()
