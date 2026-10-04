# CLAUDE.md — read this first (auto-loaded every session)

You are working on the **Newsletter Aggregator** project. This file loads into every session
(any teammate, cloud or local), so it's the shared starting point.

## Get oriented before doing anything
**Read [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md)** — it's the full state, every decision, and a
**Session Log** of what past sessions did. That's how you "continue" the shared thread of work.
Live per-newsletter status is in `data/sources.csv`. Design is in `PHASE4_DESIGN.md`.

## Hard rules
- **Never commit secrets.** Gmail OAuth token/credentials are gitignored and machine-local.
  `~/.config/google-skill/credentials.json` and `.claude/*.local.json` must never be pushed.
- **Gmail access is LOCAL-only.** Only a session with the authenticated `tools/google-skill` tool
  can read `notifyy1008@gmail.com`. Cloud/teammate sessions usually can't — don't wait on Gmail there.
  Do Gmail work in the local session, push results (e.g. `data/sources.csv`), teammates `git pull`.
- **No Claude/AI in the capture pipeline.** Phase 5 enrichment uses Groq or local Qwen — never Claude.

## How we collaborate (see CONTRIBUTING.md for detail)
- Teammates: work on a **branch**, push, open a **PR** — the owner reviews PRs.
- **Owner exception (hritvik, 2026-10-05):** while the pipeline is being built, the owner's sessions push
  straight to `main` — only after the full test suite passes; never force-push.
- When a session did meaningful work, **append a note to `PROJECT_CONTEXT.md`** (Session Log) and
  include it in your PR (or push), so everyone sees what was discussed/decided.

## Where things stand (2026-10-05)
Phases 1–3 done (38/45 confirmed; see `sources.csv`). Pipeline code lives in **`pipeline/`** (Python `nlagg`):
M0–M2 done on real mail (3,179 captured; 1-TechAI split into ~2,300 items). M3–M7 code done (extract → cluster →
compose with citation checker → approve/send → daily run). Blocked only on the owner adding `GROQ_API_KEY`
and `delivery.recipients`; then first real draft + quality review, then schedule. LLM calls need normal internet
(Windows), not the cloud workspace. Plan + milestones: `PROJECT_CONTEXT.md` §12. How to run: `pipeline/README.md`.
