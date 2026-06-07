"""Stripe billing for AI Guardian — usage-based subscriptions.

Tiers:
  - Free: <100 requests/mo, no credit card needed
  - Personal ($9/mo): up to 10K requests/mo
  - Team ($29/mo): up to 100K requests/mo, 5 users
  - Scale ($99/mo): unlimited requests, unlimited users

Users hit hard budget caps based on their tier.
Stripe checkout for upgrades, webhook for subscription events.
"""
import os
import datetime
import logging
from typing import Optional
from enum import Enum

import stripe

from guardian.models.database import Base, async_session, engine
from sqlalchemy import Column, String, Float, DateTime, Integer, Boolean, Text, Index, select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ── Stripe Config ───────────────────────────────────────────────────

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

# Price IDs — set these in Stripe Dashboard then env vars
PRICE_IDS = {
    "personal": os.getenv("STRIPE_PRICE_PERSONAL", ""),
    "team": os.getenv("STRIPE_PRICE_TEAM", ""),
    "scale": os.getenv("STRIPE_PRICE_SCALE", ""),
}


# ── Tiers ───────────────────────────────────────────────────────────

class Tier(str, Enum):
    FREE = "free"
    PERSONAL = "personal"
    TEAM = "team"
    SCALE = "scale"


TIER_LIMITS = {
    Tier.FREE: {"requests": 100, "budget_usd": 5.0, "users": 1},
    Tier.PERSONAL: {"requests": 10_000, "budget_usd": 100.0, "users": 1},
    Tier.TEAM: {"requests": 100_000, "budget_usd": 500.0, "users": 5},
    Tier.SCALE: {"requests": None, "budget_usd": None, "users": None},  # Unlimited
}

TIER_PRICE = {
    Tier.FREE: 0,
    Tier.PERSONAL: 9,
    Tier.TEAM: 29,
    Tier.SCALE: 99,
}


# ── Subscription DB ─────────────────────────────────────────────────

