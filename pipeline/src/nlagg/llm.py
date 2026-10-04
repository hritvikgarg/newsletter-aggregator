"""OpenAI-compatible chat client (Groq, Ollama, any /v1/chat/completions endpoint). Never Claude.

Standard library only (urllib), so the scheduled job needs no extra packages.
- JSON mode (`response_format: json_object`) and a parse/repair step: callers always get a dict.
- Retries on 429 / 5xx / network errors, honouring `Retry-After`; a minimum gap between requests
  keeps us under free-tier rate limits.
- Cache: identical (model, prompt_version, messages) never hits the API twice (table llm_cache).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger("nlagg.llm")

FORBIDDEN = re.compile(r"anthropic|claude", re.I)     # team rule: no Claude in the pipeline


class LLMError(RuntimeError):
    pass


@dataclass
class LLMSettings:
    base_url: str
    api_key: str | None
    extract_model: str
    write_model: str
    prompt_version: str = "v1"
    max_retries: int = 3
    min_interval_s: float = 2.5      # ~24 requests/min: below Groq's free-tier 30 RPM
    timeout_s: float = 90.0
    provider: str = "groq"

    @classmethod
    def from_config(cls, llm: dict) -> "LLMSettings":
        env = llm.get("api_key_env", "GROQ_API_KEY")
        s = cls(base_url=llm.get("base_url", "https://api.groq.com/openai/v1").rstrip("/"),
                api_key=os.environ.get(env) or None,
                extract_model=llm.get("extract_model", "openai/gpt-oss-20b"),
                write_model=llm.get("write_model", "openai/gpt-oss-120b"),
                prompt_version=str(llm.get("prompt_version", "v1")),
                max_retries=int(llm.get("max_retries", 3)),
                min_interval_s=float(llm.get("min_interval_s", 2.5)),
                provider=llm.get("provider", "groq"))
        for v in (s.base_url, s.extract_model, s.write_model, s.provider):
            if FORBIDDEN.search(v):
                raise SystemExit(f"config llm: '{v}' — Claude/Anthropic is not allowed in the pipeline (team rule)")
        return s


def _duration(v: str) -> float | None:
    """'7.66s' / '1m2.5s' / '120ms' / '3' -> seconds."""
    v = (v or "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        pass
    total, found = 0.0, False
    for num, unit in re.findall(r"([\d.]+)(ms|h|m|s)", v):
        found = True
        total += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total if found else None


def _retry_delay(headers: dict, payload: str) -> float | None:
    d = _duration(headers.get("retry-after", ""))
    if d:
        return d
    m = re.search(r"try again in ([\dhms.]+)", payload or "")
    return _duration(m.group(1)) if m else None


def is_reasoning_model(model: str) -> bool:
    return bool(re.search(r"gpt-oss|qwen3|deepseek-r1|reason", model or "", re.I))


def parse_json(text: str) -> dict:
    """Parse a model reply as a JSON object (tolerates ```json fences and text around the object)."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        v = json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            raise LLMError(f"reply is not JSON: {text[:200]!r}")
        try:
            v = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise LLMError(f"reply is not valid JSON ({e}): {text[:200]!r}") from e
    if not isinstance(v, dict):
        raise LLMError("reply JSON is not an object")
    return v


class ChatClient:
    """`chat_json(model, messages)` -> dict. `transport` is injectable for tests."""

    def __init__(self, settings: LLMSettings, conn: sqlite3.Connection | None = None, transport=None,
                 sleep=time.sleep, clock=time.monotonic):
        self.s = settings
        self.conn = conn
        self.transport = transport or self._http
        self.sleep, self.clock = sleep, clock
        self._last = 0.0
        self._pause_until = 0.0
        self.calls = 0          # API calls made (cache hits excluded)
        self.cache_hits = 0
        self.tokens = 0

    # ------------------------------------------------------------------ public
    def chat_json(self, model: str, messages: list[dict], *, temperature: float = 0.2,
                  max_tokens: int = 1200, use_cache: bool = True) -> dict:
        key = self._key(model, messages, temperature)
        if use_cache and self.conn is not None:
            row = self.conn.execute("SELECT response FROM llm_cache WHERE key = ?", (key,)).fetchone()
            if row:
                self.cache_hits += 1
                return json.loads(row[0])
        body = {"model": model, "messages": messages, "temperature": temperature,
                "max_tokens": max_tokens, "response_format": {"type": "json_object"}}
        if is_reasoning_model(model):
            # gpt-oss / qwen3 "think" before answering and the thinking counts against max_tokens:
            # keep it short and leave room for the JSON.
            body["reasoning_effort"] = "low"
            body["max_tokens"] = max_tokens + 2000
        try:
            reply = self._call(body)
        except LLMError as e:
            if "API error 400" not in str(e) or not re.search(r"response_format|json|reasoning_effort", str(e), re.I):
                raise
            # some models reject JSON mode / reasoning_effort: ask again without them (the prompt asks for JSON)
            body.pop("response_format", None)
            body.pop("reasoning_effort", None)
            reply = self._call(body)
        data = parse_json(reply)
        if self.conn is not None:
            self.conn.execute(
                "INSERT OR REPLACE INTO llm_cache (key, model, prompt_version, response, created_at) VALUES (?,?,?,?,?)",
                (key, model, self.s.prompt_version, json.dumps(data, ensure_ascii=False),
                 datetime.now(timezone.utc).isoformat(timespec="seconds")))
            self.conn.commit()
        return data

    # ------------------------------------------------------------------ internals
    def _key(self, model: str, messages: list[dict], temperature: float) -> str:
        blob = json.dumps([model, self.s.prompt_version, temperature, messages], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _call(self, body: dict) -> str:
        if FORBIDDEN.search(body["model"]):
            raise LLMError("Claude/Anthropic models are not allowed in the pipeline")
        last_err: Exception | None = None
        attempt = rate_waits = 0
        while True:
            wait = max(self.s.min_interval_s - (self.clock() - self._last), self._pause_until - self.clock())
            if wait > 0:
                self.sleep(wait)
            self._last = self.clock()
            try:
                status, headers, payload = self.transport(body)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err, status, headers, payload = e, 0, {}, ""
            headers = {k.lower(): v for k, v in (headers or {}).items()}
            if status == 200:
                self.calls += 1
                self._pace(headers)
                try:
                    j = json.loads(payload)
                    self.tokens += int((j.get("usage") or {}).get("total_tokens") or 0)
                    return j["choices"][0]["message"]["content"] or ""
                except (KeyError, IndexError, json.JSONDecodeError) as e:
                    raise LLMError(f"unexpected API reply: {payload[:300]}") from e
            if status in (400, 401, 403, 404):
                hint = ""
                if "model_not_found" in payload or "does not exist" in payload:
                    hint = " — run `python -m nlagg models` and set llm.extract_model / write_model in config.yaml"
                raise LLMError(f"API error {status}: {payload[:300]}{hint}")
            if status == 429 and rate_waits < 12 and "per day" not in payload.lower():
                # per-minute limit: wait it out (doesn't count as a failure)
                rate_waits += 1
                delay = _retry_delay(headers, payload) or 5.0
                log.info("rate limit (per minute); waiting %.1fs", delay)
                self.sleep(min(delay + 0.5, 90.0))
                continue
            if status:
                last_err = LLMError(f"API error {status}: {payload[:200]}")
            if attempt >= self.s.max_retries:
                break
            delay = _retry_delay(headers, payload) or 2.0 * (2 ** attempt)
            log.warning("LLM call failed (%s); retry %d in %.0fs", last_err, attempt + 1, delay)
            self.sleep(min(delay, 120.0))
            attempt += 1
        raise LLMError(f"LLM call failed after {self.s.max_retries + 1} attempts: {last_err}")

    def _pace(self, headers: dict) -> None:
        """Groq reports the per-minute token budget left; pause before it runs out instead of hitting 429."""
        try:
            left = float(headers.get("x-ratelimit-remaining-tokens", "inf"))
        except ValueError:
            return
        if left < 3000:
            reset = _duration(headers.get("x-ratelimit-reset-tokens", "")) or 10.0
            self._pause_until = self.clock() + reset

    def list_models(self) -> list[str]:
        """Model ids this key can use (GET /models)."""
        if not self.s.api_key and "localhost" not in self.s.base_url:
            raise SystemExit("LLM API key missing: set GROQ_API_KEY in pipeline/.env (see .env.example)")
        req = urllib.request.Request(self.s.base_url + "/models", headers={
            "User-Agent": "nlagg/0.1", **({"Authorization": f"Bearer {self.s.api_key}"} if self.s.api_key else {})})
        try:
            with urllib.request.urlopen(req, timeout=self.s.timeout_s) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raise LLMError(f"API error {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from e
        return sorted(m.get("id", "") for m in data.get("data", []) if not FORBIDDEN.search(m.get("id", "")))

    def _http(self, body: dict) -> tuple[int, dict, str]:
        if not self.s.api_key and "localhost" not in self.s.base_url and "127.0.0.1" not in self.s.base_url:
            raise SystemExit("LLM API key missing: set GROQ_API_KEY in pipeline/.env (see .env.example)")
        req = urllib.request.Request(
            self.s.base_url + "/chat/completions", data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "nlagg/0.1",
                     **({"Authorization": f"Bearer {self.s.api_key}"} if self.s.api_key else {})})
        try:
            with urllib.request.urlopen(req, timeout=self.s.timeout_s) as r:
                return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), e.read().decode("utf-8", "replace")
