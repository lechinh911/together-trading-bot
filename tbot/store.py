"""SQLite persistence (data/bot.sqlite, WAL): closed candles (rolling, last keep_bars per pair; candle writes are batched
and flushed every 10 s), virtual trades (status open / closed / void) and a small key-value table (universe,
signals_log)."""
from __future__ import annotations

import json
import os
import sqlite3

from .trades import Trade

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles(sym TEXT, tf TEXT, t INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL,
  PRIMARY KEY(sym, tf, t)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY AUTOINCREMENT, sym TEXT, tf TEXT, side TEXT, entry_t INTEGER,
  status TEXT, exit_t INTEGER, data TEXT);
CREATE INDEX IF NOT EXISTS trades_status ON trades(status);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.pending = []

    # candles
    def add_candle(self, sym, tf, bar):
        self.pending.append((sym, tf) + tuple(bar[:6]))

    def flush(self):
        if self.pending:
            self.db.executemany("INSERT OR REPLACE INTO candles VALUES(?,?,?,?,?,?,?,?)", self.pending)
            self.pending = []
        self.db.commit()

    def load_candles(self, sym, tf, limit=600):
        rows = self.db.execute("SELECT t,o,h,l,c,v FROM candles WHERE sym=? AND tf=? ORDER BY t DESC LIMIT ?",
                               (sym, tf, limit)).fetchall()
        return [tuple(r) for r in reversed(rows)]

    def prune(self, keep=600):
        self.flush()
        for sym, tf, n in self.db.execute("SELECT sym, tf, COUNT(*) FROM candles GROUP BY sym, tf").fetchall():
            if n > keep:
                self.db.execute("DELETE FROM candles WHERE sym=? AND tf=? AND t < (SELECT t FROM candles WHERE sym=? "
                                "AND tf=? ORDER BY t DESC LIMIT 1 OFFSET ?)", (sym, tf, sym, tf, keep - 1))
        self.db.commit()

    # trades
    def save_trade(self, tr: Trade):
        d = tr.to_dict()
        if tr.id is None:
            cur = self.db.execute("INSERT INTO trades(sym,tf,side,entry_t,status,exit_t,data) VALUES(?,?,?,?,?,?,?)",
                                  (tr.symbol, tr.tf, tr.side, tr.entry_t, tr.status, tr.exit_t, json.dumps(d)))
            tr.id = cur.lastrowid
            d["id"] = tr.id
        self.db.execute("UPDATE trades SET status=?, exit_t=?, data=? WHERE id=?",
                        (tr.status, tr.exit_t, json.dumps(tr.to_dict()), tr.id))
        self.db.commit()

    def load_trades(self, status=None, limit=None):
        q = "SELECT data FROM trades" + (" WHERE status=?" if status else "") + " ORDER BY id DESC" + (f" LIMIT {int(limit)}" if limit else "")
        rows = self.db.execute(q, (status,) if status else ()).fetchall()
        return [Trade(**json.loads(r[0])) for r in rows]

    def kv_get(self, k, default=None):
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def kv_set(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, json.dumps(v)))
        self.db.commit()
