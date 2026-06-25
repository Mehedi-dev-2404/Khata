# Khata

Small business owners in informal economies track money through WhatsApp — messages like "Ahmed owes me £120" or "paid Priya back 40 quid" sent to a group or a contact. No spreadsheet. No invoice. No record. Khata reads those messages, extracts the transaction, and maintains a live ledger — without changing how the owner communicates.

## Overview

Khata is a WhatsApp-native bookkeeping agent built for small business owners who manage cash flow through informal chat. Inbound messages are received via a Wassist BYOA webhook, parsed by Claude Haiku into structured transactions, and persisted to both Supabase and a local CSV ledger. Ambiguous or high-value entries are held and routed to a Slack approval channel before being committed. Overdue debtors are chased automatically with AI-drafted reminders sent back through WhatsApp. A `MOCK_MODE` flag allows the entire pipeline to run end-to-end with no external credentials.

## Technical Design

**The core problem: routing trust on a single inference call.**

When a WhatsApp message arrives, the system has one LLM call to determine three things simultaneously: the monetary amount, the direction of the transaction (money owed to the business vs. money paid out), and how confident the model is in that reading. The confidence score then gates the entire write path — high confidence goes straight to the ledger, low confidence or high value gets held for human review.

The non-obvious decision here was to make the confidence threshold a routing mechanism rather than a retry trigger. An earlier design retried low-confidence extractions with a more permissive prompt, but this introduced a failure mode where a genuinely ambiguous message ("sorted the thing with Tariq") could be coerced into a plausible-but-wrong extraction on the second pass. A false-positive ledger entry is worse than a pending flag. The current design treats uncertainty as a signal to escalate, not to resolve. The Slack gate auto-approves after a configurable timeout (default 15 minutes) so the queue does not stall indefinitely if the owner is unavailable.

The extraction prompt is also designed to be stable under the noisy surface forms of informal WhatsApp language: directional cues ("u owe me", "sent you", "settled"), emoji used as payment indicators, and implicit amounts. Rather than enumerate every surface form, the prompt encodes the underlying semantic distinctions and instructs the model to lower confidence when direction is genuinely ambiguous — pushing those cases through the flag path rather than guessing.

The chase module respects WhatsApp's 24-hour session window. Proactive messages can only be sent to contacts who have messaged within the last 24 hours. Rather than silently failing, the client classifies Wassist 4xx responses as "unreachable" and returns a structured count so the caller can distinguish network errors from session expiry.

## Architecture

```
WhatsApp (contact)
       |
       v
  Wassist BYOA
  (webhook bridge)
       |
       v
POST /webhook  [FastAPI]
       |
       +---> extraction.py
       |         |
       |         +-- MOCK_MODE=true  --> regex-based mock response
       |         +-- MOCK_MODE=false --> Claude Haiku (claude-haiku-4-5-20251001)
       |
       +---> Confidence / Amount check
                 |
        low confidence        high confidence
        or amount >= £50      and amount < £50
                 |                    |
                 v                    v
          status=flagged        status=confirmed
                 |                    |
                 v                    v
          Supabase flags       Supabase transactions
          + Slack Block Kit    + ledger.csv
          (approve/reject)
                 |
         POST /slack/actions
         (button click)
                 |
          resolve flag
          update transaction
          sync ledger.csv


POST /chase/run  [manual or scheduled]
       |
       +---> find overdue confirmed transactions (> OVERDUE_DAYS old)
       +---> Claude Sonnet drafts reminder text
       +---> Wassist send_message (respects 24h session window)
       +---> mark last_reminded_at


POST /digest/run  [manual or scheduled]
       |
       +---> aggregate today's stats from Supabase
       +---> Claude Sonnet formats narrative summary
       +---> post to Slack channel + WhatsApp (OWNER_PHONE_NUMBER)
```

## Tech Stack

| Layer | Choice |
|---|---|
| API framework | FastAPI + Uvicorn |
| LLM (extraction) | Claude Haiku (`claude-haiku-4-5-20251001`) — low latency, low cost per message |
| LLM (reminders, digest) | Claude Sonnet (`claude-sonnet-4-6`) — higher quality prose |
| Database | Supabase (Postgres via `supabase-py`) |
| Local ledger | CSV (`ledger.csv`) — zero-dependency audit trail |
| WhatsApp integration | Wassist BYOA (Bring Your Own Agent) |
| Slack integration | Slack Web API via `httpx` (Block Kit interactive messages) |
| HTTP client | `httpx` (sync and async) |
| Deployment target | Railway |

## Key Features

