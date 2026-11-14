"""WebSocket recovery against a local fake Binance stream: forced close, server drop and a silent socket must all
reconnect within seconds (with the streams in the URL) and klines must flow again."""
import asyncio
import json

import aiohttp
from aiohttp import web

from tbot.bot import Bot, Conn
from tbot.config import Settings


class FakeStream:
    def __init__(self):
        self.connects = 0
        self.urls = []
        self.mode = "normal"  # "silent": accept but never send

    async def handler(self, request):
        mode = self.mode
        ws = web.WebSocketResponse(autoping=(mode != "frozen"))  # frozen: no data and no pong (dead peer)
        await ws.prepare(request)
        self.connects += 1
        self.urls.append(str(request.rel_url))

        async def reader():  # answers the client's close frame like Binance does
            async for _ in ws:
                pass
        rt = asyncio.create_task(reader())
        try:
            while not ws.closed:
                if mode == "normal":
                    k = {"t": 0, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1", "x": False, "i": "5m"}
                    await ws.send_str(json.dumps({"stream": "btcusdt@kline_5m", "data": {"e": "kline", "s": "BTCUSDT", "k": k}}))
                await asyncio.sleep(0.05)
        except (ConnectionResetError, RuntimeError):
            pass
        rt.cancel()
        return ws


async def _run(tmp_path):
    fake = FakeStream()
    app = web.Application()
    app.router.add_get("/market/stream", fake.handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    s = Settings(data_dir=str(tmp_path / "d"), push_enabled=False, ws_base=f"ws://127.0.0.1:{port}/market/stream",
                 ws_silence_s=1, ws_ping_s=0.5)
    b = Bot(s)
    async with aiohttp.ClientSession() as sess:
        b.session = sess
        c = Conn(0, {"btcusdt@kline_5m"})
        b.conns = [c]
        c.task = asyncio.create_task(b.conn_loop(c))

        async def wait_msgs(after, timeout=5.0):
            t0 = asyncio.get_event_loop().time()
            while c.msgs <= after:
                assert asyncio.get_event_loop().time() - t0 < timeout, "no klines after reconnect"
                await asyncio.sleep(0.05)
            return asyncio.get_event_loop().time() - t0

        await wait_msgs(5)
        assert fake.urls[0] == "/market/stream?streams=btcusdt@kline_5m"
        # 1) forced reconnect (resume / SIGUSR1): new socket + data within ~1 s
        n, m = fake.connects, c.msgs
        b.force_reconnect("test")
        dt = await wait_msgs(m + 5)
        assert fake.connects == n + 1 and dt < 3
        # 2) socket that answers pings but sends no data: silence watchdog reconnects
        # 3) dead peer (no data, no pong): heartbeat ping closes it
        for mode in ("silent", "frozen"):
            fake.mode = mode
            k = fake.connects
            b.force_reconnect("test")
            while fake.connects == k:  # wait until the bad socket is in place
                await asyncio.sleep(0.02)
            fake.mode = "normal"
            n, m = fake.connects, c.msgs
            dt = await wait_msgs(m + 5, timeout=6)
            assert fake.connects == n + 1 and dt < 6
        assert b.counters["ws_silence"] >= 1
        assert all(u == fake.urls[0] for u in fake.urls)  # every reconnect carries the streams in the URL
        c.task.cancel()
        try:
            await c.task
        except asyncio.CancelledError:
            pass
    await runner.cleanup()


def test_ws_reconnects_and_data_flows(tmp_path):
    asyncio.run(_run(tmp_path))
