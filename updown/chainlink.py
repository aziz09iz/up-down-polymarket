"""Chainlink price feed via Polymarket's site API.

GET https://polymarket.com/api/crypto/crypto-price?symbol=btc&eventStartTime={start_epoch}
  -> {"openPrice": PTB, "closePrice": live-or-final, "timestamp": ms,
      "completed": bool, ...}

openPrice  = price-to-beat (Chainlink BTC/USD fix at round open)
closePrice = latest Chainlink observation (~30s cadence) while live;
             final settlement price once completed=true.
Rule: resolves "Up" iff closePrice >= openPrice (ties go to Up).

The host is aggressively rate-limited (429 after ~10 rapid calls), so every
fetch is throttled to `poll_sec` and results are cached.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from .models import Chainlink
from .polymarket import ApiError, Http

log = logging.getLogger("updown.chainlink")

BASE = "https://polymarket.com/api/crypto/crypto-price"
HISTORY = "https://polymarket.com/api/crypto/price-history"


def hist_point_at(points: list, ts: float, lo: float = -5.0,
                  hi: float = 25.0) -> Optional[float]:
    """Closest price-history point to `ts` within [ts+lo, ts+hi] seconds."""
    if not points:
        return None
    best = None
    for pt in points:
        t = pt.get("timestamp", 0) / 1000.0
        if ts + lo <= t <= ts + hi:
            d = abs(t - ts)
            if best is None or d < best[0]:
                best = (d, pt.get("value"))
    return best[1] if best and best[1] is not None else None


def choose_ptb(open_price: Optional[float], hist_open: Optional[float],
               live_ref: Optional[float], tol_bps: float = 5.0):
    """Pick the price-to-beat from two Chainlink sources.

    openPrice (crypto-price) adalah sumber UI/produksi; first point
    price-history adalah cross-check. Jika beda > tol_bps, pilih yang lebih
    dekat ke harga Chainlink live di awal round (sanity). Returns
    (ptb, source, warning).
    """
    if open_price and hist_open:
        diff_bps = abs(open_price - hist_open) / hist_open * 1e4
        if diff_bps <= tol_bps:
            return open_price, "chainlink", ""
        warn = (f"PTB antar sumber beda {diff_bps:.1f}bps: openPrice={open_price:.2f} "
                f"vs price-history={hist_open:.2f}")
        if live_ref:
            if abs(hist_open - live_ref) < abs(open_price - live_ref):
                return hist_open, "chainlink_hist", warn + " → pakai price-history"
        return open_price, "chainlink", warn + " → pakai openPrice"
    if open_price:
        return open_price, "chainlink", ""
    if hist_open:
        return hist_open, "chainlink_hist", "openPrice tidak tersedia → price-history"
    return None, "", "tidak ada sumber PTB"


class ChainlinkFeed:
    def __init__(self, http: Http, symbol: str = "btc", poll_sec: float = 20.0):
        self.http = http
        self.symbol = symbol
        self.poll_sec = poll_sec
        self.hard_gap = min(poll_sec, 4.0)   # never hit the host faster than this
        self._cache: dict = {}          # start_ts -> Chainlink
        self._last_fetch = 0.0
        self._cooldown_until = 0.0

    def cached(self, start_ts: float) -> Optional[Chainlink]:
        return self._cache.get(int(start_ts))

    async def fetch(self, start_ts: float, force: bool = False,
                    max_age: float = 0.0) -> Optional[Chainlink]:
        """Cached fetch. `force` ignores cache age but never the per-host hard gap."""
        key = int(start_ts)
        now = time.monotonic()
        if now < self._cooldown_until:
            return self._cache.get(key)
        cached = self._cache.get(key)
        if cached and not force:
            age = time.time() - cached.received_at
            if age < (max_age or self.poll_sec):
                return cached
        if (now - self._last_fetch) < self.hard_gap:
            return cached
        self._last_fetch = now
        try:
            data = await self.http.get_json(
                BASE, params={"symbol": self.symbol, "eventStartTime": key}, tries=2)
        except ApiError as exc:
            if "429" in str(exc) or "Too Many" in str(exc):
                self._cooldown_until = time.monotonic() + 30.0
            log.warning("chainlink fetch failed: %s", exc)
            return cached
        if not isinstance(data, dict) or "openPrice" not in data:
            log.debug("chainlink payload incomplete: %s", str(data)[:120])
            return cached
        sample = Chainlink(
            open_price=float(data["openPrice"]) if data.get("openPrice") is not None else None,
            close_price=float(data["closePrice"]) if data.get("closePrice") is not None else None,
            ts_ms=int(data.get("timestamp") or time.time() * 1000),
            completed=bool(data.get("completed")),
        )
        self._cache[key] = sample
        return sample

    async def fetch_history(self, start_ts: float, max_age: float = 0.0) -> list:
        """price-history series for the round (points ~1/min, starts at round open,
        keeps growing past round end until 'now')."""
        key = ("hist", int(start_ts))
        cached = self._cache.get(key)
        if cached and (time.time() - cached.received_at) < (max_age or self.poll_sec):
            return cached.points or []
        now = time.monotonic()
        if now < self._cooldown_until or (now - self._last_fetch) < self.hard_gap:
            return cached.points if cached else []
        self._last_fetch = now
        try:
            data = await self.http.get_json(
                HISTORY, params={"symbol": self.symbol, "eventStartTime": int(start_ts)},
                tries=2)
        except ApiError as exc:
            log.warning("price-history fetch failed: %s", exc)
            return cached.points if cached else []
        points = data if isinstance(data, list) else []
        snap = Chainlink(open_price=None, close_price=None, ts_ms=0, completed=False)
        snap.points = points
        self._cache[key] = snap
        return points

    async def wait_ptb(self, start_ts: float, deadline_ts: float,
                       live_ref_fn=None, interval: float = 5.0):
        """Poll until a price-to-beat can be established (cross-validated).

        Returns (ptb, source, warning). live_ref_fn: callable -> float|None
        (harga Chainlink/spot live di awal round, utk sanity check).
        """
        ptb = src = None
        warn = ""
        while time.time() < deadline_ts:
            sample = await self.fetch(start_ts, force=True)
            hist = await self.fetch_history(start_ts, max_age=2.0)
            if not hist:
                # fetch_history bisa kena throttle host (jeda 6s setelah crypto-price)
                # — coba sekali lagi agar cross-check benar-benar jalan.
                await asyncio.sleep(self.hard_gap + 2.0)
                hist = await self.fetch_history(start_ts, max_age=2.0)
            hist_open = hist_point_at(hist, start_ts)
            live_ref = None
            if sample and sample.close_price:
                live_ref = sample.close_price
            elif live_ref_fn:
                live_ref = live_ref_fn()
            ptb, src, warn = choose_ptb(
                sample.open_price if sample else None, hist_open, live_ref)
            if ptb:
                return ptb, src, warn
            await asyncio.sleep(max(interval, self.hard_gap + 0.5))
        return ptb, src, warn

    async def wait_completed(self, start_ts: float, deadline_ts: float,
                             interval: float = 8.0) -> Optional[Chainlink]:
        """Poll after round close until Chainlink marks the window completed."""
        while time.time() < deadline_ts:
            sample = await self.fetch(start_ts, force=True)
            if sample and sample.completed and sample.open_price and sample.close_price:
                return sample
            await asyncio.sleep(max(interval, self.hard_gap + 0.5))
        return self._cache.get(int(start_ts))
