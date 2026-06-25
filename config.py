import os
from typing import Optional

ANTHROPIC_API_KEY: Optional[str] = os.getenv("ANTHROPIC_API_KEY")
SUPABASE_URL: Optional[str] = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY: Optional[str] = os.getenv("SUPABASE_SERVICE_KEY")
WASSIST_API_KEY: Optional[str] = os.getenv("WASSIST_API_KEY")
WASSIST_AGENT_ID: Optional[str] = os.getenv("WASSIST_AGENT_ID")
LEDGER_CSV_PATH: str = os.getenv("LEDGER_CSV_PATH", "ledger.csv")
SLACK_WEBHOOK_URL: Optional[str] = os.getenv("SLACK_WEBHOOK_URL")
SLACK_BOT_TOKEN: Optional[str] = os.getenv("SLACK_BOT_TOKEN")
REMINDER_THRESHOLD_GBP: float = float(os.getenv("REMINDER_THRESHOLD_GBP", "50"))
CONFIDENCE_THRESHOLD: float = float(os.getenv("CONFIDENCE_THRESHOLD", "0.7"))
SLACK_CHANNEL: str = os.getenv("SLACK_CHANNEL", "#khata-approvals")
SLACK_APPROVAL_TIMEOUT_MINUTES: int = int(os.getenv("SLACK_APPROVAL_TIMEOUT_MINUTES", "15"))
MOCK_MODE: bool = os.getenv("MOCK_MODE", "true").lower() in ("1", "true", "yes")
