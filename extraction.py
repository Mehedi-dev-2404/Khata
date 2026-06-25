"""
Extraction pipeline: raw WhatsApp message → structured transaction dict.
"""

from __future__ import annotations

import json
import re
from typing import Any

import config
import database

EXTRACTION_SCHEMA = """{
  "amount": <float, the monetary amount>,
  "direction": "<owed_to_business|paid_by_business>",
  "contact_name": "<string or null>",
  "confidence": <float 0-1>,
  "reasoning": "<brief explanation>"
}"""

SYSTEM_PROMPT = """\
You are a bookkeeping assistant for a small business. Your job is to read a \
WhatsApp message and determine whether money is owed TO the business \
(direction: "owed_to_business") or was paid BY the business \
(direction: "paid_by_business").

Rules:
- "u owe me", "owes me", "still owes", "hasn't paid" → owed_to_business
- "paid ya", "paid back", "sent you", "transferred", "settled" → paid_by_business
- Emojis like 💸🤑 may accompany payment — use surrounding text for direction
- Extract the first numeric amount mentioned (ignore currency symbols)
- If no clear amount, set amount to 0 and confidence to 0
- If direction is genuinely ambiguous, lower confidence below 0.5
- Respond ONLY with a JSON object matching the schema below — no code fences, \
no extra text

Schema:
""" + EXTRACTION_SCHEMA


def _build_user_prompt(message: str, contact_phone: str, contact_history: list[dict]) -> str:
    history_block = ""
    if contact_history:
        recent = contact_history[-5:]  # last 5 entries for context
        lines = [
            f"  - {h.get('date', '?')}: {h.get('direction', '?')} £{h.get('amount', '?')}"
            for h in recent
        ]
        history_block = "\nRecent transaction history with this contact:\n" + "\n".join(lines)

    return (
        f"Contact phone: {contact_phone}{history_block}\n\n"
        f"WhatsApp message:\n{message}"
    )


def _parse_extraction(raw: str) -> dict[str, Any]:
    """Strip code fences, then parse JSON. Falls back to raw_decode on partial JSON."""
    # Remove ```json ... ``` or ``` ... ``` fences
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # raw_decode fallback — grabs the first complete JSON object
        decoder = json.JSONDecoder()
        try:
            obj, _ = decoder.raw_decode(cleaned)
            return obj
        except json.JSONDecodeError:
            return {"amount": 0.0, "direction": None, "contact_name": None,
                    "confidence": 0.0, "reasoning": "extraction_failed"}


def _mock_response(message: str) -> dict[str, Any]:
    """Deterministic fake response for MOCK_MODE — inspects message naively."""
    lower = message.lower()
    amount_match = re.search(r"[\£\$\€]?\s*(\d+(?:\.\d{1,2})?)", message)
    amount = float(amount_match.group(1)) if amount_match else 0.0

    if any(w in lower for w in ("paid", "sent", "transferred", "settled")):
        direction = "paid_by_business"
        confidence = 0.85
    elif any(w in lower for w in ("owes", "owe", "hasn't paid", "outstanding")):
        direction = "owed_to_business"
        confidence = 0.85
    else:
        direction = "owed_to_business"
        confidence = 0.4

    return {
        "amount": amount,
        "direction": direction,
        "contact_name": None,
        "confidence": confidence,
        "reasoning": "mock_mode",
    }


def _call_claude(message: str, contact_phone: str, contact_history: list[dict]) -> dict[str, Any]:
    import anthropic  # lazy import — only needed when not in mock mode

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    user_prompt = _build_user_prompt(message, contact_phone, contact_history)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw_text = response.content[0].text
    return _parse_extraction(raw_text)


def _log_to_db(message: str, result: dict[str, Any]) -> None:
    db = database.get_client()
    if db is None:
        return
    try:
        db.table("messages_log").insert({
            "raw_message": message,
            "extraction_result": result,
        }).execute()
    except Exception:
        pass  # logging failure must never crash the caller


def extract_transaction(
    message: str,
    contact_phone: str,
    contact_history: list[dict],
) -> dict[str, Any]:
    """
    Extract a transaction from a WhatsApp message.

    Returns a dict with keys:
        amount (float), direction (str), contact_name (str|None),
        confidence (float), reasoning (str)

    Never raises — returns confidence=0 / reasoning='extraction_failed' on error.
    """
    try:
        if config.MOCK_MODE:
            result = _mock_response(message)
        else:
            result = _call_claude(message, contact_phone, contact_history)
    except Exception as exc:
        result = {
            "amount": 0.0,
            "direction": None,
            "contact_name": None,
            "confidence": 0.0,
            "reasoning": f"extraction_failed: {exc}",
        }

    _log_to_db(message, result)
    return result
