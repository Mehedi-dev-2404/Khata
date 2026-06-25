from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import json

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

import config
import database
import ledger_csv
import wassist_client
from extraction import extract_transaction
from slack_gate import handle_slack_action, notify_slack_flag
from digest import send_digest

app = FastAPI(title="Khata", description="WhatsApp-native bookkeeping agent")

# ---------------------------------------------------------------------------
# Wassist inbound payload schema
# ---------------------------------------------------------------------------

class WassistPayload(BaseModel):
    message: str
    image: Optional[str] = None
    phone_number: str
    reply_callback: str


# ---------------------------------------------------------------------------
# DB helpers (inline to avoid a separate db_helpers module)
# ---------------------------------------------------------------------------

def _get_or_create_contact(db, phone: str, name: Optional[str]) -> dict[str, Any]:
    """Return existing contact row or create one; always returns a dict with 'id'."""
    result = db.table("contacts").select("*").eq("phone", phone).limit(1).execute()
    if result.data:
        row = result.data[0]
        # opportunistically update name if we got one and didn't have one
        if name and not row.get("name"):
            db.table("contacts").update({"name": name}).eq("id", row["id"]).execute()
            row["name"] = name
        return row
    new_row = {
        "id": str(uuid.uuid4()),
        "phone": phone,
        "name": name or phone,
        "business_id": "demo",
    }
    db.table("contacts").insert(new_row).execute()
    return new_row


def _get_contact_history(db, contact_id: str) -> list[dict]:
    """Return last 5 transactions for a contact, oldest first."""
    result = (
        db.table("transactions")
        .select("amount,direction,date,status,confidence")
        .eq("contact_id", contact_id)
        .order("date", desc=True)
        .limit(5)
        .execute()
    )
    return list(reversed(result.data or []))


def _insert_transaction(db, contact_id: str, extraction: dict[str, Any],
                        message: str, status: str) -> dict[str, Any]:
    row = {
        "id": str(uuid.uuid4()),
        "contact_id": contact_id,
        "amount": extraction["amount"],
        "direction": extraction.get("direction") or "owed_to_business",
        "date": datetime.now(timezone.utc).isoformat(),
        "source_message": message,
        "confidence": extraction["confidence"],
        "status": status,
    }
    db.table("transactions").insert(row).execute()
    return row


def _insert_flag(db, transaction_id: str, reason: str) -> str:
    flag_id = str(uuid.uuid4())
    db.table("flags").insert({
        "id": flag_id,
        "transaction_id": transaction_id,
        "reason": reason,
        "resolved": False,
    }).execute()
    return flag_id


@app.get("/health")
def health():
    db_ok = database.get_client() is not None
    return {"status": "ok", "db_connected": db_ok}


# ---------------------------------------------------------------------------
# Wassist webhook
# ---------------------------------------------------------------------------

@app.post("/webhook")
async def webhook(payload: WassistPayload):
    """Receive an inbound WhatsApp message from Wassist and process it."""

    db = database.get_client()

    # ---- 1. Resolve contact ---------------------------------------------------
    contact: Optional[dict] = None
    history: list[dict] = []

    if db:
        try:
            contact = _get_or_create_contact(db, payload.phone_number, None)
            history = _get_contact_history(db, contact["id"])
        except Exception as exc:
            print(f"[webhook] DB contact lookup failed: {exc}")

    # ---- 2. Extract transaction -----------------------------------------------
    extraction = extract_transaction(
        message=payload.message,
        contact_phone=payload.phone_number,
        contact_history=history,
    )

    amount: float = extraction.get("amount") or 0.0
    confidence: float = extraction.get("confidence") or 0.0
    direction: str = extraction.get("direction") or "owed_to_business"
    contact_name: Optional[str] = (
        extraction.get("contact_name")
        or (contact.get("name") if contact else None)
        or payload.phone_number
    )

    # ---- 3. Routing: auto-confirm vs flag ------------------------------------
    needs_flag = (
        confidence < config.CONFIDENCE_THRESHOLD
        or amount >= config.REMINDER_THRESHOLD_GBP
    )

    if not needs_flag:
        # --- Happy path: write and confirm ------------------------------------
        status = "confirmed"
        transaction: Optional[dict] = None

        if db and contact:
            try:
                transaction = _insert_transaction(
                    db, contact["id"], extraction, payload.message, status
                )
            except Exception as exc:
                print(f"[webhook] DB transaction insert failed: {exc}")

        if transaction:
            ledger_row = {**transaction, "contact_name": contact_name}
            try:
                ledger_csv.sync_transaction(ledger_row)
            except Exception as exc:
                print(f"[webhook] ledger_csv sync failed: {exc}")

        direction_phrase = (
            f"£{amount:.0f} owed to you from {contact_name}"
            if direction == "owed_to_business"
            else f"£{amount:.0f} paid out to {contact_name}"
        )
        reply = wassist_client.send_webhook_response(
            f"Got it — noted {direction_phrase}. Ledger updated ✓"
        )
        return JSONResponse(content=reply)

    else:
        # --- Flag path: write as flagged, alert Slack, stay silent -----------
        transaction = None
        flag_id: Optional[str] = None

        if db and contact:
            try:
                transaction = _insert_transaction(
                    db, contact["id"], extraction, payload.message, "flagged"
                )
                reason = (
                    "low_confidence" if confidence < config.CONFIDENCE_THRESHOLD
                    else "high_value"
                )
                flag_id = _insert_flag(db, transaction["id"], reason)
            except Exception as exc:
                print(f"[webhook] DB flag insert failed: {exc}")

        if transaction:
            ledger_row = {**transaction, "contact_name": contact_name}
            try:
                ledger_csv.sync_transaction(ledger_row)
            except Exception as exc:
                print(f"[webhook] ledger_csv flagged sync failed: {exc}")

        if transaction and flag_id:
            try:
                notify_slack_flag(
                    transaction={**transaction, "contact_name": contact_name},
                    flag_id=flag_id,
                )
            except Exception as exc:
                print(f"[webhook] Slack notify failed: {exc}")

        return JSONResponse(content=wassist_client.silent_response())


