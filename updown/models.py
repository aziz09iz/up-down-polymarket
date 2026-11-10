"""Shared data models."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Market:
    slug: str
    condition_id: str
    question: str
    start_ts: float                 # epoch s (round open, 5-min boundary)
    end_ts: float                   # epoch s (round close = start + 300)
    token_up: str
    token_down: str
    fee_rate: float = 0.07          # taker fee rate (crypto category)
    # filled after close
    closed: bool = False
    winner: Optional[str] = None    # "Up" | "Down" | None
    ptb: Optional[float] = None     # price-to-beat (Chainlink openPrice)
    ptb_source: str = ""            # chainlink | spot_fallback
    close_chainlink: Optional[float] = None
    resolution_source: str = ""     # chainlink | gamma | spot_fallback

    def token(self, side: str) -> str:
        return self.token_up if side == "Up" else self.token_down

    @property
    def duration_s(self) -> float:
        return self.end_ts - self.start_ts


@dataclass
class Level:
    price: float
    size: float


@dataclass
class Book:
    token_id: str
    ts: float                       # local epoch when fetched
    bids: list = field(default_factory=list)   # Level desc by price
    asks: list = field(default_factory=list)   # Level asc by price
    server_ts: Optional[float] = None          # exchange server epoch (clock sync)

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.bids and self.asks:
            return (self.bids[0].price + self.asks[0].price) / 2
        return None

    def ask_depth(self, max_price: float) -> float:
        return sum(l.size for l in self.asks if l.price <= max_price + 1e-9)

    def spread_ticks(self, tick: float) -> Optional[int]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return round((self.best_ask - self.best_bid) / tick)


@dataclass
class Spot:
    ts: float
    mid: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    source: str = ""


@dataclass
class Chainlink:
    open_price: Optional[float]     # price-to-beat
    close_price: Optional[float]    # latest (live) or final (completed) chainlink price
    ts_ms: int                      # observation timestamp from the API
    completed: bool = False
    received_at: float = field(default_factory=time.time)
    points: list = field(default_factory=list)   # price-history series (cache)


@dataclass
class Fill:
    price: float
    shares: float
    fee: float
    level_price: float              # quoted level price before adverse-move model


@dataclass
class Position:
    market_slug: str
    side: str                       # "Up" | "Down"
    stake_usdc: float               # cash spent excl. fee
    fees: float
    shares: float
    avg_price: float
    entry_ts: float
    t_left_s: float                 # seconds remaining at entry
    lead_bps: float                 # lead at entry
    ptb: float
    spot_at_entry: float
    best_ask_at_signal: float
    slippage_cost: float            # (avg_fill - best_ask_at_signal) * shares + fee portion tracked separately
    fills: list = field(default_factory=list)
    status: str = "OPEN"            # OPEN | WON | LOST | SOLD
    payout: float = 0.0
    pnl_net: float = 0.0
    exit_ts: Optional[float] = None
    exit_reason: str = ""           # resolution | flip_exit
    id: Optional[int] = None

    @property
    def eff_price(self) -> float:
        return (self.stake_usdc + self.fees) / self.shares if self.shares else 0.0


@dataclass
class Decision:
    action: str                     # "enter" | "wait" | "skip"
    side: Optional[str] = None
    reasons: dict = field(default_factory=dict)   # rule -> (ok: bool, detail: str)
    metrics: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.action == "enter"

    def failed_rules(self) -> list:
        return [f"{k}: {v[1]}" for k, v in self.reasons.items() if not v[0]]
