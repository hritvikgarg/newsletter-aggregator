# Newsletter Aggregator — Resume

> This file used to be a paste-in resume prompt. It went stale (it still said "Phase 4 not started"
> and pointed at an old local path), and `CLAUDE.md` now auto-loads shared context in every session.

To resume work:
1. `git checkout main && git pull`
2. Read **`PROJECT_CONTEXT.md`** — full state, decisions, and the Session Log (newest first).
3. Pipeline code and how to run it: **`pipeline/README.md`**.
4. Live per-newsletter status: `data/sources.csv`. Design + DB schema: `PHASE4_DESIGN.md`.

Notes kept from the old file:
- The Kill-the-Newsletter (KTN → RSS) approach was retired on 2026-08-02; its scripts now live in
  `legacy/ktn/` for reference only (`phase3_next_check.py` there writes the OLD sources.csv columns — don't run it).
- `scripts/session_log.py` is the live SessionEnd hook (local settings) and stays in `scripts/`.