# ---------------------------------------------------------------------------
# Slack interactive actions (button clicks on flagged-transaction messages)
# ---------------------------------------------------------------------------

@app.post("/slack/actions")
async def slack_actions(request: Request):
    """Receive Slack interactive component payloads (approve/reject buttons).

    Slack posts application/x-www-form-urlencoded with a single ``payload``
    field containing a JSON string. Requires python-multipart to be installed.
    """
    form = await request.form()
    raw = form.get("payload", "")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return Response(status_code=400, content="Invalid payload")

    try:
        handle_slack_action(payload)
    except Exception as exc:
        print(f"[/slack/actions] Error handling action: {exc}")

    # Slack expects a 200 quickly; return empty body to clear the spinner
    return Response(status_code=200)


# ---------------------------------------------------------------------------
# Chase / reminder trigger
# ---------------------------------------------------------------------------

@app.post("/chase/run")
def chase_run():
    """Manually trigger an overdue-reminder pass. No auth — demo only."""
    import chase
    db = database.get_client()
    summary = chase.run_chase(db)
    return summary


# ---------------------------------------------------------------------------
# Digest — manual trigger
# ---------------------------------------------------------------------------

@app.post("/digest/run")
def digest_run():
    """Manually trigger the end-of-day digest.

    Builds a summary of today's activity, formats it via Claude Sonnet, and
    pushes it to Slack (and WhatsApp if OWNER_PHONE_NUMBER is set).
    """
    db = database.get_client()
    result = send_digest(db)
    return JSONResponse(content=result)


@app.get("/ledger", response_class=HTMLResponse)
def ledger_view():
    from ledger_csv import HEADERS, _read_all_rows
    rows = _read_all_rows()

    direction_badge = {
        "owed_to_business": ('<span style="color:#1a7f37;font-weight:600">'
                             '&#8593; owed to us</span>'),
        "paid_by_business": ('<span style="color:#cf222e;font-weight:600">'
                             '&#8595; paid out</span>'),
    }

    header_cells = "".join(f"<th>{h}</th>" for h in HEADERS)

    body_rows = ""
    for row in rows:
        cells = ""
        for h in HEADERS:
            val = row.get(h, "")
            if h == "Direction":
                val = direction_badge.get(val, val)
            elif h == "Confidence":
                try:
                    pct = int(float(val) * 100)
                    bar_color = "#1a7f37" if pct >= 70 else "#bf8700"
                    val = (f'<div style="display:flex;align-items:center;gap:6px">'
                           f'<div style="width:60px;background:#eee;border-radius:4px;height:8px">'
                           f'<div style="width:{pct}%;background:{bar_color};'
                           f'border-radius:4px;height:8px"></div></div>'
                           f'<span>{pct}%</span></div>')
                except (ValueError, TypeError):
                    pass
            cells += f"<td>{val}</td>"
        body_rows += f"<tr>{cells}</tr>"

    if not rows:
        body_rows = f'<tr><td colspan="{len(HEADERS)}" style="text-align:center;color:#888;padding:2rem">No transactions yet.</td></tr>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Khata — Ledger</title>
<style>
  body {{font-family:system-ui,sans-serif;margin:0;padding:1.5rem;background:#f6f8fa;color:#1f2328}}
  h1 {{margin:0 0 1rem;font-size:1.4rem}}
  .meta {{font-size:.85rem;color:#656d76;margin-bottom:1rem}}
  table {{border-collapse:collapse;width:100%;background:#fff;border-radius:8px;
          box-shadow:0 1px 3px rgba(0,0,0,.1);overflow:hidden}}
  th {{background:#f0f6ff;padding:.6rem .8rem;text-align:left;font-size:.8rem;
       text-transform:uppercase;letter-spacing:.05em;color:#0969da;border-bottom:1px solid #d0d7de}}
  td {{padding:.55rem .8rem;font-size:.875rem;border-bottom:1px solid #f0f0f0;vertical-align:middle}}
  tr:last-child td {{border-bottom:none}}
  tr:hover td {{background:#f6f8fa}}
</style>
</head>
<body>
<h1>Khata — Ledger</h1>
<div class="meta">{len(rows)} transaction(s) &mdash; <a href="/ledger">refresh</a></div>
<table>
  <thead><tr>{header_cells}</tr></thead>
  <tbody>{body_rows}</tbody>
</table>
</body>
</html>"""
    return HTMLResponse(content=html)
