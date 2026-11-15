"""Ops fixes (2026-11-15): git push timeout kills the process group and serializes pushes;
REST errors log repr(e); after a freeze, just-closed bars are gap-filled before stale history."""
import asyncio
import logging
import os
import time

import pytest

from tbot.bot import Bot, KeyState, backfill_priority, now_ms
from tbot.config import Settings, TF_MS
from tbot.gitpush import DataPusher, kill_and_reap
from tbot.rest import Rest
from tbot.strategy import PairStrategy


def test_backfill_priority_just_closed_5m_expires_sooner():
    # Shared close boundary for 5m/15m/1h (1h = 3_600_000 ms), 30 s later: all still inside recency
    # (15 min cap). The 5m bar expires first so it sorts ahead of 15m and 1h.
    close = 3_600_000
    now = close + 30_000
    p5 = backfill_priority("5m", now)
    p15 = backfill_priority("15m", now)
    p1h = backfill_priority("1h", now)
    assert p5[0] == p15[0] == p1h[0] == 0
    assert p5 < p15 < p1h


def test_backfill_priority_4h_stale_after_15m_but_5m_still_urgent():
    # 16 min after a 4h close the 4h bar is past the 15 min recency cap; a 5m last-close is always
    # < 5 min old, so 5m stays urgent and is filled first (then the latest 5m bar can still publish).
    close_4h = 14_400_000
    now = close_4h + 16 * 60_000
    p5 = backfill_priority("5m", now)
    p4 = backfill_priority("4h", now)
    assert p5[0] == 0 and p4[0] == 1
    assert p5 < p4


def test_schedule_backfill_orders_fresh_5m_ahead_of_stale(tmp_path):
    b = Bot(Settings(data_dir=str(tmp_path / "d"), push_enabled=False))

    def ks(sym, tf):
        s = KeyState(sym, tf, PairStrategy(b.P, sym, tf), last_t=0, warm=True, busy=False)
        b.keys[(sym, tf)] = s
        return s

    # freeze time at 90 s after a 5m+15m+4h close boundary
    boundary = 14_400_000  # 4h aligned, also 5m/15m
    frozen = boundary + 90_000

    import tbot.bot as botmod
    orig = botmod.now_ms
    botmod.now_ms = lambda: frozen
    try:
        stale4h = ks("OLDUSDT", "4h")
        # make 4h stale: last close at previous 4h (boundary-4h), now is 90s into new 4h
        # last closed 4h open = boundary-4h, close = boundary, age 90s, recency 15m -> actually FRESH
        # To get a stale 4h, move clock 16 min after close
        botmod.now_ms = lambda: boundary + 16 * 60_000
        b.schedule_backfill(stale4h)
        # fresh 5m: 30 s after a 5m close. Use a later 5m close after the 4h one.
        # Simpler: schedule stale first at +16min, then a 5m that just closed.
        # At boundary+16min, last 5m close is also 16min? 16 min after 4h close:
        # 5m last close = floor((boundary+16min)/5m)*5m - 5m, age < 5m always for 5m at any now.
        # A 5m latest close is ALWAYS within 5m of now (the last closed bar is < 5m old, plus up to 5m of the forming bar...
        # age of last closed 5m is now % 5m, max just under 5m. Recency is 5m, so remain = 5m - (now % 5m) >= 0
        # (unless now is exact close, remain=5m). So 5m latest bar is NEVER stale at 'now'!
        # Stale 5m happens only if last_t is older than the current last-closed bar (we missed bars),
        # but priority is based on TF clock, not last_t. So all 5m schedules are urgent.
        # Stale bucket is for TFs whose last *current* closed bar is older than recency — only 15m/1h/4h/1d
        # when more than 15 min past their close (and 5m never, because a new 5m close happens every 5m).
        # That's correct! After a 3h pause, the latest 5m bar (the one that closed up to 5 min ago) IS still
        # within recency and should be urgent. Older missing 5m bars are filled first in _backfill, then
        # the fresh one can still publish. Good.
        fresh5 = ks("NEWUSDT", "5m")
        b.schedule_backfill(fresh5)
    finally:
        botmod.now_ms = orig

    items = []
    while not b.bf_queue.empty():
        items.append(b.bf_queue.get_nowait())
    keys = [it[2] for it in items]
    # at boundary+16min, 4h last close (boundary) is 16 min old -> stale (bucket 1);
    # 5m last close is < 5 min old -> urgent (bucket 0)
    assert keys[0] == ("NEWUSDT", "5m")
    assert keys[1] == ("OLDUSDT", "4h")
    assert items[0][0][0] == 0 and items[1][0][0] == 1


