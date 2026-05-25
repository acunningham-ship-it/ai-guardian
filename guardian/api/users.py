"""User management, API key storage, and authentication."""
import hashlib
import hmac
import os
import secrets
import base64
from datetime import datetime, timedelta
from typing import Optional

from cryptography.fernet import Fernet
from sqlalchemy import select

from guardian.models.database import async_session, APIKeyRecord, BudgetConfig
from guardian.models import settings


# ── Encryption ─────────────────────────────────────────────────────

_fernet_key = hashlib.sha256(settings.secret.encode()).digest()
_fernet = Fernet(base64.urlsafe_b64encode(_fernet_key))


def encrypt_key(plaintext: str) -> str:
    """Encrypt an API key for storage."""
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_key(ciphertext: str) -> str:
    """Decrypt a stored API key."""
    return _fernet.decrypt(ciphertext.encode()).decode()


# ── User / Key Management ──────────────────────────────────────────

async def create_user(
    user_id: str,
    provider: str,
    api_key: str,
    monthly_budget: Optional[float] = None,
    daily_budget: Optional[float] = None,
) -> dict:
    """
    Register a user with their provider API key.
    The key is encrypted before storage.
    """
    async with async_session() as session:
        # Check if user already has a key for this provider
        q = select(APIKeyRecord).where(
            APIKeyRecord.user_id == user_id,
            APIKeyRecord.provider == provider,
        )
        result = await session.execute(q)
        existing = result.scalar_one_or_none()

        encrypted = encrypt_key(api_key)

        if existing:
            existing.key_encrypted = encrypted
        else:
            session.add(APIKeyRecord(
                user_id=user_id,
                provider=provider,
                key_encrypted=encrypted,
            ))

        # Set up budget config
        q2 = select(BudgetConfig).where(BudgetConfig.user_id == user_id)
        result2 = await session.execute(q2)
        budget = result2.scalar_one_or_none()

        if not budget:
            session.add(BudgetConfig(
                user_id=user_id,
                monthly_budget=monthly_budget or settings.default_monthly_budget,
                daily_budget=daily_budget or settings.default_daily_budget,
                hard_cap=settings.default_hard_cap,
            ))
        else:
            if monthly_budget:
                budget.monthly_budget = monthly_budget
            if daily_budget:
                budget.daily_budget = daily_budget

        await session.commit()

    return {
        "user_id": user_id,
        "provider": provider,
        "status": "created",
        "monthly_budget": monthly_budget or settings.default_monthly_budget,
        "daily_budget": daily_budget or settings.default_daily_budget,
    }


async def get_user_key(user_id: str, provider: str) -> Optional[str]:
    """Get decrypted API key for a user + provider."""
    async with async_session() as session:
        q = select(APIKeyRecord).where(
            APIKeyRecord.user_id == user_id,
            APIKeyRecord.provider == provider,
        )
        result = await session.execute(q)
        record = result.scalar_one_or_none()
        if record:
            return decrypt_key(record.key_encrypted)
    return None


async def list_user_keys(user_id: str) -> list[dict]:
    """List which providers a user has keys for (not the keys themselves)."""
    async with async_session() as session:
        q = select(APIKeyRecord).where(APIKeyRecord.user_id == user_id)
        result = await session.execute(q)
        records = result.scalars().all()
        return [
            {"provider": r.provider, "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in records
        ]


async def delete_user_key(user_id: str, provider: str) -> bool:
    """Delete a stored API key."""
    async with async_session() as session:
        q = select(APIKeyRecord).where(
            APIKeyRecord.user_id == user_id,
            APIKeyRecord.provider == provider,
        )
        result = await session.execute(q)
        record = result.scalar_one_or_none()
        if record:
            await session.delete(record)
            await session.commit()
            return True
    return False


async def update_budget(
    user_id: str,
    monthly_budget: Optional[float] = None,
    daily_budget: Optional[float] = None,
    hard_cap: Optional[bool] = None,
    alert_at_pct: Optional[float] = None,
) -> dict:
    """Update budget configuration for a user."""
    async with async_session() as session:
        q = select(BudgetConfig).where(BudgetConfig.user_id == user_id)
        result = await session.execute(q)
        config = result.scalar_one_or_none()

        if not config:
            config = BudgetConfig(user_id=user_id)
            session.add(config)

        if monthly_budget is not None:
            config.monthly_budget = monthly_budget
        if daily_budget is not None:
            config.daily_budget = daily_budget
        if hard_cap is not None:
            config.hard_cap = hard_cap
        if alert_at_pct is not None:
            config.alert_at_pct = alert_at_pct

        config.updated_at = datetime.utcnow()
        await session.commit()

        return {
            "user_id": user_id,
            "monthly_budget": config.monthly_budget,
            "daily_budget": config.daily_budget,
            "hard_cap": config.hard_cap,
            "alert_at_pct": config.alert_at_pct,
        }
