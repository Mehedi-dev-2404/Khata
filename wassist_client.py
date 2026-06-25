"""
Wassist BYOA client helpers.

send_webhook_response  — build the JSON reply dict for Wassist's webhook response
send_via_callback      — POST a reply to the reply_callback URL Wassist provides
send_message           — proactively send a message to a conversation
get_conversation_id    — resolve phone number → Wassist conversation id
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

import config

WASSIST_BASE = "https://wassist.app/api/v1"

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
    """Proactively send a text message to an active Wassist conversation.

    Raises httpx.HTTPStatusError on non-2xx so callers can catch and classify.
    NOTE: Wassist only allows free-text sends to conversations where the contact
    messaged within the last 24h (WhatsApp session window). Calls to inactive
    conversations will return a 4xx — callers should catch and count as "unreachable".

    In MOCK_MODE prints instead of sending.
    """
    if config.MOCK_MODE:
        print(f"[MOCK] wassist.send_message conv={conversation_id!r}: {text!r}")
        return

    url = f"{WASSIST_BASE}/conversations/{conversation_id}/messages/"
    with httpx.Client(timeout=15) as client:
        resp = client.post(
            url,
            headers=_auth_headers(),
            json={"type": "text", "text": {"body": text}},
        )
        resp.raise_for_status()


def get_conversation_id(phone_number: str, db) -> Optional[str]:
    """Resolve a phone number to a Wassist conversation ID.

    1. Check contacts.wassist_conversation_id (cached).
    2. If null, query Wassist's List Conversations endpoint filtered by phone.
    3. Cache result on the contact row and return it.
    Returns None if no conversation exists yet (contact hasn't messaged us).
    """
    # --- 1. Check cache ---
    if db:
        try:
            result = (
                db.table("contacts")
                .select("id,wassist_conversation_id")
                .eq("phone", phone_number)
                .limit(1)
                .execute()
            )
            if result.data:
                cached = result.data[0].get("wassist_conversation_id")
                if cached:
                    return cached
                contact_id = result.data[0]["id"]
            else:
                contact_id = None
        except Exception as exc:
            print(f"[wassist] DB lookup failed: {exc}")
            contact_id = None
    else:
        contact_id = None

    # --- 2. MOCK_MODE: no real API call ---
    if config.MOCK_MODE:
        print(f"[MOCK] wassist.get_conversation_id for {phone_number} — returning None (no real conversation)")
        return None

    # --- 3. Query Wassist ---
    if not config.WASSIST_API_KEY:
        return None
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                f"{WASSIST_BASE}/conversations/",
                headers=_auth_headers(),
                params={"phone_number": phone_number, "limit": 1},
            )
            resp.raise_for_status()
            data = resp.json()

        conversations = data if isinstance(data, list) else data.get("results") or data.get("conversations") or []
        if not conversations:
            return None

        conv_id = str(conversations[0].get("id") or conversations[0].get("conversation_id") or "")
        if not conv_id:
            return None

        # --- 4. Cache on contact row ---
        if db and contact_id:
            try:
                db.table("contacts").update(
                    {"wassist_conversation_id": conv_id}
                ).eq("id", contact_id).execute()
            except Exception as exc:
                print(f"[wassist] Failed to cache conversation_id: {exc}")

        return conv_id

    except Exception as exc:
        print(f"[wassist] get_conversation_id failed for {phone_number}: {exc}")
        return None
