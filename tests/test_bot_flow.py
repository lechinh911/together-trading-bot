import asyncio

from conftest import load_fixture
from tbot.bot import Bot, KeyState
from tbot.config import Settings
from tbot.engine import Engine


def make_bot(tmp_path):
    s = Settings(data_dir=str(tmp_path / "d"), site_dir=str(tmp_path / "site"), push_enabled=False)
    b = Bot(s)
    b.bf_queue = asyncio.Queue()
    return b


def test_gap_buffers_and_schedules_backfill(tmp_path):
    b = make_bot(tmp_path)
    bars = load_fixture("ETHUSDT_1h.json")
    ks = KeyState("ETHUSDT", "1h", Engine(b.P, "ETHUSDT", "1h"), busy=False, warm=True)
    b.keys[("ETHUSDT", "1h")] = ks
    for bar in bars[:300]:
        b.on_closed_bar(ks, bar)
    assert ks.last_t == bars[299][0]
    b.on_closed_bar(ks, bars[305])  # 5 bars missing
    assert ks.busy and ks.buffer == [bars[305]] and b.bf_queue.qsize() == 1
    b.on_closed_bar(ks, bars[306])  # still buffered while backfilling
    assert len(ks.buffer) == 2 and b.bf_queue.qsize() == 1
    for bar in bars[300:305]:  # what the REST backfill would deliver
        b._process(ks, bar, live=True)
    b._drain(ks)
    assert ks.last_t == bars[306][0] and not ks.busy


def test_old_bars_never_open_trades_and_publish(tmp_path):
    b = make_bot(tmp_path)
    bars = load_fixture("BTCUSDT_4h.json")
    ks = KeyState("BTCUSDT", "4h", Engine(b.P, "BTCUSDT", "4h"), busy=False, warm=True)
    b.keys[("BTCUSDT", "4h")] = ks
    for bar in bars:
        b._process(ks, bar, live=True)
    assert not b.open and b.counters["signals"] == 0  # historical bars are not "recent"
    data = b.publisher.build()
    assert data["status"]["keys"] == 1 and data["signals"] == [] and data["stats"]["ALL"]["n"] == 0
    b.publisher.write(data)
    assert (tmp_path / "d" / "bot.json").exists()


def _msg(sym, tf, bar, closed):
    import json as _j
    t, o, h, l, c, v = bar
    return _j.dumps({"stream": f"{sym.lower()}@kline_{tf}", "data": {"e": "kline", "s": sym, "k": {
        "t": t, "i": tf, "o": str(o), "h": str(h), "l": str(l), "c": str(c), "v": str(v), "x": closed}}})


def test_forming_klines_never_processed(tmp_path):
    """Only x=true (closed) klines reach the engine / trades; forming updates only refresh the display price."""
    b = make_bot(tmp_path)
    bars = load_fixture("BTCUSDT_4h.json")
    ks = KeyState("BTCUSDT", "4h", Engine(b.P, "BTCUSDT", "4h"), busy=False, warm=True)
    b.keys[("BTCUSDT", "4h")] = ks
    calls = []
    orig = ks.engine.process_bar
    ks.engine.process_bar = lambda *a, **k: calls.append(a[0]) or orig(*a, **k)
    for bar in bars[:400]:
        b._on_ws(_msg("BTCUSDT", "4h", bar, False))
    assert calls == [] and ks.last_t is None and b.counters["closed_bars"] == 0
    assert b.last_price["BTCUSDT"] == bars[399][4]
    for bar in bars[:400]:
        b._on_ws(_msg("BTCUSDT", "4h", bar, False))  # forming update
        b._on_ws(_msg("BTCUSDT", "4h", bar, True))   # close
    assert len(calls) == 400 and ks.last_t == bars[399][0]
