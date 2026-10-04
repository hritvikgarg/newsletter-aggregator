# Phase 4 & 5 — Newsletter Archive + LLM Enrichment (Design)

Provider-agnostic design. Enrichment can run on **Groq** (cloud) or **local Qwen** (Ollama)
or any OpenAI-compatible endpoint — switching is a config change, DB and pipeline stay identical.

---

## Two stages

### Stage A — Capture (deterministic, no LLM, free)
Mirrors every newsletter out of Gmail into structured local storage.
- Pull messages via the Gmail API (auth already set up in tools/google-skill).
- For each `gmail_id` not already in DB: save raw `.eml` (lossless), extract clean `.md`, insert a `messages` row.
- Idempotent on `gmail_id` → never duplicates, always back-fills. Gmail is the safety net.

### Stage B — Enrich (LLM, swappable)
Reads the clean text of each issue, extracts structured fields via an LLM, writes to DB.

---

## LLM provider abstraction (the key part)

All targets speak the OpenAI-compatible Chat Completions API, so we use ONE client and switch by config.

| Provider | OPENAI_BASE_URL | model | API key |
|---|---|---|---|
| Groq | https://api.groq.com/openai/v1 | llama-3.3-70b-versatile (or a Qwen model) | GROQ_API_KEY |
| Local Qwen (Ollama) | http://localhost:11434/v1 | qwen2.5:14b-instruct (or qwen2.5:7b) | (dummy) |
| vLLM / LM Studio / Together | their /v1 url | their model | their key |

Config file `enrich.config.json` (or env vars):
```
{
  "provider": "groq",
  "base_url": "https://api.groq.com/openai/v1",
  "model": "llama-3.3-70b-versatile",
  "api_key_env": "GROQ_API_KEY",
  "max_retries": 3,
  "prompt_version": "v1"
}
```
Switch to local Qwen = change provider/base_url/model. Nothing else changes.

### Enforcing complete output
- Prompt embeds the exact JSON schema; instructs "all fields required, use null/[] if absent."
- Use JSON mode (`response_format={"type":"json_object"}`) where supported; Ollama supports `format` schema.
- Validate returned JSON against the schema (ajv/pydantic). Invalid → retry up to max_retries.
- Persist raw LLM JSON (`enrichment.raw_json`) for audit + re-parse.
- `messages.enrich_status` = pending | done | failed | skipped; nightly job re-runs pending/failed.

### Extraction JSON contract (one object per issue)
```
{
  "tldr": "2-3 sentence summary",
  "headline": "one line",
  "tone": "news|opinion|satire|promo",
  "sentiment": "positive|negative|neutral",
  "importance": 1-5,
  "main_topic": "string",
  "topics": ["..."],
  "claims":  [{"claim_text":"","claim_type":"fact|prediction|opinion","subject":"","context":""}],
  "quotes":  [{"quote_text":"","speaker":"","speaker_role":"","context":""}],
  "links":   [{"url":"","anchor_text":"","domain":"","link_type":"source|sponsor|social"}],
  "entities":[{"name":"","entity_type":"person|company|product|place|ticker|law_case","mention_count":1}],
  "stats":   [{"value":"","unit":"","description":""}]
}
```

---

## Database schema (SQLite: data/archive/index.db)

