"""Waitlist signup for AI Guardian landing page."""
from pydantic import BaseModel, EmailStr
from fastapi import HTTPException
from guardian.models.database import async_session
from sqlalchemy import text
import datetime


class WaitlistSignup(BaseModel):
    email: EmailStr


class WaitlistResponse(BaseModel):
    status: str
    message: str


async def init_waitlist_table():
    """Create waitlist table if it doesn't exist."""
    async with async_session() as session:
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS waitlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        await session.commit()


async def add_to_waitlist(email: str) -> dict:
    """Add email to waitlist. Idempotent — no error if already exists."""
    async with async_session() as session:
        try:
            await session.execute(
                text("INSERT OR IGNORE INTO waitlist (email) VALUES (:email)"),
                {"email": email}
            )
            await session.commit()
            return {"status": "ok", "message": "You're on the list!"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
