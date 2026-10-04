-- Newsletter archive index (SQLite). Extends PHASE4_DESIGN.md.
-- Additions vs. the original design (see PHASE4_DESIGN.md "v2 additions"):
--   messages: source_key, list_id, list_unsubscribe, sending_platform, has_html, has_text,
--             split_status, capture_backend
--   items / stories / story_items / issues_out  (story-level analysis + our generated issues)
-- Applied idempotently; PRAGMA user_version tracks the schema version.

-- ---------------------------------------------------------------- Stage A
CREATE TABLE IF NOT EXISTS messages (
  gmail_id         TEXT PRIMARY KEY,   -- hex Gmail id (API id == hex(X-GM-MSGID) on IMAP)
  thread_id        TEXT,
  segment          TEXT,               -- e.g. 1-TechAI, or 'unmatched'
  source_key       TEXT,               -- stable per-newsletter key (sender address) for "N sources covered this"
  newsletter       TEXT,
  publisher        TEXT,
  sender_name      TEXT,
  sender_email     TEXT,
  to_address       TEXT,               -- Delivered-To (carries the +tag)
  subject          TEXT,
  sent_date        TEXT,               -- ISO 8601 UTC
  received_date    TEXT,
  is_issue         INTEGER,            -- 1 real content, 0 welcome/confirm/verify
  labels           TEXT,               -- JSON list (API backend only)
  snippet          TEXT,
  list_id          TEXT,
  list_unsubscribe TEXT,
  sending_platform TEXT,               -- substack | beehiiv | mailchimp | ... | unknown
  has_html         INTEGER,
  has_text         INTEGER,
  raw_eml_path     TEXT,               -- relative to archive_dir
  clean_text_path  TEXT,               -- filled by M2
  raw_html_path    TEXT,
  word_count       INTEGER,
  reading_minutes  REAL,
  has_attachments  INTEGER,
  content_hash     TEXT,               -- sha256 of raw message
  capture_backend  TEXT,               -- api | imap | file
  ingested_at      TEXT,
  split_status     TEXT DEFAULT 'pending',   -- pending | done | failed | skipped
  enrich_status    TEXT DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS sync_log (
  id INTEGER PRIMARY KEY,
  run_at TEXT, backend TEXT, query TEXT,
  scanned INTEGER, new_added INTEGER, skipped_existing INTEGER,
  enriched INTEGER, failed INTEGER,
  status TEXT, notes TEXT
);

-- ---------------------------------------------------------------- M2: one row per story inside an issue
CREATE TABLE IF NOT EXISTS items (
  id          INTEGER PRIMARY KEY,
  gmail_id    TEXT REFERENCES messages(gmail_id),
  position    INTEGER,          -- order inside the issue
  section     TEXT,             -- section heading in the source issue, if any
  title       TEXT,
  body        TEXT,
  url         TEXT,             -- primary outbound link (tracking stripped)
  is_sponsor  INTEGER DEFAULT 0,
  segment     TEXT,
  source_key  TEXT,
  sent_date   TEXT,
  UNIQUE (gmail_id, position)
);

-- ---------------------------------------------------------------- Stage B (Phase 5)
CREATE TABLE IF NOT EXISTS enrichment (
  gmail_id       TEXT PRIMARY KEY REFERENCES messages(gmail_id),
  tldr TEXT, headline TEXT, tone TEXT, sentiment TEXT, importance INTEGER, main_topic TEXT,
  llm_provider TEXT, llm_model TEXT, prompt_version TEXT, raw_json TEXT, extracted_at TEXT, error TEXT
);
-- Per-item extraction (preferred unit; per-issue enrichment above is kept for essays)
CREATE TABLE IF NOT EXISTS item_enrichment (
  item_id        INTEGER PRIMARY KEY REFERENCES items(id),
  summary TEXT, category TEXT, importance INTEGER,
  llm_provider TEXT, llm_model TEXT, prompt_version TEXT, raw_json TEXT, extracted_at TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS claims   (id INTEGER PRIMARY KEY, gmail_id TEXT, item_id INTEGER, claim_text TEXT, claim_type TEXT, subject TEXT, context TEXT);
CREATE TABLE IF NOT EXISTS quotes   (id INTEGER PRIMARY KEY, gmail_id TEXT, item_id INTEGER, quote_text TEXT, speaker TEXT, speaker_role TEXT, context TEXT);
CREATE TABLE IF NOT EXISTS links    (id INTEGER PRIMARY KEY, gmail_id TEXT, item_id INTEGER, url TEXT, anchor_text TEXT, domain TEXT, link_type TEXT);
CREATE TABLE IF NOT EXISTS entities (id INTEGER PRIMARY KEY, gmail_id TEXT, item_id INTEGER, name TEXT, entity_type TEXT, mention_count INTEGER);
CREATE TABLE IF NOT EXISTS stats    (id INTEGER PRIMARY KEY, gmail_id TEXT, item_id INTEGER, value TEXT, unit TEXT, description TEXT);
CREATE TABLE IF NOT EXISTS topics   (id INTEGER PRIMARY KEY, gmail_id TEXT, item_id INTEGER, topic TEXT, relevance TEXT);

-- ---------------------------------------------------------------- M4: same story across sources
CREATE TABLE IF NOT EXISTS stories (
  id            INTEGER PRIMARY KEY,
  segment       TEXT,
  headline      TEXT,
  first_seen    TEXT,
  last_seen     TEXT,
  source_count  INTEGER,          -- distinct source_key covering it (salience)
  consensus     TEXT,             -- JSON: facts all sources agree on
  divergence    TEXT,             -- JSON: where sources differ
  created_at    TEXT
);
CREATE TABLE IF NOT EXISTS story_items (
  story_id INTEGER REFERENCES stories(id),
  item_id  INTEGER REFERENCES items(id),
  similarity REAL,
  PRIMARY KEY (story_id, item_id)
);

-- ---------------------------------------------------------------- M5/M6: issues WE generate
CREATE TABLE IF NOT EXISTS issues_out (
  id            INTEGER PRIMARY KEY,
  segment       TEXT,
  issue_date    TEXT,
  status        TEXT DEFAULT 'draft',  -- draft | approved | sent | rejected
  html_path     TEXT,
  md_path       TEXT,
  story_ids     TEXT,                  -- JSON list
  validator_report TEXT,               -- JSON: citation/grounding check results
  llm_model     TEXT,
  prompt_version TEXT,
  created_at    TEXT,
  UNIQUE (segment, issue_date)
);

CREATE INDEX IF NOT EXISTS idx_msg_segment ON messages(segment);
CREATE INDEX IF NOT EXISTS idx_msg_date    ON messages(sent_date);
CREATE INDEX IF NOT EXISTS idx_msg_split   ON messages(split_status);
CREATE INDEX IF NOT EXISTS idx_msg_enrich  ON messages(enrich_status);
CREATE INDEX IF NOT EXISTS idx_items_seg_date ON items(segment, sent_date);