```sql
-- Stage A: one row per email (always filled)
CREATE TABLE messages (
  gmail_id       TEXT PRIMARY KEY,
  thread_id      TEXT,
  segment        TEXT,
  newsletter     TEXT,
  publisher      TEXT,
  sender_name    TEXT,
  sender_email   TEXT,
  to_address     TEXT,
  subject        TEXT,
  sent_date      TEXT,          -- ISO 8601 UTC
  received_date  TEXT,
  is_issue       INTEGER,       -- 1 real content, 0 welcome/confirm/promo
  labels         TEXT,
  snippet        TEXT,
  raw_eml_path   TEXT,
  clean_text_path TEXT,
  raw_html_path  TEXT,
  word_count     INTEGER,
  reading_minutes REAL,
  has_attachments INTEGER,
  content_hash   TEXT,          -- sha256 of body (change detection)
  ingested_at    TEXT,
  enrich_status  TEXT DEFAULT 'pending'
);

-- Stage B: one row per enriched issue (+ which LLM produced it)
CREATE TABLE enrichment (
  gmail_id       TEXT PRIMARY KEY REFERENCES messages(gmail_id),
  tldr           TEXT,
  headline       TEXT,
  tone           TEXT,
  sentiment      TEXT,
  importance     INTEGER,
  main_topic     TEXT,
  llm_provider   TEXT,          -- groq | ollama | ...
  llm_model      TEXT,          -- llama-3.3-70b | qwen2.5:14b
  prompt_version TEXT,
  raw_json       TEXT,          -- full raw LLM output (lossless)
  extracted_at   TEXT,
  error          TEXT
);

CREATE TABLE claims (
  id INTEGER PRIMARY KEY, gmail_id TEXT REFERENCES messages(gmail_id),
  claim_text TEXT, claim_type TEXT, subject TEXT, context TEXT );

CREATE TABLE quotes (
  id INTEGER PRIMARY KEY, gmail_id TEXT REFERENCES messages(gmail_id),
  quote_text TEXT, speaker TEXT, speaker_role TEXT, context TEXT );

CREATE TABLE links (
  id INTEGER PRIMARY KEY, gmail_id TEXT REFERENCES messages(gmail_id),
  url TEXT, anchor_text TEXT, domain TEXT, link_type TEXT );

CREATE TABLE entities (
  id INTEGER PRIMARY KEY, gmail_id TEXT REFERENCES messages(gmail_id),
  name TEXT, entity_type TEXT, mention_count INTEGER );

CREATE TABLE stats (
  id INTEGER PRIMARY KEY, gmail_id TEXT REFERENCES messages(gmail_id),
  value TEXT, unit TEXT, description TEXT );

CREATE TABLE topics (
  id INTEGER PRIMARY KEY, gmail_id TEXT REFERENCES messages(gmail_id),
  topic TEXT, relevance TEXT );

CREATE TABLE sync_log (
  id INTEGER PRIMARY KEY, run_at TEXT, scanned INTEGER,
  new_added INTEGER, enriched INTEGER, failed INTEGER, status TEXT, notes TEXT );

CREATE INDEX idx_msg_segment ON messages(segment);
CREATE INDEX idx_msg_date    ON messages(sent_date);
CREATE INDEX idx_msg_enrich  ON messages(enrich_status);
```

Category-specific data lives naturally in `entities` (entity_type = ticker / law_case / product)
and `stats`, so no per-segment tables are needed.

---

## Completeness guarantees
1. Capture keyed on gmail_id → no misses, no dupes; scheduled + backfilling.
2. Raw .eml kept forever → enrichment is always re-runnable (swap models freely).
3. enrich_status + sync_log → auditable; failed/pending auto-retried each run.
4. Schema validation on LLM output → every field attempted; malformed retried.

## Recommended start
- Stage A first (free, bulletproof capture) — nothing lost from here on.
- Stage B with **Groq** to start (free tier, fast, zero hardware), then optionally switch to
  local **Qwen via Ollama** for full privacy by editing enrich.config.json.

---

## v2 additions (2026-10-04) — implemented in `pipeline/src/nlagg/schema.sql`

Why: most issues are roundups (~52%) or sectioned briefs (~35%), so one email = many stories.
The product USPs ("4/5 sources led with this", Consensus vs Conflict) need **story-level** rows that
can be matched **across** newsletters. The original schema only had per-email enrichment.

| Table / column | Purpose |
|---|---|
| `messages.source_key` | stable per-newsletter key (sender address) → "N distinct sources covered this" |
| `messages.list_id`, `list_unsubscribe`, `sending_platform`, `has_html`, `has_text`, `capture_backend` | captured headers / provenance |
| `messages.split_status` | M2 progress flag (pending/done/failed/skipped), like `enrich_status` |
| `items` | one row per story inside an issue (title, body, primary url, is_sponsor, section, position) |
| `item_enrichment` | per-item LLM output (summary, category, importance, model, prompt_version, raw_json) |
| `claims/quotes/links/entities/stats/topics.item_id` | facts can hang off an item, not just the email |
| `stories` + `story_items` | same story across sources; `source_count`, `consensus`, `divergence` |
| `issues_out` | issues WE generate: status draft→approved→sent, paths, validator report, model |

Schema version is tracked with `PRAGMA user_version` (v1 = original design above, v2 = this).
