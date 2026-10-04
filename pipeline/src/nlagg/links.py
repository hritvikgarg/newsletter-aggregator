"""Resolve opaque click-trackers (beehiiv / Substack / ConvertKit / Mailchimp ...) to their destination.

Decided 2026-10-04: only when needed — once per URL, cached in `link_resolution`, and only for items that
make it into our issue (every request counts as a click for the sender). Redirects are followed by hand
(max 6 hops) with HEAD first, then GET if the server refuses HEAD. Unresolvable links keep the tracker URL.
"""
from __future__ import annotations

import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .clean import canonical_url, link_type

log = logging.getLogger("nlagg.links")
MAX_HOPS = 6


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def _default_fetch(url: str, method: str) -> tuple[int, str | None]:
    """(status, Location header) without following redirects."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, method=method, headers={"User-Agent": "Mozilla/5.0 (nlagg link check)"})
    try:
        with opener.open(req, timeout=15) as r:
            return r.status, r.headers.get("Location")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location") if e.headers else None


def resolve_url(url: str, fetch=_default_fetch) -> str | None:
    cur = url
    for _ in range(MAX_HOPS):
        try:
            status, loc = fetch(cur, "HEAD")
            if status in (403, 405, 501) or (status >= 400 and not loc):
                status, loc = fetch(cur, "GET")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            log.info("resolve %s failed: %s", cur, e)
            return None
        if status in (301, 302, 303, 307, 308) and loc:
            cur = urllib.parse.urljoin(cur, loc)
            if link_type(cur) != "tracked":
                return canonical_url(cur)
            continue
        if 200 <= status < 300:
            return canonical_url(cur) if cur != url else None   # no redirect: not a tracker after all
        return None
    return None


def resolve_for_items(conn, item_ids: list[int], fetch=_default_fetch) -> dict[int, str]:
    """Return {item_id: best url}: the item's URL, with a tracked one replaced by its cached/resolved target."""
    out: dict[int, str] = {}
    if not item_ids:
        return out
    rows = conn.execute(f"SELECT id, url FROM items WHERE id IN ({','.join('?' * len(item_ids))})", item_ids).fetchall()
    for r in rows:
        url = r["url"]
        if not url:
            continue
        if link_type(url) != "tracked":
            out[r["id"]] = url
            continue
        hit = conn.execute("SELECT final_url, status FROM link_resolution WHERE url = ?", (url,)).fetchone()
        if hit is None:
            final = resolve_url(url, fetch)
            with conn:
                conn.execute("INSERT OR REPLACE INTO link_resolution (url, final_url, status, resolved_at) VALUES (?,?,?,?)",
                             (url, final, "ok" if final else "failed",
                              datetime.now(timezone.utc).isoformat(timespec="seconds")))
        else:
            final = hit["final_url"]
        out[r["id"]] = final or url
    return out
