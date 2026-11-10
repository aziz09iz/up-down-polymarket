"""SQLite persistence: positions, rounds, equity snapshots, key-values.

All queries are static SQL literals with `?` placeholders, inlined at the call
site. No value or identifier is ever concatenated into SQL.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from .models import Market, Position

log = logging.getLogger("updown.store")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS positions ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " market_slug TEXT NOT NULL, side TEXT NOT NULL,"
                " stake_usdc REAL, fees REAL, shares REAL, avg_price REAL,"
                " eff_price REAL, entry_ts REAL, t_left_s REAL, lead_bps REAL,"
                " ptb REAL, spot_at_entry REAL, best_ask_at_signal REAL,"
                " slippage_cost REAL, status TEXT DEFAULT 'OPEN',"
                " payout REAL DEFAULT 0, pnl_net REAL DEFAULT 0,"
                " exit_ts REAL, exit_reason TEXT DEFAULT '',"
                " winner TEXT, resolution_source TEXT DEFAULT '',"
                " fills TEXT, created_ts REAL)")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS rounds ("
                " slug TEXT PRIMARY KEY, start_ts REAL, end_ts REAL,"
                " ptb REAL, ptb_source TEXT, close_chainlink REAL,"
                " winner TEXT, resolution_source TEXT,"
                " traded INTEGER DEFAULT 0, our_side TEXT, pnl REAL DEFAULT 0,"
                " skipped_reason TEXT DEFAULT '', observed_ts REAL)")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS equity ("
                " ts REAL PRIMARY KEY, equity REAL, balance REAL, open_value REAL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS kv ("
                             " key TEXT PRIMARY KEY, value TEXT)")
            self._db.execute("CREATE INDEX IF NOT EXISTS idx_pos_status "
                             "ON positions(status)")
            self._db.execute("CREATE INDEX IF NOT EXISTS idx_pos_created "
                             "ON positions(created_ts)")
            self._db.commit()

    def close(self):
        with self._lock:
            self._db.close()

    # ── positions ──
    def insert_position(self, pos: Position, market: Market) -> int:
        values = (
            pos.market_slug, pos.side, pos.stake_usdc, pos.fees, pos.shares,
            pos.avg_price, pos.eff_price, pos.entry_ts, pos.t_left_s, pos.lead_bps,
            pos.ptb, pos.spot_at_entry, pos.best_ask_at_signal, pos.slippage_cost,
            json.dumps(pos.fills), time.time(),
        )
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO positions (market_slug, side, stake_usdc, fees, shares,"
                " avg_price, eff_price, entry_ts, t_left_s, lead_bps, ptb,"
                " spot_at_entry, best_ask_at_signal, slippage_cost, fills, created_ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", values)
            self._db.commit()
            return int(cur.lastrowid)

    def close_position(self, pos: Position, winner: Optional[str] = None,
                       resolution_source: str = ""):
        with self._lock:
            self._db.execute(
                "UPDATE positions SET status=?, payout=?, pnl_net=?, exit_ts=?,"
                " exit_reason=?, winner=?, resolution_source=? WHERE id=?",
                (pos.status, pos.payout, pos.pnl_net, pos.exit_ts, pos.exit_reason,
                 winner, resolution_source, pos.id))
            self._db.commit()

    def unresolved_positions(self) -> list:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM positions WHERE status='OPEN' ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def recent_positions(self, n: int = 20) -> list:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM positions ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) for r in rows]

    def all_closed(self) -> list:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM positions WHERE status != 'OPEN' ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def positions_since(self, ts: float) -> list:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM positions WHERE created_ts >= ? ORDER BY id",
                (ts,)).fetchall()
        return [dict(r) for r in rows]

    def restore_open(self) -> list:
        """Rehydrate OPEN positions after a restart."""
        out = []
        for r in self.unresolved_positions():
            p = Position(
                market_slug=r["market_slug"], side=r["side"], stake_usdc=r["stake_usdc"],
                fees=r["fees"], shares=r["shares"], avg_price=r["avg_price"],
                entry_ts=r["entry_ts"], t_left_s=r["t_left_s"], lead_bps=r["lead_bps"],
                ptb=r["ptb"], spot_at_entry=r["spot_at_entry"],
                best_ask_at_signal=r["best_ask_at_signal"],
                slippage_cost=r["slippage_cost"], status="OPEN", id=r["id"])
            out.append(p)
        return out

    # ── rounds ──
    def upsert_round(self, slug: str, start_ts: Optional[float] = None,
                     end_ts: Optional[float] = None, ptb: Optional[float] = None,
                     ptb_source: str = "", close_chainlink: Optional[float] = None,
                     winner: Optional[str] = None, resolution_source: str = "",
                     traded: int = 0, our_side: Optional[str] = None,
                     pnl: float = 0.0, skipped_reason: str = "",
                     observed_ts: Optional[float] = None):
        with self._lock:
            self._db.execute(
                "INSERT INTO rounds (slug, start_ts, end_ts, ptb, ptb_source,"
                " close_chainlink, winner, resolution_source, traded, our_side, pnl,"
                " skipped_reason, observed_ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(slug) DO UPDATE SET"
                " start_ts=excluded.start_ts, end_ts=excluded.end_ts,"
                " ptb=excluded.ptb, ptb_source=excluded.ptb_source,"
                " close_chainlink=excluded.close_chainlink,"
                " winner=excluded.winner, resolution_source=excluded.resolution_source,"
                " traded=excluded.traded, our_side=excluded.our_side,"
                " pnl=excluded.pnl, skipped_reason=excluded.skipped_reason,"
                " observed_ts=excluded.observed_ts",
                (slug, start_ts, end_ts, ptb, ptb_source, close_chainlink, winner,
                 resolution_source, traded, our_side, pnl, skipped_reason, observed_ts))
            self._db.commit()

    def recent_rounds(self, n: int = 15) -> list:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM rounds ORDER BY start_ts DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) for r in rows]

    def get_round(self, slug: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM rounds WHERE slug=?", (slug,)).fetchone()
        return dict(row) if row else None

    # ── equity ──
    def snapshot_equity(self, equity: float, balance: float, open_value: float):
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO equity (ts, equity, balance, open_value)"
                " VALUES (?, ?, ?, ?)", (time.time(), equity, balance, open_value))
            self._db.commit()

    def equity_series(self, since_ts: float = 0.0) -> list:
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, equity FROM equity WHERE ts >= ? ORDER BY ts",
                (since_ts,)).fetchall()
        return [(r["ts"], r["equity"]) for r in rows]

    # ── kv ──
    def kv_get(self, key: str, default=None):
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str):
        with self._lock:
            self._db.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))
            self._db.commit()
