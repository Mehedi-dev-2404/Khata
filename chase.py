"""
Chase module: find overdue debtors and send WhatsApp reminders via Wassist.

Public API:
    find_overdue_transactions(db) -> list[dict]
    send_reminder(transaction, db) -> bool
    run_chase(db) -> {"reminded": int, "unreachable": int, "errors": int}
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import config
import wassist_client

WASSIST_INACTIVE_STATUS_CODES = {403, 422, 429}  # codes Wassist returns for inactive sessions


# ---------------------------------------------------------------------------
# Draft reminder text via Claude Sonnet
# ---------------------------------------------------------------------------

def _draft_reminder(contact_name: str, amount: float, days_overdue: int) -> str:
    """Generate a short, polite reminder message.

    In MOCK_MODE returns a deterministic string. Real mode uses Sonnet.
    """
    if config.MOCK_MODE:
        return (
            f"Hi {contact_name}, just a friendly reminder that £{amount:.0f} "
            f"has been outstanding for {days_overdue} day(s). "
            f"Please let us know when you can settle up. Thanks!"
        )

    import anthropic

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    prompt = (
        f"Write a short, friendly WhatsApp reminder message for a small business owner "
        f"to send to a customer named {contact_name!r}. "
        f"The customer owes £{amount:.2f} and the invoice has been outstanding for "
        f"{days_overdue} day(s). "
        f"Keep it under 3 sentences, no emojis, plain English. "
        f"Do not include any greeting prefix like 'Here is a message:' — just the message itself."
    )
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=120,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------

def find_overdue_transactions(db) -> list[dict]:
    """Return confirmed, owed-to-business transactions that are overdue and
    haven't been reminded today.

    Filters:
      - status = 'confirmed'
      - direction = 'owed_to_business'
      - date < now() - OVERDUE_DAYS
      - last_reminded_at IS NULL OR last_reminded_at < now() - 1 day
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=config.OVERDUE_DAYS)).isoformat()
    reminded_cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    try:
        result = (
            db.table("transactions")
            .select(
                "id,contact_id,amount,direction,date,source_message,"
                "confidence,status,last_reminded_at"
            )
            .eq("status", "confirmed")
            .eq("direction", "owed_to_business")
            .lt("date", cutoff)
            .execute()
        )
        rows = result.data or []
    except Exception as exc:
        print(f"[chase] DB query failed: {exc}")
        return []

    # Filter last_reminded_at in Python — avoids complex OR in supabase-py
    filtered = []
    for row in rows:
        lra = row.get("last_reminded_at")
        if lra is None or lra < reminded_cutoff:
            filtered.append(row)

    return filtered


def _get_contact(db, contact_id: str) -> Optional[dict]:
    try:
        result = db.table("contacts").select("*").eq("id", contact_id).single().execute()
        return result.data if result.data else None
    except Exception:
        return None


def _mark_reminded(db, transaction_id: str) -> None:
    try:
        db.table("transactions").update(
            {"last_reminded_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", transaction_id).execute()
    except Exception as exc:
        print(f"[chase] Failed to update last_reminded_at for {transaction_id}: {exc}")


# ---------------------------------------------------------------------------
# Reminder sender
# ---------------------------------------------------------------------------

def send_reminder(transaction: dict, db) -> bool:
    """Send a WhatsApp reminder for a single overdue transaction.

    Returns True on success, False on unreachable (no conversation / inactive
    session). Raises on unexpected errors so run_chase can count them.
    """
    contact_id = str(transaction.get("contact_id", ""))
    transaction_id = str(transaction.get("id", ""))
    amount = float(transaction.get("amount", 0))

    # Resolve contact
    contact = _get_contact(db, contact_id) if db else None
    contact_name = (contact.get("name") if contact else None) or "there"
    phone = (contact.get("phone") if contact else None) or ""

    # Days overdue
    date_str = transaction.get("date", "")
    try:
        tx_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        days_overdue = (datetime.now(timezone.utc) - tx_date).days
    except Exception:
        days_overdue = config.OVERDUE_DAYS

    # Get conversation ID
    conv_id = wassist_client.get_conversation_id(phone, db)
    if conv_id is None:
        print(f"[chase] No conversation for {phone} ({contact_name}) — skipping")
        return False

    # Draft message
    try:
        text = _draft_reminder(contact_name, amount, days_overdue)
    except Exception as exc:
        print(f"[chase] Draft failed for {transaction_id}: {exc}")
        raise

    # Send
    try:
        wassist_client.send_message(conv_id, text)
    except Exception as exc:
        # Treat 4xx from Wassist as "unreachable" (inactive session window)
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code in WASSIST_INACTIVE_STATUS_CODES or (
            status_code and 400 <= status_code < 500
        ):
            print(
                f"[chase] Wassist rejected send for {phone} "
                f"(HTTP {status_code}) — contact outside 24h session window"
            )
            return False
        raise

    # Success — update last_reminded_at
    if db:
        _mark_reminded(db, transaction_id)

    print(f"[chase] Reminded {contact_name} ({phone}) — £{amount:.0f} overdue {days_overdue}d")
    return True


# ---------------------------------------------------------------------------
# Full chase pass
# ---------------------------------------------------------------------------

def run_chase(db) -> dict[str, int]:
    """Run a full reminder pass over all overdue transactions.

    Returns:
        {"reminded": int, "unreachable": int, "errors": int}
    """
    overdue = find_overdue_transactions(db)
    print(f"[chase] Found {len(overdue)} overdue transaction(s)")

    reminded = 0
    unreachable = 0
    errors = 0

    for tx in overdue:
        try:
            sent = send_reminder(tx, db)
            if sent:
                reminded += 1
            else:
                unreachable += 1
        except Exception as exc:
            print(f"[chase] Unexpected error on transaction {tx.get('id')}: {exc}")
            errors += 1

    summary = {"reminded": reminded, "unreachable": unreachable, "errors": errors}
    print(f"[chase] Run complete: {summary}")
    return summary
