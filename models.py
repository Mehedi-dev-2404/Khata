from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field


class Contact(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    name: str
    phone: str
    business_id: str = "demo"


class Transaction(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    contact_id: uuid.UUID
    amount: float
    direction: Literal["owed_to_business", "paid_by_business"]
    date: datetime = Field(default_factory=datetime.utcnow)
    source_message: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0)
    status: Literal["pending", "confirmed", "flagged", "rejected"] = "pending"


class MessageLog(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    raw_message: str
    extraction_result: Optional[Dict[str, Any]] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Flag(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    transaction_id: uuid.UUID
    reason: str
    slack_message_ts: Optional[str] = None
    resolved: bool = False
    resolution: Optional[str] = None