- Inbound WhatsApp messages are parsed into `{amount, direction, confidence, contact_name}` by a single Claude Haiku call constrained to a JSON schema; no code fences or prose are accepted in the response.
- Transactions with confidence below `CONFIDENCE_THRESHOLD` (default `0.7`) or amount at or above `REMINDER_THRESHOLD_GBP` (default `£50`) are written with `status=flagged` and posted to Slack as interactive Block Kit messages with Approve and Reject buttons.
- Flagged transactions auto-approve after `SLACK_APPROVAL_TIMEOUT_MINUTES` (default 15) via a daemon thread, preventing indefinite queue buildup.
- The CSV ledger and Supabase are kept in sync via an upsert on `Transaction ID`; status changes from Slack actions propagate to both stores.
- The chase pass queries `confirmed + owed_to_business` transactions older than `OVERDUE_DAYS` (default 3) that have not been reminded in the past 24 hours, drafts a personalised reminder via Claude Sonnet, and sends it through Wassist. Contacts outside the WhatsApp 24-hour session window are counted as `unreachable` rather than treated as errors.
- Wassist conversation IDs are resolved from phone number and cached on the `contacts` row to avoid repeated API lookups.
- `GET /ledger` serves a self-contained HTML table with confidence visualised as a proportional bar and direction colour-coded, requiring no frontend build step.
- `MOCK_MODE=true` (the default) replaces all external calls — Anthropic, Supabase, Wassist, Slack — with deterministic local stubs. The full request/response cycle can be tested with a single `curl` and no credentials.

## Getting Started

### Prerequisites

- Python 3.10 or later
- pip

### Local Setup (without Docker)

1. Clone the repository.

   ```bash
   git clone <repo-url>
   cd Khata
   ```

2. Create and activate a virtual environment.

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

3. Install dependencies.

   ```bash
   pip install -r requirements.txt
   ```

4. Copy the environment variable template and configure it (see the table below). For a zero-credential local test, the defaults with `MOCK_MODE=true` require no changes.

   ```bash
   cp .env.example .env
   ```

5. Start the server.

   ```bash
   uvicorn main:app --reload --port 8000
   ```

6. Verify the server is running.

   ```bash
   curl http://localhost:8000/health
   ```

### Local Setup (with Docker)

A `Dockerfile` and `railway.toml` are the intended deployment artefacts for Railway. To run locally with Docker:

1. Build the image.

   ```bash
   docker build -t khata .
   ```

2. Run with environment variables.

   ```bash
   docker run --env-file .env -p 8000:8000 khata
   ```

### Environment Variables

| Variable | Description | Example |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude calls | `sk-ant-...` |
| `SUPABASE_URL` | Supabase project URL | `https://xxxx.supabase.co` |
| `SUPABASE_SERVICE_KEY` | Supabase service role key (bypasses RLS) | `eyJ...` |
| `WASSIST_API_KEY` | Wassist BYOA API key | `wsk_...` |
| `WASSIST_AGENT_ID` | Wassist agent identifier | `agent_abc123` |
| `SLACK_BOT_TOKEN` | Slack bot OAuth token (requires `chat:write`) | `xoxb-...` |
| `SLACK_WEBHOOK_URL` | Slack incoming webhook URL (used for digest fallback) | `https://hooks.slack.com/...` |
| `SLACK_CHANNEL` | Slack channel for flagged transactions and digest | `#khata-approvals` |
| `SLACK_APPROVAL_TIMEOUT_MINUTES` | Minutes before a flagged transaction is auto-approved | `15` |
| `LEDGER_CSV_PATH` | Path to the local CSV ledger file | `ledger.csv` |
| `CONFIDENCE_THRESHOLD` | Minimum confidence (0–1) to auto-confirm a transaction | `0.7` |
| `REMINDER_THRESHOLD_GBP` | Amount (GBP) at or above which a transaction is always flagged | `50` |
| `OVERDUE_DAYS` | Days after which an unpaid transaction triggers a chase reminder | `3` |
| `OWNER_PHONE_NUMBER` | Owner's WhatsApp number; receives the daily digest | `+447911123456` |
| `MOCK_MODE` | Set to `false` to enable live API calls; `true` by default | `true` |

## API Reference

### `GET /health`

Returns server and database status.

**Response**

```json
{
  "status": "ok",
  "db_connected": true
}
```

---

### `POST /webhook`

Receives an inbound WhatsApp message from the Wassist webhook bridge and runs the full extraction and routing pipeline.

**Request body**

```json
{
  "message": "Ahmed still owes me £80 from last week",
  "image": null,
  "phone_number": "+447911123456",
  "reply_callback": "https://wassist.app/api/v1/callbacks/abc123"
}
```

**Response — auto-confirmed transaction**

```json
{
  "type": "message",
  "content": "Got it — noted £80 owed to you from Ahmed. Ledger updated ✓"
}
```

