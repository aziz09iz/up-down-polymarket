"""Fast BTC spot feeds (exchange) + Chainlink calibration.

The strategy needs a sub-second BTC price, but Polymarket settles on Chainlink.
We track a fast exchange feed (Binance WS bookTicker by default) and keep an
EWMA offset `chainlink - exchange` from every fresh Chainlink sample, so:

    calibrated_chainlink_est = exchange_mid + offset

Realtime vol (bps per sqrt(second)) is estimated from exchange log-returns for
the optional EV filter.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import time
from collections import deque
from typing import Optional

import aiohttp

from .models import Spot
from .netguard import validate_url

log = logging.getLogger("updown.spot")

BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@bookTicker"

REST_SOURCES = {
    "binance": ("https://api.binance.com/api/v3/ticker/bookTicker",
                {"symbol": "BTCUSDT"},
                lambda d: (float(d["bidPrice"]), float(d["askPrice"]))),
    "bybit": ("https://api.bybit.com/v5/market/tickers",
              {"category": "spot", "symbol": "BTCUSDT"},
              lambda d: (float(d["result"]["list"][0]["bid1Price"]),
                         float(d["result"]["list"][0]["ask1Price"]))),
    "coinbase": ("https://api.coinbase.com/v2/prices/BTC-USD/spot", {},
                 lambda d: (float(d["data"]["amount"]),) * 2),
    "okx": ("https://www.okx.com/api/v5/market/ticker", {"instId": "BTC-USDT"},
            lambda d: (float(d["data"][0]["bidPx"]), float(d["data"][0]["askPx"]))),
}


class SpotTracker:
    """Holds the latest quote, a short history, offset calibration and vol estimate."""

    def __init__(self, name: str):
        self.name = name
        self.last: Optional[Spot] = None
        self.history: deque = deque(maxlen=2000)     # (ts, mid)
        self.offset = 0.0                            # chainlink - exchange (EWMA)
        self.offset_samples = 0
        self.offset_updated = 0.0
        self._var_per_s = 0.0
        self._vol_ready = False
        self.ok = False

    # ── updates ──
    def update(self, ts: float, bid: Optional[float], ask: Optional[float], source: str = ""):
        if bid and ask:
            mid = (bid + ask) / 2
        elif bid:
            mid = bid
        elif ask:
            mid = ask
        else:
            return
        now = time.time()
        s = Spot(ts=ts or now, mid=mid, bid=bid, ask=ask, source=source or self.name)
        prev = self.last
        self.last = s
        self.ok = True
        self.history.append((s.ts, mid))
        # EWMA variance of log returns -> bps / sqrt(s)
        if prev and 0.05 < (s.ts - prev.ts) < 5.0 and prev.mid > 0:
            r = math.log(mid / prev.mid)
            dt = s.ts - prev.ts
            a = 0.05
            self._var_per_s = (1 - a) * self._var_per_s + a * (r * r / dt)
            self._vol_ready = self._var_per_s > 0
        return s

    def calibrate(self, chainlink_price: float, chainlink_ts_ms: int, alpha: float = 0.3):
        """Feed a fresh Chainlink observation; update EWMA offset."""
        ts = chainlink_ts_ms / 1000.0
        ex_mid = self.mid_at(ts)
        if ex_mid is None:
            return None
        delta = chainlink_price - ex_mid
        if self.offset_samples == 0:
            self.offset = delta
        else:
            self.offset = (1 - alpha) * self.offset + alpha * delta
        self.offset_samples += 1
        self.offset_updated = time.time()
        return delta

    def mid_at(self, ts: float, max_age: float = 10.0) -> Optional[float]:
        """Exchange mid closest to `ts` (search from the tail)."""
        best = None
        best_d = 1e18
        for t, m in reversed(self.history):
            d = abs(t - ts)
            if d < best_d:
                best_d, best = d, m
            if t < ts - max_age:
                break
        return best if best_d <= max_age else None

    # ── reads ──
    @property
    def mid(self) -> Optional[float]:
        return self.last.mid if self.last else None

    @property
    def age_s(self) -> float:
        return time.time() - self.last.ts if self.last else 1e9

    @property
    def calibrated_mid(self) -> Optional[float]:
        return self.last.mid + self.offset if self.last else None

    def vol_bps_sqrt_s(self, default: float = 0.75, lo: float = 0.3, hi: float = 3.0) -> float:
        if not self._vol_ready:
            return default
        return min(hi, max(lo, math.sqrt(self._var_per_s) * 1e4))


class WsFeed:
    """Binance bookTicker WebSocket with auto-reconnect; gives up -> REST fallback."""

    def __init__(self, tracker: SpotTracker, on_permanent_fail=None):
        self.tracker = tracker
        self.on_permanent_fail = on_permanent_fail
        self._task: Optional[asyncio.Task] = None
        self.connected = False
        self.gave_up = False

    async def start(self):
        self._task = asyncio.create_task(self._run(), name="binance-ws")

    async def stop(self):
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self):
        fails = 0
        while True:
            try:
                await validate_url(BINANCE_WS)
                async with aiohttp.ClientSession() as sess:
                    async with sess.ws_connect(BINANCE_WS, heartbeat=30,
                                               timeout=aiohttp.ClientWSTimeout(ws_receive=15)) as ws:
                        self.connected = True
                        fails = 0
                        log.info("binance WS connected")
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    d = json.loads(msg.data)
                                    bid = float(d["b"]) if d.get("b") else None
                                    ask = float(d["a"]) if d.get("a") else None
                                    self.tracker.update(time.time(), bid, ask, "binance-ws")
                                except (KeyError, ValueError, json.JSONDecodeError):
                                    continue
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                fails += 1
                log.warning("binance WS error (%s): %s", type(exc).__name__, exc)
            self.connected = False
            if fails >= 3 and not self.gave_up:
                self.gave_up = True
                log.error("binance WS failed %d× — falling back to REST polling", fails)
                if self.on_permanent_fail:
                    self.on_permanent_fail()
                return
            await asyncio.sleep(min(2.0 * fails, 15.0))


class RestPoller:
    def __init__(self, tracker: SpotTracker, source: str, poll_sec: float,
                 session_factory=None):
        if source not in REST_SOURCES:
            raise ValueError(f"unknown spot source {source!r} "
                             f"(choose: {', '.join(REST_SOURCES)})")
        self.tracker = tracker
        self.source = source
        self.poll_sec = poll_sec
        self.url, self.params, self.parse = REST_SOURCES[source]
        self._task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self.errors = 0

    async def start(self):
        self._task = asyncio.create_task(self._run(), name=f"rest-{self.source}")

    async def stop(self):
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._session and not self._session.closed:
            await self._session.close()

    async def _run(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=6),
            headers={"User-Agent": "updown-paper-bot/0.1"})
        await validate_url(self.url)
        while True:
            # if a faster feed (WS) keeps the tracker fresh, poll lazily
            tr = self.tracker
            other_fresh = (tr.last is not None and tr.last.source != self.source
                           and tr.age_s < self.poll_sec * 0.5)
            if not other_fresh:
                try:
                    async with self._session.get(self.url, params=self.params) as resp:
                        if resp.status == 429:
                            self.errors += 1
                            await asyncio.sleep(5.0)
                            continue
                        if resp.status == 200:
                            d = await resp.json(content_type=None)
                            bid, ask = self.parse(d)
                            self.tracker.update(time.time(), bid, ask, self.source)
                            self.errors = 0
                        else:
                            self.errors += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.errors += 1
                    if self.errors in (1, 5, 20):
                        log.warning("REST %s error: %s", self.source, exc)
            await asyncio.sleep(self.poll_sec * (3.0 if other_fresh else 1.0))


class SpotManager:
    """Primary (WS+REST) and optional secondary (cross-check) spot feeds."""

    def __init__(self, primary: str = "binance", use_ws: bool = True,
                 poll_sec: float = 1.0, secondary: str = "coinbase",
                 secondary_poll_sec: float = 3.0):
        self.primary = SpotTracker(primary)
        self.secondary = SpotTracker(secondary) if secondary else None
        self._pollers: list = []
        self._ws: Optional[WsFeed] = None
        self._use_ws = use_ws and primary == "binance"
        self._primary_name = primary
        self._poll_sec = poll_sec
        self._secondary_poll = secondary_poll_sec

    async def start(self):
        # REST poller always runs (safety net); WS on top when enabled.
        # The poller slows itself down automatically while WS keeps data fresh.
        rest = RestPoller(self.primary, self._primary_name, self._poll_sec)
        self._pollers.append(rest)
        await rest.start()
        if self._use_ws:
            self._ws = WsFeed(self.primary, on_permanent_fail=self._on_ws_gave_up)
            await self._ws.start()
        if self.secondary:
            p = RestPoller(self.secondary, self.secondary.name, self._secondary_poll)
            self._pollers.append(p)
            await p.start()

    def _on_ws_gave_up(self):
        log.warning("WS feed nonaktif — REST poller mengambil alih (%.1fs)",
                    self._poll_sec)

    async def stop(self):
        if self._ws:
            await self._ws.stop()
        for p in self._pollers:
            await p.stop()

    @property
    def divergence_bps(self) -> Optional[float]:
        if not (self.secondary and self.primary.mid and self.secondary.mid):
            return None
        if self.secondary.age_s > 15:
            return None
        return abs(self.primary.mid - self.secondary.mid) / self.secondary.mid * 1e4
