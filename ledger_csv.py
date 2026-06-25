"""CSV-backed ledger for Khata.

Columns (same logical schema as the old sheets_sync.py):
  Contact | Amount | Direction | Date | Status | Confidence | Source Message | Transaction ID

Transaction ID is the upsert key.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime
from typing import Any, Dict, List

import config

HEADERS = [
    "Contact",
    "Amount",
    "Direction",
    "Date",
    "Status",
    "Confidence",
    "Source Message",
    "Transaction ID",
]
ID_COL = "Transaction ID"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _row_from_transaction(t: Dict[str, Any]) -> Dict[str, str]:
    """Convert a transaction dict to an ordered dict of CSV cell values."""
    contact = t.get("contact_name") or str(t.get("contact_id", ""))
    amount = str(t.get("amount", ""))
    direction = t.get("direction", "")
    date_val = t.get("date", "")
    if isinstance(date_val, datetime):
        date_val = date_val.strftime("%Y-%m-%d %H:%M")
    status = t.get("status", "")
    confidence = str(round(float(t.get("confidence", 0)), 4))
    source_message = (t.get("source_message") or "").strip()
    transaction_id = str(t.get("id", ""))
    return {
        "Contact": contact,
        "Amount": amount,
        "Direction": direction,
        "Date": str(date_val),
        "Status": status,
        "Confidence": confidence,
        "Source Message": source_message,
        "Transaction ID": transaction_id,
    }


def _read_all_rows() -> List[Dict[str, str]]:
    """Read all rows from the CSV; return empty list if file doesn't exist."""
    path = config.LEDGER_CSV_PATH
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_all_rows(rows: List[Dict[str, str]]) -> None:
    """Write rows to the CSV, creating or overwriting the file."""
    path = config.LEDGER_CSV_PATH
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def sync_transaction(transaction: dict) -> None:
    """Upsert a single transaction row in the CSV ledger.

    If a row with the same Transaction ID already exists it is overwritten;
    otherwise a new row is appended.
    """
    new_row = _row_from_transaction(transaction)
    tid = new_row[ID_COL]

    rows = _read_all_rows()
    for i, row in enumerate(rows):
        if row.get(ID_COL) == tid:
            rows[i] = new_row
            _write_all_rows(rows)
            return

    # Not found — append
    rows.append(new_row)
    _write_all_rows(rows)


def full_resync(transactions: list[dict]) -> None:
    """Rewrite the entire CSV ledger from a list of transaction dicts."""
    rows = [_row_from_transaction(t) for t in transactions]
    _write_all_rows(rows)
