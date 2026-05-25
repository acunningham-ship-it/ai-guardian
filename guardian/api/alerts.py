"""Webhook alerts for budget thresholds and quality issues."""
import asyncio
import json
from datetime import datetime
from typing import Optional

import httpx

from guardian.models.database import async_session, UsageLog
from guardian.models.schemas import BudgetStatus


# ── Alert Types ─────────────────────────────────────

class AlertType:
    BUDGET_WARNING = "budget_warning"
    BUDGET_EXCEEDED = "budget_exceeded"
    QUALITY_FAIL = "quality_fail"
    AGENT_CAPPED = "agent_capped"
    RUNAWAY_DETECTED = "runaway_detected"


# ── Webhook Sender ─────────────────────────────────────────────────

async def send_webhook(url: str, payload: dict) -> bool:
    """Send a webhook notification. Returns True if successful."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json", "User-Agent": "AI-Guardian/0.1"},
            )
            return resp.status_code < 400
    except Exception:
        return False


async def send_slack_alert(webhook_url: str, title: str, message: str, fields: Optional[list] = None):
    """Send a formatted Slack webhook alert."""
    slack_payload = {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"🛡️ {title}"},
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": message}},
        ]
    }
    if fields:
        slack_payload["blocks"].append({
            "type": "section",
            "fields": [{"type": "mrkdwn", "text": f"*{k}:*\n{v}"} for k, v in fields],
        })
    await send_webhook(webhook_url, slack_payload)


# ── Alert Dispatcher ───────────────────────────────────────────────

async def check_and_alert(
    user_id: str,
    budget_status: BudgetStatus,
    daily_spent: float,
    daily_budget: float,
    monthly_spent: float,
    monthly_budget: float,
    webhook_url: Optional[str] = None,
    quality_score: Optional[float] = None,
    agent_capped: bool = False,
):
    """Check thresholds and send alerts if needed."""
    alerts = []

    # Budget alerts
    if budget_status == BudgetStatus.WARNING:
        pct = (monthly_spent / monthly_budget * 100) if monthly_budget else 0
        alerts.append({
            "type": AlertType.BUDGET_WARNING,
            "title": "Budget Warning",
            "message": f"Monthly budget at {pct:.0f}% (${monthly_spent:.2f} / ${monthly_budget:.2f})",
            "fields": [
                ("User", user_id),
                ("Daily", f"${daily_spent:.2f} / ${daily_budget:.2f}"),
                ("Monthly", f"${monthly_spent:.2f} / ${monthly_budget:.2f}"),
            ],
        })
    elif budget_status == BudgetStatus.EXCEEDED:
        alerts.append({
            "type": AlertType.BUDGET_EXCEEDED,
            "title": "Budget Exceeded!",
            "message": f"Monthly budget EXCEEDED (${monthly_spent:.2f} / ${monthly_budget:.2f}). Requests will be blocked.",
            "fields": [
                ("User", user_id),
                ("Daily", f"${daily_spent:.2f} / ${daily_budget:.2f}"),
                ("Monthly", f"${monthly_spent:.2f} / ${monthly_budget:.2f}"),
            ],
        })

    # Quality alerts
    if quality_score is not None and quality_score < 50:
        alerts.append({
            "type": AlertType.QUALITY_FAIL,
            "title": "Low Quality Output Detected",
            "message": f"AI output quality score: {quality_score:.1f}/100. Security or performance issues found.",
            "fields": [("User", user_id), ("Score", f"{quality_score:.1f}/100")],
        })

    # Agent alerts
    if agent_capped:
        alerts.append({
            "type": AlertType.AGENT_CAPPED,
            "title": "Agent Session Capped",
            "message": "An agent was stopped due to iteration limit or timeout. Check for runaway behavior.",
            "fields": [("User", user_id)],
        })

    # Send alerts
    if webhook_url and alerts:
        for alert in alerts:
            payload = {
                "alert_type": alert["type"],
                "timestamp": datetime.utcnow().isoformat(),
                "user_id": user_id,
                "title": alert["title"],
                "message": alert["message"],
                "fields": alert.get("fields", []),
            }
            # Try generic webhook first
            sent = await send_webhook(webhook_url, payload)
            if not sent and "slack.com" in webhook_url:
                # Fall back to Slack format
                await send_slack_alert(
                    webhook_url, alert["title"], alert["message"], alert.get("fields"),
                )

    return alerts
