"""Contents API pusher: no git binary; token never logged; double-publish guard."""
import asyncio
import base64
import json
import logging
import os
import time

import pytest

from tbot.gitpush import DataPusher, parse_github_repo, _redact, FRESH_S
from tbot.publisher import publisher_identity


def test_parse_github_repo_variants():
    assert parse_github_repo("https://github.com/lechinh911/together-trading-bot.git") == (
        "lechinh911", "together-trading-bot")
    assert parse_github_repo("https://github.com/lechinh911/together-trading-bot") == (
        "lechinh911", "together-trading-bot")
    assert parse_github_repo("git@github.com:lechinh911/together-trading-bot.git") == (
        "lechinh911", "together-trading-bot")
    assert parse_github_repo("https://api.github.com/repos/lechinh911/together-trading-bot") == (
        "lechinh911", "together-trading-bot")
    with pytest.raises(ValueError):
        parse_github_repo("https://example.invalid/repo.git")


def test_redact_strips_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_TOGETHER", "TESTTOKENSECRET")
    assert "TESTTOKENSECRET" not in _redact("boom TESTTOKENSECRET trailing")
    assert "***" in _redact("boom TESTTOKENSECRET trailing")


def test_setup_requires_token(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_TOKEN_TOGETHER", raising=False)
    p = DataPusher("https://github.com/lechinh911/together-trading-bot.git", str(tmp_path))
    assert asyncio.run(p.setup()) is False
    assert p.last_error == "GITHUB_TOKEN_TOGETHER not set"


def _meta(obj, sha="abc"):
    raw = json.dumps(obj).encode()
    return {"sha": sha, "content": base64.b64encode(raw).decode()}


def _pusher(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_TOGETHER", "TESTTOKENSECRET")
    monkeypatch.delenv("TBOT_FORCE_PUBLISH", raising=False)
    p = DataPusher("https://github.com/lechinh911/together-trading-bot.git", str(tmp_path))
    p.ready = True
    return p


def test_push_puts_bot_json_and_skips_identical(tmp_path, monkeypatch):
    p = _pusher(tmp_path, monkeypatch)
    calls = []
    payload = json.dumps({"schema": "tbot-1", "publisher_host": "vps1", "generated": time.time()}).encode()

    async def req(session, method, url, **kw):
        calls.append((method, url, kw.get("json")))
        if method == "GET":
            return 200, json.dumps(_meta(json.loads(payload), sha="sha1"))
        if method == "PUT":
            return 200, "{}"
        raise AssertionError(method)

    p._request = req
    assert asyncio.run(p.push({"bot.json": payload}, "same")) is False  # identical
    assert all(m != "PUT" for m, *_ in calls)
    calls.clear()
    newer = json.dumps({"schema": "tbot-1", "publisher_host": "vps1", "generated": time.time() + 1}).encode()
    assert asyncio.run(p.push({"bot.json": newer}, "upd")) is True
    assert p.pushes == 1
    assert any(m == "PUT" for m, *_ in calls)
    put = next(c for c in calls if c[0] == "PUT")
    assert put[2]["branch"] == "data"
    assert put[2]["sha"] == "sha1"
    body = base64.b64decode(put[2]["content"])
    assert body == newer
    # token must not appear in the PUT payload
    assert "TESTTOKENSECRET" not in json.dumps(put[2])


def test_sha_mismatch_retries_once(tmp_path, monkeypatch):
    p = _pusher(tmp_path, monkeypatch)
    puts = []

    async def req(session, method, url, **kw):
        if method == "GET":
            sha = "sha-new" if puts else "sha-old"
            return 200, json.dumps(_meta({"publisher_host": "vps1", "generated": 0}, sha=sha))
        if method == "PUT":
            puts.append(kw["json"]["sha"])
            if kw["json"]["sha"] == "sha-old":
                return 409, "sha mismatch"
            return 200, "{}"
        raise AssertionError(method)

    p._request = req
    data = json.dumps({"publisher_host": "vps1", "generated": time.time()}).encode()
    assert asyncio.run(p.push({"bot.json": data}, "m")) is True
    assert puts == ["sha-old", "sha-new"]


def test_guard_blocks_fresh_other_host(tmp_path, monkeypatch, caplog):
    p = _pusher(tmp_path, monkeypatch)
    remote = {"publisher_host": "box-a", "generated": time.time() - 30}

    async def req(session, method, url, **kw):
        if method == "GET":
            return 200, json.dumps(_meta(remote))
        raise AssertionError("must not PUT")

    p._request = req
    local = json.dumps({"publisher_host": "vps-b", "generated": time.time()}).encode()
    with caplog.at_level(logging.WARNING, logger="tbot.git"):
        assert asyncio.run(p.push({"bot.json": local}, "m")) is False
    assert p.blocked_other_host is True
    assert "box-a" in p.last_error
    assert "TBOT_FORCE_PUBLISH" in p.last_error
    assert "TESTTOKENSECRET" not in caplog.text
    assert "TESTTOKENSECRET" not in (p.last_error or "")


def test_guard_allows_same_host_and_stale_other(tmp_path, monkeypatch):
    p = _pusher(tmp_path, monkeypatch)

    async def req_same(session, method, url, **kw):
        if method == "GET":
            return 200, json.dumps(_meta({"publisher_host": "vps1", "generated": time.time() - 10}))
        return 200, "{}"

    p._request = req_same
    local = json.dumps({"publisher_host": "vps1", "generated": time.time()}).encode()
    assert asyncio.run(p.push({"bot.json": local}, "m")) is True

    p2 = _pusher(tmp_path, monkeypatch)

    async def req_stale(session, method, url, **kw):
        if method == "GET":
            return 200, json.dumps(_meta({"publisher_host": "box-a", "generated": time.time() - FRESH_S - 60}))
        return 200, "{}"

    p2._request = req_stale
    local2 = json.dumps({"publisher_host": "vps-b", "generated": time.time()}).encode()
    assert asyncio.run(p2.push({"bot.json": local2}, "m")) is True


def test_force_publish_overrides_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("TBOT_FORCE_PUBLISH", "1")
    p = _pusher(tmp_path, monkeypatch)
    monkeypatch.setenv("TBOT_FORCE_PUBLISH", "1")

    async def req(session, method, url, **kw):
        if method == "GET":
            return 200, json.dumps(_meta({"publisher_host": "box-a", "generated": time.time()}))
        return 200, "{}"

    p._request = req
    local = json.dumps({"publisher_host": "vps-b", "generated": time.time()}).encode()
    assert asyncio.run(p.push({"bot.json": local}, "m")) is True


def test_timeout_logs_repr_not_token(tmp_path, monkeypatch, caplog):
    p = _pusher(tmp_path, monkeypatch)

    async def req(session, method, url, **kw):
        raise asyncio.TimeoutError()

    p._request = req
    with caplog.at_level(logging.WARNING, logger="tbot.git"):
        assert asyncio.run(p.push({"bot.json": b"{}"}, "m")) is False
    assert p.last_error and "TimeoutError" in p.last_error
    assert "TESTTOKENSECRET" not in p.last_error
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "data push failed:" in text
    assert "TimeoutError" in text
    assert "TESTTOKENSECRET" not in text


def test_pusher_serializes_concurrent_http(tmp_path, monkeypatch):
    p = _pusher(tmp_path, monkeypatch)
    running = 0
    max_running = 0

    async def req(session, method, url, **kw):
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        await asyncio.sleep(0.05)
        running -= 1
        if method == "GET":
            return 404, ""
        return 201, "{}"

    p._request = req

    async def go():
        return await asyncio.gather(
            p.push({"bot.json": b'{"a":1}'}, "a"),
            p.push({"bot.json": b'{"b":2}'}, "b"),
        )

    a, b = asyncio.run(go())
    assert max_running == 1
    assert a is True and b is True
    assert p.pushes == 2


def test_publisher_identity_stable(tmp_path):
    a = publisher_identity(str(tmp_path))
    b = publisher_identity(str(tmp_path))
    assert a == b
    assert a[0] and a[1].startswith(a[0] + "-")
    assert (tmp_path / "publisher_id").is_file()
    # file is not a secret — just a host id
    assert "token" not in (tmp_path / "publisher_id").read_text().lower()
