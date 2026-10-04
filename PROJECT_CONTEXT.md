# PROJECT CONTEXT & TEAM HANDOFF — Newsletter Aggregator

> **Purpose of this file:** the single source of truth for the whole project — what it is,
> every decision made, current status, how things work, and what's next. Read this top-to-bottom
> to get fully up to speed. A running **Session Log** at the bottom records each work session.
>
> **Last updated:** 2026-10-04 · **Maintainer:** tdsworks@gmail.com

---

## 1. What this project is (one paragraph)
A **newsletter aggregation + AI-summarization system**. We subscribe a single dedicated Gmail
to ~45 newsletters across 9 topic segments, auto-sort them with Gmail filters, then (in progress)
scrape every issue into a structured local archive + database, and finally (planned) use a
**non-Claude LLM** (Groq or local Qwen) to summarize them into our own digest newsletter(s).
End goal: build our **own newsletter product per segment** that aggregates + synthesizes the
sources rather than depending on any single one.

---

## 2. Current status snapshot (2026-10-04)
- **Phase 1–3 complete.** 38/45 confirmed delivering, 3 pending, 4 dropped (`data/sources.csv`).
- **Gmail API read access is set up and working** (OAuth, read-only, via the `google-skill` tool) on the original machine.
- **Phase 4 capture code is built** (`pipeline/`, M1: Gmail → .eml + SQLite, tested); first real-inbox run pending.
- **Phase 5 (LLM enrichment) is designed** (LLM-agnostic; Groq/Qwen, NOT Claude); per-item, not per-email (schema v2).
- **Product strategy for the News segment** has been explored in depth (USP, techniques, sample issues).
- **Pipeline plan M0–M8** is in §12; first segment end-to-end is 1-TechAI.

---

## 3. Phase roadmap
| Phase | What | Status |
|---|---|---|
| 1 — Curate | Research + pick 45 newsletters, 9 segments | ✅ Done |
| 2 — Gmail infra | Dedicated Gmail, +tag addresses, 9 filters + labels, sender-filters | ✅ Done |
| 3 — Subscribe & verify | Sign up all 45, confirm, live-verify via Gmail API | ✅ ~Done |
| 4 — Archive/ingestion | Scrape Gmail → .eml + .md + SQLite index; daily auto-sync | 🟡 Capture code built (`pipeline/`, M1) — needs first real-inbox run |
| 5 — LLM enrichment | Non-Claude LLM extracts claims/quotes/entities etc. → DB | ⏳ Designed |
| 6 — Digest product | Compose our own per-segment newsletter from the archive | ⏳ Strategy explored |

---

## 4. Gmail infrastructure (DONE)
- **Account:** `notifyy1008@gmail.com` (dedicated; NOT a personal inbox).
- **Approach:** originally used Kill-the-Newsletter (KTN → RSS) — **RETIRED** because the KTN address
  got blocked by signup forms. Now uses **Gmail `+tag` plus-addressing** (one real account).
- **9 segments → +tag → label:**

| Segment | Subscribe address | Gmail label |
|---|---|---|
| 1-TechAI | notifyy1008+techai@gmail.com | Newsletters/1-TechAI |
| 2-BizFinance | notifyy1008+biz@gmail.com | Newsletters/2-BizFinance |
| 3-Legal | notifyy1008+legal@gmail.com | Newsletters/3-Legal |
| 4-HRPeopleOps | notifyy1008+hr@gmail.com | Newsletters/4-HR |
| 5-GitHubRepos | notifyy1008+github@gmail.com | Newsletters/5-GitHub |
| 6-IndieHacker | notifyy1008+indie@gmail.com | Newsletters/6-IndieHacker |
| 7-Absurdist | notifyy1008+satire@gmail.com | Newsletters/7-Absurdist |
| 8-MicroSmallCap | notifyy1008+smallcap@gmail.com | Newsletters/8-SmallCap |
| 9-MainstreamNews | notifyy1008+news@gmail.com | Newsletters/9-News |

