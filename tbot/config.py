"""Strategy parameters (v4 presets) and service settings. Active preset is the default."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

TIMEFRAMES = ["5m", "15m", "1h", "4h", "1d"]
TF_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
# Higher-timeframe bias: Pine default htfTf = "240" (4h) for EVERY chart timeframe (see htf.py).
HTF_TF = "4h"


@dataclass(frozen=True)
class Params:
    ema_fast: int = 20
    ema_slow: int = 50
    piv_left: int = 5
    piv_right: int = 5
    atr_len: int = 14
    cluster_atr: float = 0.5
    min_cluster: int = 3
    spike_atr: float = 1.0
    min_retrace: float = 0.55
    tol_atr: float = 0.75
    min_height_atr: float = 1.60
    min_bars: int = 5
    max_gap: int = 100
    max_bars: int = 50
    opp_cancel: bool = True
    min_quality: float = 0.40
    break_buf: float = 0.15
    cancel_retest: bool = False
    cross_win: int = 0
    use_volume: bool = True
    vol_len: int = 20
    vol_mult: float = 1.15
    need_cross: bool = False
    confirm_win: int = 0
    spread_atr: float = 0.30
    slope_len: int = 3
    slope_atr: float = 0.25
    min_score: int = 2
    adx_len: int = 14
    adx_min: float = 20.0
    use_htf: bool = True
    htf_ema: int = 50
    use_rsi: bool = False
    rsi_len: int = 14
    rsi_long_max: float = 72.0
    rsi_short_min: float = 28.0
    cooldown: int = 5
    # virtual exits
    stop_cap_atr: float = 3.0
    tp_r: float = 1.5
    be_at_r: float = 1.0
    trail_atr: float = 1.30
    commission: float = 0.0004  # per side


PRESETS = {
    # Only the Active preset values were specified (V4_RULES.md). Other presets of the user's Pine script are not
    # reproduced here; individual params can be overridden with TBOT_PARAMS='{"min_quality":0.5,...}'.
    "Active": Params(),
}


def load_params(preset: str = "Active") -> Params:
    import json
    p = PRESETS.get(preset, PRESETS["Active"])
    raw = os.environ.get("TBOT_PARAMS")
    if raw:
        p = replace(p, **json.loads(raw))
    return p


@dataclass
class Settings:
    preset: str = os.environ.get("TBOT_PRESET", "Active")
    min_quote_volume: float = float(os.environ.get("TBOT_MIN_QVOL", "1000000"))
    timeframes: list = field(default_factory=lambda: list(TIMEFRAMES))
    rest_base: str = os.environ.get("TBOT_REST", "https://fapi.binance.com")
    # Binance moved USDT-M market streams (klines) to the /market path; legacy /stream no longer pushes klines.
    ws_base: str = os.environ.get("TBOT_WS", "wss://fstream.binance.com/market/stream")
    ws_ping_s: float = 20.0  # aiohttp heartbeat ping (closes the socket if the pong is missing)
    ws_silence_s: int = 30  # no message for this long on a connection -> reconnect it
    catchup_every_s: int = 60  # REST catch-up of pairs whose closed bar is missing > 30 s after the close
    streams_per_conn: int = int(os.environ.get("TBOT_STREAMS_PER_CONN", "200"))
    weight_budget_per_min: int = int(os.environ.get("TBOT_WEIGHT_BUDGET", "600"))
    history_limit: int = int(os.environ.get("TBOT_HISTORY", "499"))  # klines limit<500 -> weight 2
    keep_bars: int = 600
    backfill_concurrency: int = int(os.environ.get("TBOT_BACKFILL_CONC", "4"))
    universe_refresh_s: int = 3600
    data_dir: str = os.environ.get("TBOT_DATA", "/workspace/together_trading_bot/data")
    # Live data goes to the GitHub `data` branch; the dashboard reads it client-side (no site redeploys).
    repo_url: str = os.environ.get("TBOT_REPO", "https://github.com/lechinh911/together-trading-bot.git")
    data_branch: str = os.environ.get("TBOT_DATA_BRANCH", "data")
    push_enabled: bool = os.environ.get("TBOT_PUSH", "1") == "1"
    publish_every_s: int = 60  # local JSON
    push_every_s: int = int(os.environ.get("TBOT_PUSH_EVERY", "120"))  # push if the trading content changed
    heartbeat_push_s: int = int(os.environ.get("TBOT_HEARTBEAT_PUSH", "600"))  # status-only refresh

    @property
    def params(self) -> Params:
        return load_params(self.preset)
