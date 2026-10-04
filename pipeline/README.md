# pipeline/ — newsletter → our own digest

Python package `nlagg`. Turns the subscribed inbox into an archive, then (later milestones) into our
own per-segment newsletter. Plan and task list: see **Pipeline plan** in `PROJECT_CONTEXT.md` §12.

| Milestone | Stage | Status |
|---|---|---|
| M0 | Repo setup, config, DB schema v2 (items/stories/issues_out) | ✅ merged (PR #1) |
| M1 | **Capture** Gmail → `.eml` + `messages` rows (no AI) | ✅ merged (PR #1) — needs a real-inbox run |
| M2 | **Clean** HTML → `.md` + **split** each issue into story `items` (no AI) | ✅ code (PR #2) — needs the 20-email check |
| M3 | Per-item LLM extraction (Groq / local Qwen — never Claude) | |
| M4 | Cluster items across sources into `stories` (consensus / divergence) | |
| M5 | Compose our issue (template + exemplar prompt + citation validator) | |
| M6–M8 | Review & deliver, schedule & monitor, more segments | |

First segment end-to-end: **1-TechAI** (`segments.active` in `config.yaml`). Capture always takes everything.

## Setup (once per machine)
```bash
cd pipeline
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[gmail,dev]"
pytest -q                                               # 46 tests, no network needed
```

## Capture (M1)
```bash
python -m nlagg init-db                    # creates data/archive/index.db (gitignored)
python -m nlagg capture --dry-run          # how many new mails would be pulled
python -m nlagg capture                    # first run = full backfill; later runs = incremental
python -m nlagg stats                      # per-segment counts, per-sender health, last sync runs
```
Options: `--backend api|imap|file`, `--backfill`, `--since YYYY-MM-DD`, `--limit N`, `--from DIR` (file backend).

## Clean & split (M2)
```bash
python -m nlagg split                      # issues of segments.active (1-TechAI) -> .md + items
python -m nlagg split --all-segments       # every segment
python -m nlagg split --redo               # re-split after changing rules
python -m nlagg split-review               # hand-check sheet -> pipeline/out/review/ (gitignored)
```
What it does, per captured issue (`is_issue = 1`):
- **Clean**: HTML → ordered blocks → `<same name>.md` next to the `.eml`. Drops hidden preheaders,
  tracking pixels, `<style>`, "view in browser"/"sign up" lines, and the footer (first unsubscribe /
  preferences / "you're receiving this" marker in the last 40% of the email).
- **Links**: unwraps redirect wrappers that embed the destination (TLDR `/CL0/…`, `?url=`, Google
  `?q=`), strips tracking-only params (`utm_*`, `mc_*`, `fbclid`, …). Opaque click-trackers
  (beehiiv, Substack redirect, ConvertKit, …) are kept and typed `tracked`.
- **Split** into `items`: a title-like line (heading, fully bold, or a short line that is a link)
  starts a story; short unlinked headings before a title become the `section`; referral /
  advertise / feedback blocks are dropped; < 2 stories but ≥ 150 words → one `essay`; a short
  post with "keep reading" → one `teaser` pointing at the full post.
- **Sponsors** (`items.is_sponsor`, links typed `sponsor`) are detected from labels ("(Sponsor)",
  "Together with X", "Presented by"), not from the word "sponsor" in a news story.
- **Promo mails** (`messages.is_promo`): sale / upgrade / webinar subject **and** ≤ 2 stories.
- Re-runnable: one transaction per issue; `--redo` replaces items; failures → `split_status = failed`
  with `split_error`, retried next run. Schema v3 columns are added to older DBs automatically.

**M2 done-check (from the plan):** run `split-review` on 20 real 1-TechAI issues and tick OK / Not OK;
target ≥ 90% OK. Fixtures cover the five shapes, but rules must be tuned on real mail.

**Backends** — all produce the same hex Gmail id, so you can switch without duplicates:
- `api` (default): Gmail REST API. Reuses the existing google-skill login — `~/.config/google-skill/credentials.json`
  + `tools/google-skill/.claude/google-skill.local.json`. Only works on a machine where that auth was done.
- `imap`: Gmail IMAP + App Password (`NLAGG_IMAP_PASSWORD`, see `.env.example`). **Use this for scheduled
  runs**: OAuth apps left in "Testing" mode get refresh tokens that expire after ~7 days.
- `file`: import a folder of `.eml` files (e.g. an old `export_inbox.ts` export: ids are read from `__<id>.eml`).

**What capture guarantees**
- Idempotent on `gmail_id` — re-runs never duplicate; failures are logged in `sync_log` and retried next run.
- Incremental window = last successful sync − `overlap_days` (3), so late mail is never missed.
- File is written before the DB row, so a DB row never points at a missing file.
- Routing: `sender_overrides` first (Morning Brew, HR Brew, FindLaw, Babylon Bee, Lawfare), then the `+tag`
  via `data/category_feeds.csv`, else `unmatched`. Welcome/confirm mails get `is_issue = 0`.

Layout: `data/archive/<segment>/<YYYY-MM-DD>/<sender-slug>__<gmail_id>.eml` — kept local (copyrighted content).

## Code map
```
src/nlagg/
  config.py        config.yaml + category_feeds.csv -> Config
  schema.sql       full DB schema (v2), applied idempotently
  db.py            connect/init, insert_message, sync_log
  parse_email.py   raw email -> messages row (routing, is_issue, platform, word count)
  fetchers.py      GmailApiBackend | ImapBackend | EmlDirBackend
  capture.py       M1 orchestration
  clean.py         M2: HTML -> blocks, link canonicalisation, boilerplate/footer removal, markdown
  split.py         M2: blocks -> story items (roundup / essay / teaser), sponsor + promo rules
  split_run.py     M2 orchestration + split-review sheet
  cli.py           `python -m nlagg ...`
tests/             synthetic emails, fake Gmail API / IMAP servers, fixtures/ per newsletter shape
prompts/, templates/  added in M3/M5
```

## Known gaps (tracked for next PRs)
- `newsletter` / `publisher` columns are empty: needs a sender → newsletter map (`data/senders.csv`),
  best built from the first real `nlagg stats` output.
- Opaque tracked links (beehiiv/Substack/ConvertKit) are not resolved to the real URL yet; that
  needs a network request per link (open question in PROJECT_CONTEXT §12).
- Teasers point at the full post but don't fetch it.
