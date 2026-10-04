import json

import pytest

from tests.conftest import FakeChat, seed_items
from nlagg import db
from nlagg.extract import number_in_text, run_extract, validate
from nlagg.llm import ChatClient, LLMError, LLMSettings, parse_json

NOW = "2026-10-05T06:00:00+00:00"
BODY = ('Google announced Gemini 4 Argon on Tuesday. The model scores 74% on the agent benchmark and costs $2.50 '
        'per million tokens. "This is our most capable model yet," said Sundar Pichai.')


def reply_ok(model, messages):
    return {"summary": "Google announced Gemini 4 Argon, scoring 74% on an agent benchmark.",
            "category": "launch", "importance": 5,
            "entities": [{"name": "Google", "type": "company"}, {"name": "Gemini 4 Argon", "type": "model"},
                         {"name": "OpenAI", "type": "company"}],                    # OpenAI: not in text
            "claims": [{"text": "The model scores 74% on the agent benchmark", "type": "number"},
                       {"text": "It costs $9 per million tokens", "type": "number"}],   # 9: not in text
            "stats": [{"value": "74%", "unit": "benchmark score", "description": "agent benchmark"},
                      {"value": "$3B", "unit": "revenue", "description": "made up"}],
            "quotes": [{"text": "This is our most capable model yet", "speaker": "Sundar Pichai"},
                       {"text": "We will win the AI race", "speaker": "Sundar Pichai"}],
            "topics": ["ai models", "google"]}


def test_validate_drops_ungrounded_values():
    ex = validate(reply_ok(None, None), "Gemini 4 Argon\n" + BODY)
    assert [e["name"] for e in ex.entities] == ["Google", "Gemini 4 Argon"]
    assert [c["text"] for c in ex.claims] == ["The model scores 74% on the agent benchmark"]
    assert [s["value"] for s in ex.stats] == ["74%"]
    assert [q["text"] for q in ex.quotes] == ["This is our most capable model yet"]
    assert ex.dropped == 4 and ex.importance == 5 and ex.category == "launch"


def test_validate_coerces_bad_fields():
    ex = validate({"summary": "x", "category": "weird", "importance": "9", "entities": ["Google"]}, BODY)
    assert ex.category == "other" and ex.importance == 5 and ex.entities == [{"name": "Google", "type": "other"}]
    with pytest.raises(LLMError):
        validate({"summary": ""}, BODY)


def test_number_grounding():
    t = "it raised $1,299 and 25 million users"
    assert number_in_text("$1,299", t) and number_in_text("1299", t) and number_in_text("25M", t)
    assert not number_in_text("$26M", t) and not number_in_text("none", t)


def test_run_extract_saves_and_skips(cfg):
    ids = seed_items(cfg, [
        {"key": "g", "sender": "TLDR", "title": "Gemini 4 Argon", "body": BODY, "sent": "2026-10-04T10:00:00+00:00"},
        {"key": "ad", "sender": "TLDR", "title": "Buy our CRM (Sponsor)", "body": BODY, "is_sponsor": 1,
         "sent": "2026-10-04T10:00:00+00:00"},
        {"key": "old", "sender": "TLDR", "title": "Old story", "body": BODY, "sent": "2026-09-01T10:00:00+00:00"},
        {"key": "intro", "sender": "TLDR", "title": None or "x", "body": BODY, "kind": "intro",
         "sent": "2026-10-04T10:00:00+00:00"},
    ])
    from datetime import datetime
    fake = FakeChat(reply_ok)
    r = run_extract(cfg, days=3, client=fake, now=datetime.fromisoformat(NOW))
    assert (r.selected, r.done, r.failed) == (1, 1, 0)          # sponsor, old and intro items skipped
    assert "Newsletter: TLDR" in fake.calls[0][1][1]["content"]
    conn = db.connect(cfg.db_path)
    e = conn.execute("SELECT * FROM item_enrichment WHERE item_id = ?", (ids["g"],)).fetchone()
    assert e["summary"].startswith("Google announced") and e["error"] is None and e["llm_model"] == "openai/gpt-oss-20b"
    assert {r[0] for r in conn.execute("SELECT name FROM entities WHERE item_id=?", (ids["g"],))} == {"Google", "Gemini 4 Argon"}
    assert conn.execute("SELECT COUNT(*) FROM stats WHERE item_id=?", (ids["g"],)).fetchone()[0] == 1
    conn.close()
    # second run: nothing left to do
    assert run_extract(cfg, days=3, client=fake, now=datetime.fromisoformat(NOW)).selected == 0


