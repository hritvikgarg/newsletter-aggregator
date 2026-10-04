# Team workflow — how we collaborate

We work in **separate branches** and share changes through **pull requests (PRs)**.
Each person runs their **own** Claude Code session; we stay in sync through the repo
(shared files + `PROJECT_CONTEXT.md`) and review each other's work in PRs.

---

## The loop (every time you work)

```
1. git checkout main && git pull            # get the latest
2. git checkout -b <yourname>/<what>         # your own branch, e.g. kavya/phase4-parser
3. …do the work in your Claude Code session…
4. Ask Claude: "update PROJECT_CONTEXT.md with this session"   # keep shared context current
5. git add -A && git commit -m "clear message"
6. git push -u origin <yourname>/<what>      # push YOUR branch (never force-push main)
7. Open a PR into main  (Claude can do this, or use the GitHub UI)
```

Then the other person **reviews the PR on GitHub** to see exactly what changed, comments,
and **merges** when it looks good. After a merge, everyone `git checkout main && git pull`.

## Branch naming
`<yourname>/<short-topic>` — e.g. `kavya/news-digest-template`, `partner/gmail-sync-script`.
One PR = one focused change. Keep them small so they're easy to review.

## Reviewing a teammate's work (what "see his changes" means)
- Open the repo's **Pull requests** tab on GitHub → click the PR.
- The **Files changed** tab is the diff — every line they added/removed.
- The PR description + `PROJECT_CONTEXT.md` Session Log tell you *why*.
- Approve & **Merge** (or request changes). That's the review.

## First-time setup (each machine)
1. `git clone https://github.com/hritvikgarg/newsletter-aggregator.git`  
   (canonical repo since 2026-10-04; the older `KavyaJain321/newsletter-aggregator` copy is no longer the main one)
2. Read **`PROJECT_CONTEXT.md`** top-to-bottom — it's the full handoff.
3. Gmail access is **per-machine** and NOT in the repo (secrets are gitignored). If you need to
   read the inbox, set up your own Gmail auth (see `PROJECT_CONTEXT.md` §6). Note: **Gmail-dependent
   work (fetching/verifying mail) is best done on a machine that already has access**, then pushed —
   cloud sessions usually can't reach Gmail, and that's fine.

## Rules (keep the repo safe)
- **Never commit secrets.** `credentials.json`, `*.local.json` (tokens), `node_modules/`, and
  `data/archive/` are gitignored — keep it that way.
- **Never push straight to `main`** for real changes — use a branch + PR so the other person can review.
  (Exception, owner decision 2026-10-05: the owner's own sessions push to `main` during the pipeline build, tests first.)
- **Don't force-push shared branches.**
- Keep `data/sources.csv` (live status) and `PROJECT_CONTEXT.md` (context + session log) current.

## Seeing each other's *discussions* (not just code)
- The durable record is **`PROJECT_CONTEXT.md` → Session Log**. Ask Claude to append a summary
  when a session matters; it ships with your PR, so the reviewer sees the discussion too.
- (Optional) If you're all in the same claude.ai workspace, you can also share/view sessions there —
  that's a claude.ai account feature, separate from this repo.
