# Newsletter Aggregator

An automated pipeline that **subscribes one Gmail to ~45 newsletters across 9 topic segments**,
auto-sorts them, archives every issue into a structured store, and (in progress) uses a
**non-Claude LLM** (Groq / local Qwen) to summarize them into our own digest newsletters.

> **👉 New here? Read [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md) first.** It's the full handoff:
> every decision, current status, how the Gmail API access works, the DB design, and the
> product strategy. It also has a **Session Log** recording what each work session discussed.

---

## Team workflow (how we collaborate)

We work in **separate branches** and share changes via **pull requests**. Each person runs their
own Claude Code session; we stay in sync through the repo + `PROJECT_CONTEXT.md`, and review each
other's work in PRs. **Full details: [`CONTRIBUTING.md`](CONTRIBUTING.md).**

**The loop:**
1. `git checkout main && git pull` — get the latest.
2. `git checkout -b <yourname>/<topic>` — your own branch.
3. Read [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md) (esp. the **Session Log**), then do your work.
4. Ask Claude: *"update PROJECT_CONTEXT.md with this session"* when it matters.
5. `git commit` → `git push -u origin <yourname>/<topic>` → **open a PR into `main`**.
6. The other person reviews the PR's **Files changed** diff and merges it.

Keep `data/sources.csv` as the live status tracker; keep design in `PHASE4_DESIGN.md`.
GitHub does **not** stream live Claude Code chats — the durable discussion record is the
`PROJECT_CONTEXT.md` Session Log, which travels with each PR.

---

## First-time setup (each machine needs its own Gmail access)

Secrets are **never** committed. Each teammate authenticates their own Gmail API access:

1. Install Node.js (v20+) and `pnpm`.
2. Set up the Gmail tool:
   ```bash
   cd tools/google-skill
   pnpm install
   # Provide OWN Google Cloud OAuth creds at ~/.config/google-skill/credentials.json
   # (Google Cloud project → enable Gmail API → OAuth Desktop client → download JSON)
   npx tsx skills/gmail/scripts/gmail.ts auth   # opens browser, read-only Gmail scope
   ```
   Full steps: see **Section 6** of [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md).
3. Read the inbox:
   ```bash
   npx tsx skills/gmail/scripts/gmail.ts list --query="in:anywhere newer_than:2d" --max=50
   ```

The Gmail API is **free**. The capture/parse pipeline uses **no AI**; only the enrichment step (Phase 5)
uses an LLM, and that is **Groq or local Qwen — never Claude**.

---

## Repo map
| Path | What |
|---|---|
| [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md) | Master context / team handoff / session log |
| [`PHASE4_DESIGN.md`](PHASE4_DESIGN.md) | Archive + LLM-enrichment design + full DB schema |
| [`NEWSLETTER_SCHEDULE.md`](NEWSLETTER_SCHEDULE.md) | Per-newsletter frequency & expected delivery |
| `data/sources.csv` | Live per-newsletter status tracker |
| `data/category_feeds.csv` | 9 segments → +tag → label mapping |
| `gmail_filters*.xml` | Importable Gmail filters |
| `pipeline/` | **Pipeline code** (Python `nlagg`): capture → split → enrich → cluster → compose. See `pipeline/README.md` |
| `tools/google-skill/` | Gmail-API tool (secrets & node_modules are gitignored) |
| `legacy/ktn/` | Retired Kill-the-Newsletter scripts (reference only) |
| `data/archive/` | Local newsletter archive (.eml + .md) — **gitignored, stays local** |

---

## Status (2026-10-04)
Phases 1–3 done: **38/45 confirmed delivering**, 3 pending, 4 dropped (`data/sources.csv`).
Pipeline: **M0 setup + M1 capture code done** (`pipeline/`), awaiting the first real-inbox run.
Next: M2 split issues into stories → M3 LLM extraction → M4 cross-source clustering → M5 our digest,
starting with the 1-TechAI segment. Plan: `PROJECT_CONTEXT.md` §12.

Quick start for the pipeline:
```bash
cd pipeline && pip install -e ".[gmail,dev]" && pytest -q
python -m nlagg capture --dry-run     # on the machine with Gmail auth
```