- **Filters:** 9 tag-based filters match on `deliveredto:notifyy1008+<tag>@gmail.com` (NOT the "To" field —
  it's unreliable for plus-addresses) and apply the segment label. Imported from `gmail_filters.xml`.
- **Sender-based filters** (for sites that rejected the `+`, so we used the plain address):
  Morning Brew (morningbrew.com)→2-BizFinance, HR Brew (hr-brew via morningbrew.com)→4-HR,
  FindLaw (findlaw.com)→3-Legal, Babylon Bee (babylonbee.com)→7-Absurdist. From `gmail_filters_sender.xml`.
- **Lawfare quirk:** subscribed via a Substack recommendation on the +techai address, so it lands in
  1-TechAI; `gmail_filter_lawfare.xml` re-tags it to 3-Legal (needs the +techai filter to also exclude it).
- **Known gotcha:** some sites reject `+` addresses → fall back to plain `notifyy1008@gmail.com` + add a
  sender filter. Some show a "did you mean…?" nudge — keep the +address as-is.

---

## 5. Subscription status
- **Active / delivering:** ~39–41 of 45. See `data/sources.csv` (the live tracker) for exact per-row status.
- **Dead (cannot subscribe — do not retry):** Workology (no signup), The Hard Times (404), Small Cap
  Discoveries (paid), OTC Adventures (Substack deleted).
- **Needed manual final steps (may already be done):** RedChip (enter code `1fcc44` on redchip.com),
  The Bootstrapped Founder (click the confirm email).
- `data/sources.csv` columns: segment, newsletter_name, publisher, signup_url, subscriber_estimate,
  send_frequency, double_optin, description, pick_type, subscribe_email, gmail_label, **status**,
  signup_note, date_subscribed, expected_next_check.
  Status values: confirmed_active | subscribed | subscribed_pending_confirmation | pending_confirmation |
  no_email_yet | dropped | failed_retry.

---

## 6. Gmail API access — how it works (IMPORTANT for team)
- We use the open-source **google-skill** (https://github.com/The-Focus-AI/google-skill) cloned to
  `tools/google-skill/`. It talks to the Gmail API over OAuth.
- **We narrowed the scope to Gmail read-only** (edited `tools/google-skill/scripts/lib/auth.ts` SCOPES to
  only `gmail.readonly`). No Calendar/Drive/etc. access.
- **Own Google Cloud OAuth credentials** were created (project `newsletter-reader`, Desktop OAuth client).
  The shared/embedded creds in the repo do NOT work (locked to the authors' testers).
- **Secrets are LOCAL, not in the repo (do NOT commit):**
  - OAuth client: `~/.config/google-skill/credentials.json`
  - Refresh token: `tools/google-skill/.claude/google-skill.local.json`
- **⚠️ Team/cloud note:** these secrets are machine-local. A teammate or cloud run must do their OWN
  Google Cloud OAuth setup + `npx tsx skills/gmail/scripts/gmail.ts auth` (see `PHASE4_DESIGN.md` /
  the skill's setup-guide). Do not share tokens in the repo.
- **Read the inbox (examples), run from `tools/google-skill/`:**
  - `npx tsx skills/gmail/scripts/gmail.ts list --query="in:anywhere newer_than:2d" --max=50`
  - `npx tsx skills/gmail/scripts/gmail.ts read <message-id>`
- **Cost:** Gmail API is FREE (no billing). The `$300 Google Cloud` banner is irrelevant.
- **No Claude/AI anywhere in the capture path** — it's plain code. Only Phase 5 uses an LLM (Groq/Qwen).

---

## 7. Content-structure findings (audit of 70 real issues)
- Every newsletter is HTML; ~70% also ship `text/plain` (easier to parse); ~30% HTML-only
  (all Semafor editions, Axios, NYT, most Substacks).
- **Images are ALWAYS remote URLs** (0 inline/attached across all). **Zero attachments** anywhere.
  Store image URLs, not files. `<img>` counts are inflated by tracking pixels/spacers → filter them.
- **5 content shapes** (restricted to our 45 picks, ~31 classified):
  - **Links-roundup** (~52%): multi-story digest. Newsletters: Morning Brew, The Hustle, Daily Upside,
    Above the Law, FindLaw, SCOTUSblog, HR Brew, HR Dive, SHRM, Trends.vc, The Onion, NYT, Axios, Semafor,
    Superhuman AI, The Neuron.
  - **Brief/sectioned** (~35%): TLDR, TLDR WebDev, Console, Bytes, Indie Hackers, Starter Story,
    National Law Review, Lawfare, Babylon Bee, Zacks, Washington Post.
  - **Content essay** (2): The Average Joe, The Diff.
  - **Image+content** (1): The Guardian.
  - **Teaser/preview** (1): Planet MicroCap (truncated free Substack — full text is on the web).
- **Parser priority:** build the links-roundup extractor first, then brief/sectioned, then essays/teaser.
- **Truncated Substack free tiers** deliver only a preview + "read online" link (handle specially).

---

## 8. Phase 4 — Archive design (see PHASE4_DESIGN.md for full detail)
Two stages; Stage A is deterministic (no AI), Stage B is the LLM enrichment.

**Stage A — Capture (no AI):** Gmail API → for each new `gmail_id`: save raw `.eml` (lossless) +
clean `.md` (text) + insert a `messages` row. Idempotent on `gmail_id` → never miss/dupe.
**Folder layout:** `data/archive/<segment>/<YYYY-MM-DD>/<slug>__<gmailid>.eml` and `.md`.
**Proof done:** captured 1 real email (Above the Law) → `data/archive/3-Legal/2026-08-06/`.

**Fetch method options:** Gmail REST API (current, free, `format=raw` gives lossless .eml) OR IMAP +
App Password (no Google Cloud, needs 2FA). Both give identical raw .eml; API also gives snippet/labels
directly. Browser-scrape/Takeout are fallbacks. Chosen: **API** (already working).

**What one email yields (24 fields):** gmail_id, thread_id, segment (from +tag), sender name/email,
reply_to, subject, sent_date, received_date, is_issue, sending_platform (from headers), unsubscribe link,
labels, size, snippet, word_count, reading_minutes, link_count, attachments, content_hash, eml_path,
md_path — plus all links (deterministically extracted), and (Stage B) claims/quotes/entities/etc.

---

## 9. Phase 5 — LLM enrichment design (NON-CLAUDE, swappable)
- **Provider-agnostic:** Groq (cloud, free tier, fast) or local Qwen via Ollama (private, free) or any
  OpenAI-compatible endpoint. Switch = change base_url/model/api_key. **Never Claude.**
- One JSON object per issue → validated against schema → fanned out into DB tables.
- **DB = SQLite (`data/archive/index.db`).** Tables:
  - `messages` (Stage A capture, always filled) — see field list above + `enrich_status`.
  - `enrichment` (1 per issue) — tldr, headline, tone, sentiment, importance, main_topic,
    **llm_provider, llm_model, prompt_version, raw_json** (records WHICH model produced it), extracted_at, error.
  - `claims` (claim_text, claim_type fact/prediction/opinion, subject, context)
  - `quotes` (quote_text, speaker, speaker_role, context)
  - `links` (url, anchor_text, domain, link_type)
  - `entities` (name, entity_type person/company/product/ticker/law_case, mention_count)
  - `stats` (value, unit, description)
  - `topics` (topic, relevance)
  - `sync_log` (run_at, scanned, new_added, enriched, failed, status)
- Completeness: raw .eml kept forever → re-runnable; enrich_status auto-retries failures; schema validation.

---

## 10. Product strategy — building our OWN newsletter (News segment explored)
**Core principle:** we produce no original content, so we compete on the **synthesis/curation layer**
that no single source can build. This also makes us **independent of any single source** (sources are
interchangeable fuel; redundancy means losing one loses ~0 coverage; modular ingestion).

**News segment source USPs (analyzed from real issues):**
- NYT (The Morning): one deep explainer/day, authority. WaPo: scoops but paywalled/monetized.
- Guardian (First Edition): personality/wit, free. Axios AM: "Smart Brevity", speed, free/sponsored.
- Semafor: "Semaform" (fact vs. reporter's view vs. room for disagreement), global editions, transparency.

**Our candidate USPs (aggregator-only advantages):**
1. **Consensus vs. Conflict** — per story: agreed facts vs. how outlets diverge (killer feature).
2. **Bias-balancing / neutrality** — read across the spectrum in one place.
3. **Cross-source salience ranking** — "4/5 outlets led with this."
4. **One inbox, not five** (convenience). 5. **Personalization** (topics/length/tone).
6. **Memory/archive moat** — story timelines, "slow burn" early detection, prediction tracking.

**Creative techniques brainstormed (signature stack):** Consensus Meter, Prediction Ledger (score the
pundits), "What you can ignore today", Dinner-Party Line (shareable), Good-News Close, layered depth
(30s/3min/deep), Steelman of the day, Your Blind Spots, Ask-the-newsletter, mood toggle, honest slow-day.
**Recommended positioning:** *"5 minutes. Every side. Zero doom."*

**Sample issues built** (in chat, from real Aug-7 stories): a full "everything" issue + single-technique
variants (Ultra-Brief, Consensus-only, Deep-dive-only, Voice-only, Neutral-Wire). Verdict: ship A's
ingredients at B's length; lead marketing with Consensus; use Voice as seasoning.
**Legal/ethical guardrail:** stay transformative (summaries + attribution + links back); never republish
full paywalled text. Closest real competitor to study: Ground News.

---

## 11. Key files index
| File | What |
|---|---|
| `PROJECT_CONTEXT.md` | THIS file — master context / team handoff |
| `RESUME.md` | Quick resume prompt / project summary |
| `data/sources.csv` | Live per-newsletter tracker (status, addresses, labels) |
| `data/category_feeds.csv` | 9 segments → +tag → label mapping |
| `PHASE4_DESIGN.md` | Full Phase 4/5 archive + LLM-enrichment design + DB schema |
| `NEWSLETTER_SCHEDULE.md` | Per-newsletter frequency, send day, expected delivery |
| `SIGNUP_SEQUENCE.md` / `FINISH_ACTIONS.md` | Signup + confirm/redo checklists (mostly historical now) |
| `GMAIL_SETUP.md` | How the 9 Gmail filters/labels were set up |
| `gmail_filters.xml` / `gmail_filters_sender.xml` / `gmail_filter_lawfare.xml` | Importable Gmail filters |
| `tools/google-skill/` | Cloned Gmail-API tool (auth + list/read). Secrets are local, not committed. |
| `data/archive/` | The newsletter archive (.eml + .md by segment/date). 1 proof file so far. |
| `pipeline/` | **The pipeline code** (Python pkg `nlagg`): capture now; split/enrich/cluster/compose next. See `pipeline/README.md` |
| `legacy/ktn/` | KTN-era Python/Playwright — OBSOLETE, kept for reference |
| `scripts/session_log.py` | SessionEnd hook that appends to this file's Session Log |

---

## 12. Open questions / next steps
**Pipeline plan (2026-10-04)** — milestones, each a small PR. First segment end-to-end: **1-TechAI**
(5/5 confirmed, 3 overlapping dailies → clustering testable fast, lower paywall risk than News).
- **M0 ✅** repo setup, `pipeline/` package, config, schema v2 (items/stories/issues_out).
- **M1 ✅ code / ⏳ real run** capture: Gmail → `.eml` + `messages` (api | imap | file backends, idempotent).
  → Run `python -m nlagg capture` on the machine with Gmail auth; check `nlagg stats` vs Gmail label counts.
- **M2** clean HTML (strip tracking/pixels), sponsor detection, split issues into `items`
  (roundups first, then sectioned briefs, essays = 1 item, teasers → fetch "read online"). Done = ≥90% correct on 20 TechAI emails.
- **M3** per-item extraction with Groq (small model) / local Qwen; pydantic validation; cache by content_hash+prompt_version.
- **M4** local embeddings → cluster items within 48h into `stories`; salience = distinct sources; consensus vs divergence.
- **M5** compose: hook · top story · "Everyone's talking about" · quick hits · "Safe to skip" · close;
  exemplar-based style prompt; **citation validator** (every sentence cites item ids; reject unknown names/numbers).
- **M6** human approve → send to team only (Buttondown/Beehiiv later). **M7** daily schedule + source-health alerts.
- **M8** more segments by config (Biz, GitHub next; News last).

Still open from before:
1. Pick the Phase 5 LLM for real (Groq to start vs local Qwen).
2. Build a sender → newsletter map (`data/senders.csv`) from the first real `nlagg stats` run.
3. Decide whether to trim bonus subscriptions (Guardian/Semafor editions, Substack recs inflate volume).
4. Confirm the 2 pending manual steps (RedChip code, Bootstrapped Founder confirm).
5. **Scheduled runs:** OAuth app in "Testing" → refresh token expires ~7 days. Use IMAP + App Password
   for the scheduled job, or publish the OAuth app.

## 13. How this file stays current (the "auto-save context" ask)
This file is the durable memory across sessions and for the team. To keep it fresh:
- **At the end of each session, append a dated entry to the Session Log below** summarizing what was
  discussed/decided/changed. (Ask Claude: "update PROJECT_CONTEXT.md with this session.")
- For true hands-off automation, a Claude Code **Stop hook** can be configured in `.claude/settings.json`
  to prompt a session summary — ask Claude to "set up a hook to append session summaries to
  PROJECT_CONTEXT.md" (uses the update-config skill).
- Keep `data/sources.csv` as the live status source; keep design in `PHASE4_DESIGN.md`.

---

## 14. SESSION LOG (append newest at top)

### Session 2026-10-04 — Pipeline plan + M0/M1 (hritvik, branch `hritvik/pipeline-m0-m1`)
- Reviewed repo + 7 reference repos (run-llama, projectgreenhat, AI-Weekly-Digest, news-digest, asadcs, …).
  Borrowed: exemplar style prompts, tool-vs-LLM split, link verification, draft-first delivery, Actions scheduling.
- **Decisions:** build in this repo (no new repo); Python pipeline in `pipeline/`; **story item** (not email) is the
  unit of analysis; start with **1-TechAI**; small model for extraction, big model only for writing; citation validator.
- **M0:** `pipeline/` package (`nlagg`), `config.yaml`, `.env.example`, schema v2 (see PHASE4_DESIGN.md "v2 additions");
  KTN scripts → `legacy/ktn/`; RESUME.md replaced with a pointer.
- **M1:** capture implemented — Gmail API (reuses google-skill token), IMAP (App Password), and .eml-folder backends;
  same hex gmail_id in all; idempotent; incremental with 3-day overlap; sender-override + `+tag` routing; is_issue;
  platform detection; `sync_log`; `nlagg stats` source-health view. 17 tests (fake Gmail API/IMAP), all passing.
- **Not done:** no run against the real inbox yet (needs the machine with Gmail auth).
- **Note:** repo now lives at `hritvikgarg/newsletter-aggregator` (copy of `KavyaJain321/newsletter-aggregator`);
  CONTRIBUTING.md still points at Kavya's — agree on ONE canonical repo.

### Session 2026-08-06/07 — Setup verification, Gmail API, archive design, product strategy
- Pivoted fully from KTN → Gmail +tags (done earlier); this session focused on verification + design.
- Set up **Gmail API read access** via google-skill: created own Google Cloud OAuth (project
  `newsletter-reader`), narrowed scope to gmail.readonly, authenticated successfully.
- **Live-verified subscriptions** repeatedly via the API. Result: ~39–41/45 active; identified 4 dead
  (Workology, Hard Times, Small Cap Discoveries, OTC Adventures); confirmed SHRM, Bytes, FindLaw, NLR,
  SCOTUSblog, Babylon Bee, Indie Hackers, Zacks are delivering. Updated `data/sources.csv` throughout.
- Produced `NEWSLETTER_SCHEDULE.md` (frequency/send-day/expected delivery for all 45).
- **Audited content structure** of 70 real issues → 5 content shapes; ~52% links-roundup, ~35% brief.
  Built + ran throwaway analysis scripts (cleaned up after).
- **Proved Phase-4 capture** on 1 real email (Above the Law) → saved .eml + .md to
  `data/archive/3-Legal/2026-08-06/`.
- **Designed Phase 4 (archive) + Phase 5 (LLM enrichment)** → wrote `PHASE4_DESIGN.md`. Key decisions:
  SQLite + .eml + .md; capture is AI-free; enrichment is **LLM-agnostic (Groq/local Qwen, NEVER Claude)**;
  fetch via Gmail API (IMAP is a viable alt). Full DB schema defined.
- **Product strategy** for the News segment: analyzed each source's USP from real issues; defined our
  aggregator USPs (Consensus vs Conflict, neutrality, memory moat); brainstormed creative techniques;
  built sample News issues (full + single-technique variants) for quality review.
- Created **this `PROJECT_CONTEXT.md`** as the team handoff / cross-session memory.
- **Next:** build the real capture script + backfill; set daily sync; pick Phase-5 LLM; finalize digest template.

<!-- Add the next session's summary above this line -->