def test_rest_timeout_log_uses_repr(caplog):
    class FakeLim:
        async def acquire(self, w):
            return None

        def on_response(self, status, headers):
            return 0

    class BoomSess:
        def get(self, *a, **k):
            raise asyncio.TimeoutError()

    async def go():
        r = Rest("https://example.invalid", FakeLim(), BoomSess())
        with caplog.at_level(logging.WARNING, logger="tbot.rest"):
            out = await r.get("/fapi/v1/klines", {"symbol": "BTCUSDT"}, 1, retries=1)
        return out

    assert asyncio.run(go()) is None
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "klines" in text
    assert "TimeoutError" in text
    assert "error  " not in text  # no empty message after "error "


def test_git_timeout_kills_process_group_and_reaps():
    async def go():
        # A session-leader sleep that would otherwise be left running after wait_for times out.
        p = await asyncio.create_subprocess_exec(
            "sleep", "30", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        pid = p.pid
        try:
            await asyncio.wait_for(p.communicate(), 0.3)
            raise AssertionError("sleep should not have finished")
        except asyncio.TimeoutError:
            await kill_and_reap(p)
        assert p.returncode is not None
        # process is gone (reaped); kill(pid, 0) must fail
        with pytest.raises(OSError):
            os.kill(pid, 0)

    asyncio.run(go())


def test_data_pusher_timeout_kills_and_logs_repr(tmp_path, caplog):
    async def go():
        pusher = DataPusher("https://example.invalid/repo.git", str(tmp_path), "data")
        pusher.cmd_timeout = 0.3
        pusher.ready = True  # skip setup (network)
        # Force _git to run `sleep` instead of git, by patching create_subprocess_exec via a wrapper _git
        orig_git = pusher._git

        async def sleepy(*args, cwd=None, check=True):
            p = await asyncio.create_subprocess_exec(
                "sleep", "30", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                await asyncio.wait_for(p.communicate(), pusher.cmd_timeout)
            except asyncio.TimeoutError:
                await kill_and_reap(p)
                raise asyncio.TimeoutError(f"git {args[0]} timed out after {pusher.cmd_timeout}s")
            return 0, ""

        pusher._git = sleepy
        with caplog.at_level(logging.WARNING, logger="tbot.git"):
            ok = await pusher.push({"bot.json": b"{}"}, "test")
        assert ok is False
        assert pusher.last_error and "TimeoutError" in pusher.last_error
        text = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "TimeoutError" in text
        assert "data push failed:" in text
        # message after the colon is not empty
        assert "data push failed: TimeoutError" in text

    asyncio.run(go())


def test_data_pusher_serializes_concurrent_pushes(tmp_path):
    async def go():
        pusher = DataPusher("https://example.invalid/repo.git", str(tmp_path), "data")
        pusher.ready = True
        running = 0
        max_running = 0
        order = []

        async def slow_git(*args, cwd=None, check=True):
            nonlocal running, max_running
            running += 1
            max_running = max(max_running, running)
            order.append(args[0] if args else "")
            await asyncio.sleep(0.05)
            running -= 1
            # add / diff --cached --quiet / commit / push
            if args and args[0] == "diff":
                return 0, ""  # no change -> push returns False quickly after add+diff
            return 0, ""

        pusher._git = slow_git
        a, b = await asyncio.gather(
            pusher.push({"bot.json": b"a"}, "a"),
            pusher.push({"bot.json": b"b"}, "b"),
        )
        assert max_running == 1  # never overlapped
        assert a is False and b is False  # diffs reported no change

    asyncio.run(go())
