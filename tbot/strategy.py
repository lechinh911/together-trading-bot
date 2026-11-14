"""One symbol+timeframe = Pine strategy instance: engine (setups/signals) + at most one virtual position
(pyramiding=0) + cooldown from lastExitBar, executed exactly in the Pine order on each CLOSED bar:
  1. broker step: the open position's stop / TP orders are checked on this bar (orders set at the previous close);
  2. script step: indicators, pivots, setups, signal with coolOk = bar - lastExitBar >= cooldown (lastExitBar from
     before this bar) and entry only if flat; then BE/trail update of a still-open position at the close;
  3. if the position closed on this bar: lastExitBar = this bar.
"""
from __future__ import annotations

from typing import Optional

from .config import Params
from .engine import Engine
from .trades import Trade, apply_bar, manage, open_trade


class PairStrategy:
    def __init__(self, params: Params, symbol: str = "", tf: str = ""):
        self.p = params
        self.engine = Engine(params, symbol, tf)
        self.trade: Optional[Trade] = None
        self.trade_public = False  # False = shadow trade from warm-up replay (kept for state parity, not published)
        self.last_exit_idx = -99999

    def on_bar(self, bar, htf_dir: int = 0, publish: bool = True):
        """Returns (signal_or_None, opened_trade_or_None, closed_trade_or_None)."""
        eng = self.engine
        i = eng.n  # bar_index of this bar
        closed = None
        tr = self.trade
        if tr is not None and bar[0] > (tr.last_t or 0):
            if apply_bar(tr, bar, eng.peek_atr(bar), self.p, do_manage=False):
                closed = tr
                self.trade = None
        cool_ok = i - self.last_exit_idx >= self.p.cooldown
        sig = eng.process_bar(bar, htf_dir, cool_ok, self.trade is None)
        opened = None
        if self.trade is not None:  # still open after this bar: BE / trail at the close (applies from next bar)
            manage(self.trade, bar, eng.atr[-1], self.p)
        if sig is not None:
            opened = open_trade(sig, self.p)
            if opened is not None:
                self.trade = opened
                self.trade_public = publish
        if closed is not None:
            self.last_exit_idx = i
        return sig, opened, closed
