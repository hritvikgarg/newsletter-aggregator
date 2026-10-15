"""AI-generated card images (owner decision 2026-10-15: our own images only, no publisher photos).

The writer gives each story a short visual brief; we add one house style so every issue looks like the same
publication, generate the image, and save it next to the issue (out/issues/<segment>/img/). The HTML file uses the
relative path; `nlagg approve` embeds the files in the email (inline attachments), so nothing is hot-linked.

Providers (config `images:`):
  pollinations       GET  {base_url}/image/{prompt}?width=&height=&seed=&model=&nologo=true   key optional
  openai_compatible  POST {base_url}/images/generations {model, prompt, size, response_format: b64_json}
                     (Together, Nebius, a local server ... anything speaking that API)
  cloudflare         POST https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{model}
                     Workers AI free daily allowance; model e.g. @cf/black-forest-labs/flux-1-schnell
Never Claude/Anthropic (team rule). A failed image is not an error: the card falls back to a colour tile.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .llm import FORBIDDEN

log = logging.getLogger("nlagg.images")

DEFAULT_STYLE = ("editorial 3D illustration, soft studio lighting, deep indigo and violet palette with one warm "
                 "highlight, clean minimal composition, high detail, no text, no letters, no words, no logos, "
                 "no watermark, no recognisable real people")
SAFE_RE = re.compile(r"\b(logo|text|caption|letters?|words?|signature|watermark)\b", re.I)


@dataclass
class ImageSettings:
    provider: str = "pollinations"
    base_url: str = "https://gen.pollinations.ai"
    model: str = "flux"
    api_key: str | None = None
    account_id: str | None = None
    style: str = DEFAULT_STYLE
    width: int = 1200
    height: int = 630
    timeout_s: float = 120.0

    @classmethod
    def from_config(cls, comp: dict) -> "ImageSettings":
        im = comp.get("ai_images", {}) or {}
        s = cls(provider=im.get("provider", "pollinations"), base_url=str(im.get("base_url", cls.base_url)).rstrip("/"),
                model=im.get("model", "flux"), api_key=os.environ.get(im.get("api_key_env", "IMAGE_API_KEY")) or None,
                style=im.get("style", DEFAULT_STYLE), width=int(im.get("width", 1200)),
                height=int(im.get("height", 630)),
                account_id=os.environ.get(im.get("account_id_env", "CF_ACCOUNT_ID")) or None)
        for v in (s.base_url, s.model, s.provider):
            if FORBIDDEN.search(v):
                raise SystemExit(f"ai_images: '{v}' — Claude/Anthropic is not allowed in the pipeline (team rule)")
        return s


def full_prompt(brief: str, style: str) -> str:
    brief = SAFE_RE.sub("", re.sub(r"\s+", " ", brief or "")).strip(" ,.")
    return f"{brief}. {style}"[:900]


def _http_get(url: str, headers: dict, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "nlagg/0.1", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type") or ""
        data = r.read(15_000_000)
    if not ctype.startswith("image/") and not data[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"RIFF"):
        raise ValueError(f"not an image ({ctype or 'unknown type'}): {data[:120]!r}")
    return data


def _http_post_json(url: str, body: dict, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "nlagg/0.1", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def generate(prompt: str, s: ImageSettings, seed: int, *, get=_http_get, post=_http_post_json) -> bytes:
    auth = {"Authorization": f"Bearer {s.api_key}"} if s.api_key else {}
    if s.provider == "pollinations":
        q = {"width": s.width, "height": s.height, "seed": seed, "model": s.model, "nologo": "true"}
        url = f"{s.base_url}/image/{urllib.parse.quote(prompt, safe='')}?{urllib.parse.urlencode(q)}"
        return get(url, auth, s.timeout_s)
    if s.provider == "openai_compatible":
        j = post(f"{s.base_url}/images/generations",
                 {"model": s.model, "prompt": prompt, "size": f"{s.width}x{s.height}", "n": 1,
                  "response_format": "b64_json"}, auth, s.timeout_s)
        d = (j.get("data") or [{}])[0]
        if d.get("b64_json"):
            return base64.b64decode(d["b64_json"])
        if d.get("url"):
            return get(d["url"], {}, s.timeout_s)
        raise ValueError(f"no image in reply: {str(j)[:200]}")
    if s.provider == "cloudflare":
        if not s.account_id or not s.api_key:
            raise ValueError("cloudflare needs CF_ACCOUNT_ID and IMAGE_API_KEY (an API token with Workers AI) in .env")
        base = s.base_url if "cloudflare.com" in s.base_url else "https://api.cloudflare.com/client/v4"
        j = post(f"{base}/accounts/{s.account_id}/ai/run/{s.model}",
                 {"prompt": prompt, "steps": 6, "seed": seed}, auth, s.timeout_s)
        img = (j.get("result") or {}).get("image")
        if not img:
            raise ValueError(f"no image in reply: {str(j)[:200]}")
        return base64.b64decode(img)
    raise SystemExit(f"unknown ai_images.provider '{s.provider}' (pollinations | openai_compatible | cloudflare)")


def _ext(data: bytes) -> str:
    return "png" if data[:4] == b"\x89PNG" else ("webp" if data[:4] == b"RIFF" else "jpg")


def make_images(briefs: dict[str, str], out_dir: Path, s: ImageSettings, **kw) -> dict[str, str]:
    """{card key: brief} -> {card key: path relative to the issue file ('img/<hash>.jpg')}.
    Same prompt -> same file (re-composing an issue does not pay twice)."""
    out: dict[str, str] = {}
    img_dir = out_dir / "img"
    for key, brief in briefs.items():
        if not brief:
            continue
        prompt = full_prompt(brief, s.style)
        h = hashlib.sha256(f"{s.provider}|{s.model}|{prompt}".encode()).hexdigest()[:16]
        existing = sorted(img_dir.glob(f"{h}.*")) if img_dir.exists() else []
        if existing:
            out[key] = f"img/{existing[0].name}"
            continue
        try:
            data = generate(prompt, s, int(h[:6], 16), **kw)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as e:
            log.warning("image for %s failed: %s", key, e)
            continue
        img_dir.mkdir(parents=True, exist_ok=True)
        path = img_dir / f"{h}.{_ext(data)}"
        path.write_bytes(data)
        out[key] = f"img/{path.name}"
    return out