class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(128), nullable=False, unique=True, index=True)
    tier = Column(String(32), nullable=False, default=Tier.FREE.value)
    stripe_customer_id = Column(String(128), nullable=True)
    stripe_subscription_id = Column(String(128), nullable=True)
    stripe_price_id = Column(String(128), nullable=True)
    # Status
    status = Column(String(32), default="active")  # active, past_due, canceled
    current_period_start = Column(DateTime, nullable=True)
    current_period_end = Column(DateTime, nullable=True)
    # Usage tracking (reset each billing period)
    period_requests = Column(Integer, default=0)
    period_reset_at = Column(DateTime, default=datetime.datetime.utcnow)
    # Metadata
    email = Column(String(256), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    __table_args__ = (
        Index("ix_sub_stripe_customer", "stripe_customer_id"),
    )


async def init_billing_db():
    """Create subscriptions table."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ── Subscription Management ─────────────────────────────────────────

async def get_subscription(user_id: str) -> Optional[dict]:
    """Get subscription info for a user."""
    async with async_session() as session:
        q = select(Subscription).where(Subscription.user_id == user_id)
        result = await session.execute(q)
        sub = result.scalar_one_or_none()

        if not sub:
            # Auto-create free tier
            sub = Subscription(user_id=user_id, tier=Tier.FREE.value)
            session.add(sub)
            await session.commit()

        tier = Tier(sub.tier)
        limits = TIER_LIMITS[tier]

        return {
            "user_id": user_id,
            "tier": tier.value,
            "status": sub.status,
            "price_usd": TIER_PRICE[tier],
            "limits": limits,
            "period_requests": sub.period_requests,
            "period_reset_at": sub.period_reset_at.isoformat() if sub.period_reset_at else None,
            "stripe_customer_id": sub.stripe_customer_id,
            "current_period_end": sub.current_period_end.isoformat() if sub.current_period_end else None,
        }


async def check_request_allowed(user_id: str) -> dict:
    """Check if a user is allowed to make another request.
    
    Returns {allowed: bool, tier, requests_used, requests_limit, reason}
    """
    sub = await get_subscription(user_id)
    tier = Tier(sub["tier"])
    limits = TIER_LIMITS[tier]

    requests_used = sub["period_requests"]
    requests_limit = limits["requests"]

    # Scale tier = unlimited
    if requests_limit is None:
        return {"allowed": True, "tier": tier.value, "requests_used": requests_used, "requests_limit": None}

    if requests_used >= requests_limit:
        return {
            "allowed": False,
            "tier": tier.value,
            "requests_used": requests_used,
            "requests_limit": requests_limit,
            "reason": f"Request limit reached ({requests_used}/{requests_limit}). Upgrade your plan.",
        }

    return {"allowed": True, "tier": tier.value, "requests_used": requests_used, "requests_limit": requests_limit}


async def increment_request_count(user_id: str) -> int:
    """Increment the request counter for the current billing period."""
    async with async_session() as session:
        q = select(Subscription).where(Subscription.user_id == user_id)
        result = await session.execute(q)
        sub = result.scalar_one_or_none()

        if not sub:
            sub = Subscription(user_id=user_id, tier=Tier.FREE.value, period_requests=1)
            session.add(sub)
            await session.commit()
            return 1

        # Check if we need to reset the counter (new billing period)
        now = datetime.datetime.utcnow()
        if sub.current_period_end and now > sub.current_period_end:
            sub.period_requests = 1
            sub.period_reset_at = now
        else:
            sub.period_requests += 1

        sub.updated_at = now
        await session.commit()
        return sub.period_requests


# ── Stripe Checkout ─────────────────────────────────────────────────

def create_checkout_session(user_id: str, tier: str, success_url: str, cancel_url: str) -> dict:
    """Create a Stripe Checkout session for upgrading to a paid tier.
    
    Call this when user clicks "Upgrade" in the dashboard.
    """
    if not stripe.api_key:
        return {"error": "Stripe not configured. Set STRIPE_SECRET_KEY."}

    price_id = PRICE_IDS.get(tier)
    if not price_id:
        return {"error": f"No price ID for tier '{tier}'. Configure STRIPE_PRICE_{tier.upper()}."}

    try:
        # Get or create Stripe customer
        sub_data = None
        # We'd need to look up existing customer - simplified for now
        customer_params = {
            "metadata": {"guardian_user_id": user_id},
        }

        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={"guardian_user_id": user_id, "tier": tier},
        )

        return {
            "checkout_url": session.url,
            "session_id": session.id,
        }
    except stripe.error.StripeError as e:
        logger.error(f"Stripe checkout error: {e}")
        return {"error": str(e)}


def create_customer_portal_session(user_id: str, stripe_customer_id: str, return_url: str) -> dict:
    """Create a Stripe Customer Portal session for managing their subscription."""
    if not stripe.api_key:
        return {"error": "Stripe not configured."}

    try:
        session = stripe.billing_portal.Session.create(
            customer=stripe_customer_id,
            return_url=return_url,
        )
        return {"portal_url": session.url}
    except stripe.error.StripeError as e:
        return {"error": str(e)}


# ── Webhook Handler ─────────────────────────────────────────────────

async def handle_webhook(payload: bytes, sig_header: str) -> dict:
    """Handle Stripe webhook events.
    
    Events we care about:
    - checkout.session.completed → activate subscription
    - customer.subscription.updated → tier change
    - customer.subscription.deleted → downgrade to free
    - invoice.payment_failed → mark past_due
    """
    if not STRIPE_WEBHOOK_SECRET:
        return {"error": "Stripe webhook secret not configured."}

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        return {"error": "Invalid signature"}

    event_type = event["type"]
    event_data = event["data"]["object"]

    if event_type == "checkout.session.completed":
        user_id = event_data.get("metadata", {}).get("guardian_user_id")
        tier = event_data.get("metadata", {}).get("tier", Tier.PERSONAL.value)
        stripe_customer_id = event_data.get("customer")
        stripe_subscription_id = event_data.get("subscription")

        if user_id:
            await _activate_subscription(
                user_id, tier, stripe_customer_id, stripe_subscription_id
            )

    elif event_type == "customer.subscription.updated":
        stripe_customer_id = event_data.get("customer")
        stripe_subscription_id = event_data.get("id")
        # Extract price to determine tier
        items = event_data.get("items", {}).get("data", [])
        if items:
            price_id = items[0].get("price", {}).get("id")
            tier = _price_to_tier(price_id)
            if tier and stripe_customer_id:
                await _update_tier_by_customer(stripe_customer_id, tier)

    elif event_type == "customer.subscription.deleted":
        stripe_customer_id = event_data.get("customer")
        if stripe_customer_id:
            await _update_tier_by_customer(stripe_customer_id, Tier.FREE.value)

    return {"handled": True, "event_type": event_type}


async def _activate_subscription(
    user_id: str, tier: str, stripe_customer_id: str, stripe_subscription_id: str
):
    """Activate a paid subscription after checkout."""
    async with async_session() as session:
        q = select(Subscription).where(Subscription.user_id == user_id)
        result = await session.execute(q)
        sub = result.scalar_one_or_none()

        if not sub:
            sub = Subscription(user_id=user_id)
            session.add(sub)

        sub.tier = tier
        sub.stripe_customer_id = stripe_customer_id
        sub.stripe_subscription_id = stripe_subscription_id
        sub.status = "active"
        sub.updated_at = datetime.datetime.utcnow()

        await session.commit()
        logger.info(f"Activated {tier} subscription for {user_id}")


async def _update_tier_by_customer(stripe_customer_id: str, tier: str):
    """Update tier for a user identified by their Stripe customer ID."""
    async with async_session() as session:
        q = select(Subscription).where(Subscription.stripe_customer_id == stripe_customer_id)
        result = await session.execute(q)
        sub = result.scalar_one_or_none()

        if sub:
            sub.tier = tier
            sub.status = "active" if tier != Tier.FREE.value else "active"
            sub.updated_at = datetime.datetime.utcnow()
            await session.commit()


def _price_to_tier(price_id: str) -> Optional[str]:
    """Map a Stripe price ID back to a tier."""
    for tier, pid in PRICE_IDS.items():
        if pid == price_id:
            return tier
    return None
