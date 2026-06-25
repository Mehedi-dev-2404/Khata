"""
Wassist BYOA client helpers.

send_webhook_response  — build the JSON reply dict for Wassist's webhook response
send_via_callback      — POST a reply to the reply_callback URL Wassist provides
send_message           — proactively send a message to a conversation (Round 4)
"""

from __future__ import annotations

from typing import Any

import httpx

import config

_HEADERS = {"Content-Type": "application/json"}


def _auth_headers() -> dict[str, str]:
    headers = dict(_HEADERS)
    if config.WASSIST_API_KEY:
        headers["X-API-Key"] = config.WASSIST_API_KEY
    return headers


def send_webhook_response(content: str) -> dict[str, Any]:
    """Return the dict Wassist expects as the webhook HTTP response body."""
    return {"type": "message", "content": content}


def silent_response() -> dict[str, Any]:
    """Return the silent no-reply dict Wassist expects."""
    return {"content": "No CUSTOMER message reply"}


def send_via_callback(reply_callback_url: str, content: str) -> None:
    """POST a reply message to the Wassist reply_callback URL.

    Used when we want to reply outside the synchronous webhook response window.
    Logs errors but does not raise.
    """
    if not reply_callback_url:
        return
    try:
        with httpx.Client(timeout=10) as client:
            client.post(
                reply_callback_url,
                json={"type": "message", "content": content},
                headers=_auth_headers(),
            )
    except Exception as exc:
        print(f"[wassist] send_via_callback failed: {exc}")


def send_message(conversation_id: str, text: str) -> None:
    """Proactively send a message to a Wassist conversation.

    # TODO: Round 4 (reminders) implements the full body.
    Requires WASSIST_AGENT_ID and the correct Wassist outbound endpoint.
    """
    pass
