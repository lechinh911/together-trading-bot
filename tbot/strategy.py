"""PairStrategy = one Pine strategy instance for one symbol + timeframe: the Engine (setups / signals) plus at most one
virtual position (pyramiding=0) and the cooldown counter lastExitBar. Each CLOSED bar runs in the Pine order:
  1. broker step: the open position's stop / TP (set at the previous close) are checked on this bar;
  2. script step: indicators, pivots, setups and the signal with coolOk = bar_index - lastExitBar >= cooldown
     (lastExitBar as it was before this bar) and entry only if flat; a position that survived step 1 then gets its
     BE / trail update at this close (applies from the next bar). The entry bar itself is not managed;
  3. if the position closed on this bar: lastExitBar = this bar. (Like Pine, a new entry on the exit bar itself is
     possible when the previous exit was >= cooldown bars ago.)
"""
from __future__ import annotations

from typing import Optional

from .config import Params
from .engine import Engine
from .trades import Trade, check_exit, manage, open_trade, fee_dominated


class PairStrategy:
    def __init__(self, params: Params, symbol: str = "", tf: str = "", hist=None):
        self.p = params
        self.engine = Engine(params, symbol, tf, hist)
        self.trade: Optional[Trade] = None
        self.trade_public = False  # False = shadow trade from warm-up replay (kept for state parity, not published)
        self.last_exit_idx = -99999  # Pine lastExitBar

    def on_bar(self, bar, htf_dir: int = 0, publish: bool = True):
        """Returns (signal_or_None, opened_trade_or_None, closed_trade_or_None)."""
        eng = self.engine
        i = eng.n  # bar_index of this bar
        closed = None
        held = None  # position that was open before this bar and is checked on it
        tr = self.trade
        if tr is not None and bar[0] > (tr.last_t or 0):
            if check_exit(tr, bar, self.p):
                closed, self.trade = tr, None
            else:
                held = tr
        cool_ok = i - self.last_exit_idx >= self.p.cooldown
        sig = eng.process_bar(bar, htf_dir, cool_ok, self.trade is None)
        if held is not None:  # BE / trail at this close; skipped for bars already applied (restored trades)
            manage(held, bar, eng.atr[-1], self.p)
        opened = None
        if sig is not None:
            opened = open_trade(sig, self.p)
            if opened is not None:
                self.trade = opened
                self.trade_public = publish and not fee_dominated(opened, self.p)
        if closed is not None:
            self.last_exit_idx = i
        return sig, opened, closed
