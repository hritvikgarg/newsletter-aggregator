"""M7 — the daily run: capture -> split -> extract -> cluster -> compose -> health check -> tell the owner.

Each step is isolated: a failing step is logged and the run continues where that still makes sense
(no new mail is not an error; a failed capture still lets yesterday's mail be composed). The issue stays a
DRAFT — sending needs `nlagg approve`. A lock file stops two runs overlapping.
Log: pipeline/out/logs/daily-<date>.log. Health: picked newsletters that went quiet.
"""
from __future__ import annotations

import logging
import os
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .config import Config

log = logging.getLogger("nlagg.daily")


@dataclass
class DailyResult:
    steps: dict[str, str] = field(default_factory=dict)       # step -> "ok: ..." | "failed: ..."
    alerts: list[str] = field(default_factory=list)
    issue_path: str | None = None
    log_path: Path | None = None

    @property
    def ok(self) -> bool:
        return not any(v.startswith("failed") for v in self.steps.values())


def quiet_sources(cfg: Config, quiet_days: int = 4, lookback_days: int = 30, now: datetime | None = None) -> list[str]:
    """Newsletters (sender) of the active segments that sent issues in the last month but none recently."""
    now = now or datetime.now(timezone.utc)
    conn = db.connect(cfg.db_path)
    segs = cfg.active_segments or ["1-TechAI"]
    rows = conn.execute(
        f"""SELECT segment, sender_email, MAX(sent_date) last, COUNT(*) n FROM messages
            WHERE is_issue = 1 AND segment IN ({','.join('?' * len(segs))}) AND sent_date >= ?
            GROUP BY segment, sender_email""", (*segs, (now - timedelta(days=lookback_days)).isoformat())).fetchall()
    conn.close()
    cutoff = (now - timedelta(days=quiet_days)).isoformat()
    return [f"{r['segment']}: {r['sender_email']} — nothing since {r['last'][:10]} ({r['n']} issues in {lookback_days} d)"
            for r in rows if r["last"] < cutoff and r["n"] >= 3]


def run_daily(cfg: Config, *, backend=None, client=None, now: datetime | None = None, smtp_factory=None,
              skip_capture: bool = False) -> DailyResult:
    now = now or datetime.now(timezone.utc)
    d = cfg.raw.get("daily", {})
    out_dir = cfg.repo_root / "pipeline" / "out" / "logs"
    out_dir.mkdir(parents=True, exist_ok=True)
    res = DailyResult(log_path=out_dir / f"daily-{now.date().isoformat()}.log")
    lock = out_dir / "daily.lock"
    if lock.exists() and (datetime.now().timestamp() - lock.stat().st_mtime) < 3 * 3600:
        res.steps["lock"] = "failed: another daily run is in progress (pipeline/out/logs/daily.lock)"
        return res
    lock.write_text(str(os.getpid()), encoding="utf-8")
    handler = logging.FileHandler(res.log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger("nlagg").addHandler(handler)
    logging.getLogger("nlagg").setLevel(logging.INFO)
    try:
        _steps(cfg, res, d, backend, client, now, skip_capture)
        quiet = quiet_sources(cfg, int(d.get("health_quiet_days", 4)), now=now)
        res.alerts += [f"quiet newsletter: {q}" for q in quiet]
        res.steps["health"] = f"ok: {len(quiet)} quiet newsletter(s)"
        _notify(cfg, res, now, smtp_factory)
    finally:
        for k, v in res.steps.items():
            log.info("step %-8s %s", k, v)
        logging.getLogger("nlagg").removeHandler(handler)
        handler.close()
        lock.unlink(missing_ok=True)
    return res


def _step(res: DailyResult, name: str, fn) -> object | None:
    try:
        out = fn()
        return out
    except SystemExit as e:                       # config problems (missing key/password) arrive as SystemExit
        res.steps[name] = f"failed: {e}"
    except Exception as e:                        # noqa: BLE001 — a daily job must not die on one step
        res.steps[name] = f"failed: {type(e).__name__}: {e}"
        log.error("step %s failed:\n%s", name, traceback.format_exc())
    return None


def _steps(cfg: Config, res: DailyResult, d: dict, backend, client, now: datetime, skip_capture: bool) -> None:
    from .capture import run_capture
    from .cluster import run_cluster
    from .compose import run_compose
    from .extract import run_extract
    from .split_run import run_split

    if skip_capture:
        res.steps["capture"] = "ok: skipped"
    else:
        def capture():
            b = backend
            if b is None:
                name = d.get("capture_backend", "imap")
                if name == "imap":
                    from .fetchers import ImapBackend
                    b = ImapBackend(cfg)
                else:
                    from .fetchers import GmailApiBackend
                    b = GmailApiBackend(cfg)
            try:
                return run_capture(cfg, b)
            finally:
                getattr(b, "close", lambda: None)()
        r = _step(res, "capture", capture)
        if r is not None:
            res.steps["capture"] = f"ok: {r.new_added} new, {r.failed} failed"
            if r.failed:
                res.alerts.append(f"capture: {r.failed} message(s) failed to download")
    r = _step(res, "split", lambda: run_split(cfg, segments=cfg.active_segments))
    if r is not None:
        res.steps["split"] = f"ok: {r.processed} issues -> {r.items} items, {r.failed} failed"
    r = _step(res, "extract", lambda: run_extract(cfg, days=float(d.get("extract_days", 2)), client=client, now=now))
    if r is not None:
        res.steps["extract"] = f"ok: {r.done}/{r.selected} items, {r.failed} failed, {r.tokens:,} tokens"
        if r.failed and not r.done:
            res.steps["extract"] = f"failed: all {r.failed} items failed ({(r.errors or ['?'])[0][:160]})"
    for seg in cfg.active_segments or ["1-TechAI"]:
        r = _step(res, "cluster", lambda: run_cluster(cfg, segment=seg, end=now, hours=float(d.get("cluster_hours", 48)),
                                                       client=client))
        if r is not None:
            res.steps["cluster"] = f"ok: {r.items} items -> {r.stories} stories, {r.multi_source} multi-source"
        r = _step(res, "compose", lambda: run_compose(cfg, segment=seg, issue_date=now.date().isoformat(), client=client))
        if r is not None:
            if r.skipped_reason:
                res.steps["compose"] = f"ok: nothing composed ({r.skipped_reason})"
            else:
                res.steps["compose"] = f"ok: draft \"{r.subject}\" ({len(r.dropped)} sentences dropped)"
                res.issue_path = str(r.html_path)


def _notify(cfg: Config, res: DailyResult, now: datetime, smtp_factory) -> None:
    from .deliver import notify_owner
    lines = [f"Daily run {now:%Y-%m-%d %H:%M} UTC — {'OK' if res.ok else 'PROBLEMS'}", ""]
    lines += [f"{k:<8} {v}" for k, v in res.steps.items()]
    if res.alerts:
        lines += ["", "Alerts:"] + [f"- {a}" for a in res.alerts]
    if res.issue_path:
        lines += ["", f"Draft: {res.issue_path}", "Approve and send:  python -m nlagg approve",
                  "Reject:            python -m nlagg reject --reason \"...\""]
    subject = ("Draft ready: " if res.issue_path else "Daily run: ") + ("OK" if res.ok else "needs attention")
    try:
        if notify_owner(cfg, subject, "\n".join(lines), smtp_factory):
            res.steps["notify"] = "ok: owner emailed"
    except Exception as e:                        # noqa: BLE001
        res.steps["notify"] = f"failed: {e}"
