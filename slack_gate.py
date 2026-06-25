"""Slack approval gate for flagged Khata transactions.

Flow:
  1. Webhook handler calls notify_slack_flag(transaction, flag_id)
     → posts Block Kit message with Approve/Reject buttons
     → stores Slack message ts on flags row
     → starts a daemon thread that auto-approves after SLACK_APPROVAL_TIMEOUT_MINUTES

  2. Slack sends button click to POST /slack/actions
     → handle_slack_action(payload) resolves the flag, updates transaction
       status, syncs ledger, edits the Slack message to show outcome

MOCK_MODE (no SLACK_BOT_TOKEN set): all Slack API calls are printed to stdout;
no crash, a fake ts "mock_ts_000" is used so the rest of the flow proceeds.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Optional

import httpx

import config
from database import get_client

SLACK_API = "https://slack.com/api"


# ---------------------------------------------------------------------------
# Internal Slack helpers
# ---------------------------------------------------------------------------

def _slack_post(method: str, payload: dict) -> dict:
    """POST to Slack Web API. Falls back to mock print when no token."""
    if not config.SLACK_BOT_TOKEN:
        print(f"[MOCK] Slack {method}:", json.dumps(payload, default=str, indent=2))
        return {"ok": True, "ts": "mock_ts_000", "channel": "mock_channel"}
    resp = httpx.post(
        f"{SLACK_API}/{method}",
        headers={
            "Authorization": f"Bearer {config.SLACK_BOT_TOKEN}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=10,
    )
    data = resp.json()
    if not data.get("ok"):
        print(f"[SlackGate] API error on {method}: {data.get('error')}")
    return data


def _build_flag_blocks(transaction: dict, flag_id: str) -> list:
    contact = transaction.get("contact_name") or str(transaction.get("contact_id", "unknown"))
    amount = float(transaction.get("amount", 0))
    direction = transaction.get("direction", "")
    confidence = float(transaction.get("confidence", 0))
    source = (transaction.get("source_message") or "").strip()
    reasoning = (transaction.get("reasoning") or "").strip()

    dir_label = "owed to us ↑" if direction == "owed_to_business" else "paid out ↓"
    conf_pct = int(confidence * 100)

    text = (
        f":warning: *Flagged transaction — approval required*\n"
        f"• Contact: *{contact}*\n"
        f"• Amount: *£{amount:.2f}* ({dir_label})\n"
        f"• Confidence: *{conf_pct}%*\n"
        f"• Source: _{source or 'n/a'}_"
    )
    if reasoning:
        text += f"\n• Flagged because: _{reasoning}_"

    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": "approve_transaction",
                    "value": flag_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "style": "danger",
                    "action_id": "reject_transaction",
                    "value": flag_id,
                },
            ],
        },
    ]


def _edit_slack_message(channel: str, ts: str, text: str) -> None:
    """Replace an existing Slack message with a plain resolved text."""
    _slack_post("chat.update", {
        "channel": channel,
        "ts": ts,
        "text": text,
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    })


# ---------------------------------------------------------------------------
# Internal database helpers
# ---------------------------------------------------------------------------

def _get_flag(flag_id: str) -> Optional[dict]:
    db = get_client()
    if not db:
        return None
    result = db.table("flags").select("*").eq("id", flag_id).single().execute()
    return result.data if result.data else None


def _resolve_flag(flag_id: str, resolution: str) -> None:
    db = get_client()
    if not db:
        return
    db.table("flags").update({"resolved": True, "resolution": resolution}).eq("id", flag_id).execute()


def _update_transaction_status(transaction_id: str, status: str) -> Optional[dict]:
    """Update transaction status and return the fresh row, or None if no DB."""
    db = get_client()
    if not db:
        return None
    db.table("transactions").update({"status": status}).eq("id", transaction_id).execute()
    result = db.table("transactions").select("*").eq("id", transaction_id).single().execute()
    return result.data if result.data else None


def _sync_ledgers(transaction: dict) -> None:
    """Push the updated transaction to the CSV ledger."""
    try:
        from ledger_csv import sync_transaction as csv_sync
        csv_sync(transaction)
    except Exception as exc:
        print(f"[SlackGate] ledger_csv sync error: {exc}")


# ---------------------------------------------------------------------------
# Auto-approve background thread
# ---------------------------------------------------------------------------

def _auto_approve(flag_id: str, transaction_id: str, channel: str) -> None:
    """Sleeps for the configured timeout then approves if still unresolved."""
    delay_seconds = config.SLACK_APPROVAL_TIMEOUT_MINUTES * 60
    time.sleep(delay_seconds)

    flag = _get_flag(flag_id)
    if not flag or flag.get("resolved"):
        return  # Already handled manually — nothing to do

    updated = _update_transaction_status(transaction_id, "confirmed")
    _resolve_flag(flag_id, "auto_approved")

    ts = flag.get("slack_message_ts")
    if ts and channel:
        _edit_slack_message(
            channel,
            ts,
            f":white_check_mark: Auto-approved after {config.SLACK_APPROVAL_TIMEOUT_MINUTES}m timeout",
        )

    if updated:
        _sync_ledgers(updated)

    print(f"[SlackGate] Auto-approved flag {flag_id} after {config.SLACK_APPROVAL_TIMEOUT_MINUTES}m")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def notify_slack_flag(transaction: dict, flag_id: str) -> None:
    """Post a Slack Block Kit message for a flagged transaction.

    Args:
        transaction: dict matching the Transaction model fields, optionally
                     enriched with ``contact_name`` and ``reasoning`` keys.
        flag_id:     UUID string of the corresponding flags row.

    Side-effects:
        - Posts to Slack (or prints in MOCK_MODE).
        - Stores the Slack message ts on the flags row.
        - Starts a daemon thread for auto-approve after timeout.
    """
    channel = config.SLACK_CHANNEL
    blocks = _build_flag_blocks(transaction, flag_id)

    result = _slack_post("chat.postMessage", {
        "channel": channel,
        "text": "Flagged transaction — approval required",
        "blocks": blocks,
    })

    ts = result.get("ts") or result.get("message", {}).get("ts")
    actual_channel = result.get("channel", channel)

    db = get_client()
    if db and ts:
        db.table("flags").update({"slack_message_ts": ts}).eq("id", flag_id).execute()

    transaction_id = str(transaction.get("id", ""))
    threading.Thread(
        target=_auto_approve,
        args=(flag_id, transaction_id, actual_channel),
        daemon=True,
        name=f"auto-approve-{flag_id[:8]}",
    ).start()

    print(f"[SlackGate] Notified Slack for flag {flag_id} (ts={ts})")


def handle_slack_action(payload: dict) -> None:
    """Process an interactive button click from Slack.

    Expected to be called from POST /slack/actions after parsing the
    URL-encoded ``payload`` field as JSON.

    Handles action_ids:
        approve_transaction  → status "confirmed", syncs ledgers
        reject_transaction   → status "rejected"

    In both cases: marks flag resolved, edits the Slack message to show
    outcome + actor name. Silently ignores unknown action_ids.
    """
    actions = payload.get("actions", [])
    if not actions:
        return

    action = actions[0]
    action_id = action.get("action_id")
    flag_id = action.get("value", "")
    user_name = payload.get("user", {}).get("name", "someone")
    ts = payload.get("message", {}).get("ts")
    channel = payload.get("channel", {}).get("id")

    if action_id not in ("approve_transaction", "reject_transaction"):
        return

    flag = _get_flag(flag_id)
    if not flag:
        print(f"[SlackGate] Flag {flag_id} not found — ignoring action")
        return

    if flag.get("resolved"):
        if channel and ts:
            _edit_slack_message(channel, ts, "_(already resolved — no action taken)_")
        return

    transaction_id = str(flag.get("transaction_id", ""))

    if action_id == "approve_transaction":
        updated = _update_transaction_status(transaction_id, "confirmed")
        _resolve_flag(flag_id, "approved")
        edit_text = f":white_check_mark: Approved by {user_name}"
        if updated:
            _sync_ledgers(updated)
    else:
        updated = _update_transaction_status(transaction_id, "rejected")
        _resolve_flag(flag_id, "rejected")
        edit_text = f":x: Rejected by {user_name}"

    if channel and ts:
        _edit_slack_message(channel, ts, edit_text)

    print(f"[SlackGate] Flag {flag_id} {action_id.split('_')[0]}d by {user_name}")