def test_run_extract_records_failures_and_retries_next_time(cfg):
    from datetime import datetime
    seed_items(cfg, [{"key": "g", "sender": "TLDR", "title": "Gemini", "body": BODY, "sent": "2026-10-04T10:00:00+00:00"}])
    r = run_extract(cfg, client=FakeChat(lambda m, msg: LLMError("reply is not JSON")), now=datetime.fromisoformat(NOW))
    assert (r.done, r.failed) == (0, 1)
    r = run_extract(cfg, client=FakeChat(reply_ok), now=datetime.fromisoformat(NOW))
    assert (r.selected, r.done) == (1, 1)


def test_parse_json_tolerates_fences():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Sure! {"a": 2} hope this helps') == {"a": 2}
    with pytest.raises(LLMError):
        parse_json("no json here")


def _settings(**kw):
    return LLMSettings(base_url="https://api.groq.com/openai/v1", api_key="k", extract_model="m",
                       write_model="w", min_interval_s=0, **kw)


def test_client_retries_429_then_caches(cfg):
    conn = db.connect(cfg.db_path)
    seq = [(429, {"retry-after": "1"}, "rate limited"),
           (200, {}, json.dumps({"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {"total_tokens": 50}}))]
    sent, slept = [], []

    def transport(body):
        sent.append(body)
        return seq.pop(0)

    c = ChatClient(_settings(), conn, transport=transport, sleep=slept.append)
    assert c.chat_json("m", [{"role": "user", "content": "hi"}]) == {"ok": True}
    assert slept == [1.0] and c.calls == 1 and c.tokens == 50
    assert sent[0]["response_format"] == {"type": "json_object"}
    assert c.chat_json("m", [{"role": "user", "content": "hi"}]) == {"ok": True}     # cache hit, no transport call
    assert c.cache_hits == 1 and len(sent) == 2
    conn.close()


def test_client_does_not_retry_auth_errors():
    c = ChatClient(_settings(), None, transport=lambda b: (401, {}, "invalid api key"), sleep=lambda s: None)
    with pytest.raises(LLMError, match="401"):
        c.chat_json("m", [{"role": "user", "content": "hi"}])


def test_claude_is_refused():
    with pytest.raises(SystemExit):
        LLMSettings.from_config({"extract_model": "claude-3-haiku"})
    with pytest.raises(SystemExit):
        LLMSettings.from_config({"base_url": "https://api.anthropic.com/v1"})


def test_unknown_model_stops_early_with_hint(cfg):
    from datetime import datetime
    seed_items(cfg, [{"key": f"g{i}", "sender": "TLDR", "gmail_id": f"m{i}", "title": "Gemini", "body": BODY,
                      "sent": "2026-10-04T10:00:00+00:00"} for i in range(5)])
    calls = []

    def transport(body):
        calls.append(body)
        return 404, {}, '{"error":{"message":"The model does not exist","code":"model_not_found"}}'

    client = ChatClient(_settings(), None, transport=transport, sleep=lambda s: None)
    r = run_extract(cfg, client=client, now=datetime.fromisoformat(NOW))
    assert r.failed == 1 and len(calls) == 1 and "nlagg models" in r.errors[0]


def test_reasoning_models_get_low_effort_and_room(cfg):
    sent = []
    ok = (200, {}, json.dumps({"choices": [{"message": {"content": '{"a": 1}'}}]}))
    c = ChatClient(_settings(), None, transport=lambda b: (sent.append(b), ok)[1], sleep=lambda s: None)
    c.chat_json("openai/gpt-oss-20b", [{"role": "user", "content": "x"}], max_tokens=900)
    assert sent[0]["reasoning_effort"] == "low" and sent[0]["max_tokens"] == 2900


def test_json_mode_rejected_falls_back(cfg):
    seq = [(400, {}, '{"error":{"message":"response_format json_object is not supported"}}'),
           (200, {}, json.dumps({"choices": [{"message": {"content": '{"a": 2}'}}]}))]
    sent = []
    c = ChatClient(_settings(), None, transport=lambda b: (sent.append(dict(b)), seq.pop(0))[1], sleep=lambda s: None)
    assert c.chat_json("m", [{"role": "user", "content": "x"}]) == {"a": 2}
    assert "response_format" in sent[0] and "response_format" not in sent[1]
