"""End-of-day digest for Khata.

Public API:
    build_digest_data(db) -> dict
    format_digest_message(data: dict) -> str
    send_digest(db) -> dict

MOCK_MODE (MOCK_MODE=true or missing credentials): skips real API calls,
prints the digest message, returns {"slack_sent": True, "whatsapp_sent": False, ...}.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import config

# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def build_digest_data(db) -> dict:
    """Query Supabase for today's activity summary.

    Returns:
        {
            "transactions_today": int,
            "reminders_today": int,
            "outstanding_gbp": float,
            "unresolved_flags": int,
            "date": "YYYY-MM-DD",
        }
    """
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()

    data: dict[str, Any] = {
        "transactions_today": 0,
        "reminders_today": 0,
        "outstanding_gbp": 0.0,
        "unresolved_flags": 0,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }

    if not db:
        return data

    # Transactions logged today
    try:
        result = (
            db.table("transactions")
            .select("id", count="exact")
            .gte("date", today_start)
            .execute()
        )
        data["transactions_today"] = result.count or 0
    except Exception as exc:
        print(f"[digest] transactions_today query failed: {exc}")

    # Reminders sent today (best-effort — column may not exist yet)
    try:
        result = (
            db.table("transactions")
            .select("id", count="exact")
            .gte("last_reminded_at", today_start)
            .execute()
        )
        data["reminders_today"] = result.count or 0
    except Exception:
        data["reminders_today"] = 0  # column absent — fine, skip silently

    # Outstanding amount: confirmed + owed_to_business
    try:
        result = (
            db.table("transactions")
            .select("amount")
            .eq("status", "confirmed")
            .eq("direction", "owed_to_business")
            .execute()
        )
        rows = result.data or []
        data["outstanding_gbp"] = sum(float(r.get("amount", 0)) for r in rows)
    except Exception as exc:
        print(f"[digest] outstanding_gbp query failed: {exc}")

    # Unresolved flags
    try:
        result = (
            db.table("flags")
            .select("id", count="exact")
            .eq("resolved", False)
            .execute()
        )
        data["unresolved_flags"] = result.count or 0
    except Exception as exc:
        print(f"[digest] unresolved_flags query failed: {exc}")

    return data


# ---------------------------------------------------------------------------
# Message formatting via Claude
# ---------------------------------------------------------------------------

def format_digest_message(data: dict) -> str:
    """Use Claude Sonnet to turn digest data into one friendly paragraph.

    Falls back to a plain template string when ANTHROPIC_API_KEY is absent
    or MOCK_MODE is True.
    """
    def _plain_format() -> str:
        parts = [f"{data['transactions_today']} transaction(s) logged today"]
        if data["reminders_today"]:
            parts.append(f"{data['reminders_today']} reminder(s) sent")
        parts.append(f"£{data['outstanding_gbp']:.2f} outstanding")
        if data["unresolved_flags"]:
            parts.append(f"{data['unresolved_flags']} flagged for review")
        else:
            parts.append("no flags pending")
        return "*Khata daily digest* — " + ", ".join(parts) + "."

    if config.MOCK_MODE or not config.ANTHROPIC_API_KEY:
        return _plain_format()

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        prompt = (
            "You are a friendly bookkeeping assistant. Write ONE short paragraph "
            "(2–3 sentences, no bullet points, conversational tone) summarising "
            "this end-of-day ledger activity for a small business owner:\n\n"
            f"- Transactions logged today: {data['transactions_today']}\n"
            f"- Reminders sent today: {data['reminders_today']}\n"
            f"- Total outstanding (owed to business): £{data['outstanding_gbp']:.2f}\n"
            f"- Unresolved flags needing review: {data['unresolved_flags']}\n\n"
            "Keep it under 60 words. Start with 'Today' or a similar opener."
        )
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as exc:
        print(f"[digest] Claude formatting failed, using plain template: {exc}")
        return _plain_format()


# ---------------------------------------------------------------------------
# Slack post (minimal inline — avoids importing private _slack_post)
# ---------------------------------------------------------------------------

def _post_to_slack(message: str) -> bool:
    """Post message to SLACK_CHANNEL. Returns True on success."""
    if not config.SLACK_BOT_TOKEN:
        print(f"[MOCK] digest → Slack: {message}")
        return True  # treat as sent in mock
    try:
        import httpx
        resp = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {config.SLACK_BOT_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "channel": config.SLACK_CHANNEL,
                "text": message,
                "blocks": [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": message},
                    }
                ],
            },
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            print(f"[digest] Slack post error: {data.get('error')}")
            return False
        return True
    except Exception as exc:
        print(f"[digest] Slack post failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# WhatsApp send (best-effort)
# ---------------------------------------------------------------------------

def _post_to_whatsapp(message: str, db) -> bool:
    """Send digest to OWNER_PHONE_NUMBER via Wassist. Best-effort — never raises."""
    if not config.OWNER_PHONE_NUMBER:
        return False
    try:
        import wassist_client

        # Resolve conversation_id from contacts table by owner phone
        conversation_id: Optional[str] = None
        if db:
            result = (
                db.table("contacts")
                .select("id")
                .eq("phone", config.OWNER_PHONE_NUMBER)
                .limit(1)
                .execute()
            )
            if result.data:
                conversation_id = result.data[0]["id"]

        # Fall back to phone number itself as conversation_id if not in contacts
        conversation_id = conversation_id or config.OWNER_PHONE_NUMBER

        wassist_client.send_message(conversation_id, message)
        return True
    except Exception as exc:
        print(f"[digest] WhatsApp send failed (non-fatal): {exc}")
        return False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def send_digest(db) -> dict:
    """Build, format, and send the end-of-day digest.

    Args:
        db: Supabase client from database.get_client() — may be None.

    Returns:
        {"slack_sent": bool, "whatsapp_sent": bool, "data": dict}
    """
    data = build_digest_data(db)
    message = format_digest_message(data)

    print(f"[digest] Message: {message}")

    if config.MOCK_MODE:
        print("[MOCK] digest → would send to Slack and WhatsApp (skipping real calls)")
        return {"slack_sent": True, "whatsapp_sent": False, "data": data}

    slack_sent = _post_to_slack(message)
    whatsapp_sent = _post_to_whatsapp(message, db)

    return {"slack_sent": slack_sent, "whatsapp_sent": whatsapp_sent, "data": data}