**Response — flagged transaction (silent; Slack notified)**

```json
{
  "content": "No CUSTOMER message reply"
}
```

---

### `POST /slack/actions`

Receives interactive button payloads from Slack (approve/reject on flagged transaction messages). Called by Slack's interactive components API; expects `application/x-www-form-urlencoded` with a `payload` field containing a JSON string.

Returns HTTP 200 with an empty body. State changes (transaction status, ledger sync, Slack message update) happen synchronously before the response is returned.

---

### `POST /chase/run`

Triggers a full overdue-reminder pass. Queries all confirmed, owed-to-business transactions older than `OVERDUE_DAYS` that have not been reminded in the past 24 hours, drafts reminders via Claude Sonnet, and sends them through Wassist.

**Response**

```json
{
  "reminded": 3,
  "unreachable": 1,
  "errors": 0
}
```

`unreachable` counts contacts outside the WhatsApp 24-hour session window. `errors` counts unexpected failures.

---

### `POST /digest/run`

Builds an end-of-day summary from Supabase, formats it as a short narrative via Claude Sonnet, and posts it to the configured Slack channel. Optionally sends the same message to `OWNER_PHONE_NUMBER` via WhatsApp.

**Response**

```json
{
  "slack_sent": true,
  "whatsapp_sent": false,
  "data": {
    "transactions_today": 7,
    "reminders_today": 2,
    "outstanding_gbp": 340.00,
    "unresolved_flags": 1,
    "date": "2026-06-25"
  }
}
```

---

### `GET /ledger`

Returns a self-contained HTML page rendering all transactions from the local CSV ledger. Direction is colour-coded; confidence is displayed as a proportional bar. No authentication.

## Pipeline: End-to-End Flow

1. A contact sends a WhatsApp message to the business number (e.g. "Riz owes me 65 quid").
2. Wassist receives the message and POSTs it to `POST /webhook` on the Khata server.
3. `extraction.py` sends the message to Claude Haiku with a structured JSON schema prompt, providing the contact's recent transaction history as context. The model returns `{amount, direction, confidence, contact_name, reasoning}`.
4. The webhook handler checks two conditions:
   - `confidence < CONFIDENCE_THRESHOLD` (default 0.7), **or**
   - `amount >= REMINDER_THRESHOLD_GBP` (default £50).
5. If neither condition is met, the transaction is written to Supabase with `status=confirmed`, synced to `ledger.csv`, and a confirmation reply is sent back through the Wassist webhook response.
6. If either condition is met, the transaction is written with `status=flagged`. A Slack Block Kit message with Approve and Reject buttons is posted to `SLACK_CHANNEL`. A daemon thread starts that will auto-approve the flag after `SLACK_APPROVAL_TIMEOUT_MINUTES` if no action is taken.
7. When a reviewer clicks Approve or Reject in Slack, Slack POSTs to `POST /slack/actions`. The handler resolves the flag, updates `status` to `confirmed` or `rejected`, syncs `ledger.csv`, and edits the Slack message to show the outcome and the reviewer's name.
8. Separately, `POST /chase/run` can be called on a schedule. It finds confirmed, unpaid transactions past their overdue threshold, drafts a WhatsApp reminder via Claude Sonnet, and sends it through Wassist — skipping contacts outside the 24-hour session window.
9. `POST /digest/run` aggregates the day's activity and posts a narrative summary to Slack and optionally to the owner's WhatsApp.

## Demo

With `MOCK_MODE=true` (the default), no credentials are required. Start the server and send a test message:

```bash
# Start the server
uvicorn main:app --reload --port 8000

# Simulate an inbound WhatsApp message — high confidence, auto-confirmed
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Tariq owes me £35 from the market run",
    "phone_number": "+447911000001",
    "reply_callback": "https://example.com/callback"
  }'

# Simulate a high-value message — will be flagged, Slack notification printed to stdout
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "message": "settled the big invoice £200",
    "phone_number": "+447911000002",
    "reply_callback": "https://example.com/callback"
  }'

# Trigger the chase pass (prints mock send to stdout)
curl -X POST http://localhost:8000/chase/run

# Trigger the daily digest (prints mock message to stdout)
curl -X POST http://localhost:8000/digest/run

# View the HTML ledger
open http://localhost:8000/ledger
```

In `MOCK_MODE`, Slack notifications, Wassist sends, and Claude API calls are all printed to stdout rather than making network requests.

## Project Status

Hackathon proof-of-concept (~3 hours build time). Core pipeline is functional end-to-end. Production hardening would require: Slack request signature verification on `/slack/actions`, per-business multi-tenancy (the `business_id` field is currently hardcoded to `"demo"`), and a scheduled job runner for chase and digest (currently manual HTTP triggers).

## License

MIT
