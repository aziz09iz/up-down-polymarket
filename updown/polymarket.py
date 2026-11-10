"""Polymarket API clients: Gamma (market discovery/resolution) + CLOB (books).

Endpoints verified live:
  GET https://gamma-api.polymarket.com/events?slug=btc-updown-5m-{start_epoch}
  GET https://clob.polymarket.com/book?token_id={token}
  GET https://clob.polymarket.com/fee-rate?token_id={token}
  GET https://clob.polymarket.com/prices-history?market={token}&startTs=&endTs=&fidelity=1
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional
from urllib.parse import urlparse

import aiohttp

from .models import Book, Level, Market
from .netguard import validate_url

log = logging.getLogger("updown.polymarket")

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
UA = "updown-paper-bot/0.1 (research; contact: local)"


class ApiError(Exception):
    pass


class Http:
    """aiohttp JSON GET with per-host min-interval throttling + retry/backoff."""

    def __init__(self, default_min_interval: float = 0.35):
        self._session: Optional[aiohttp.ClientSession] = None
        self._locks: dict = {}
        self._last: dict = {}
        self._min_interval = default_min_interval
        self.host_min_interval: dict = {"polymarket.com": 6.0}

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": UA, "Accept": "application/json"},
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_json(self, url: str, params: Optional[dict] = None,
                       tries: int = 3, timeout: float = 9.0) -> Any:
        await validate_url(url)          # SSRF guard (skema + host publik)
        host = urlparse(url).netloc
        lock = self._locks.setdefault(host, asyncio.Lock())
        min_iv = self.host_min_interval.get(host, self._min_interval)
        last_exc: Optional[Exception] = None
        last_status = 0
        for attempt in range(tries):
            async with lock:
                wait = min_iv - (time.monotonic() - self._last.get(host, 0.0))
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last[host] = time.monotonic()
                sess = await self.session()
                try:
                    async with sess.get(url, params=params,
                                        timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                        last_status = resp.status
                        if resp.status == 429:
                            ra = resp.headers.get("Retry-After")
                            backoff = float(ra) if ra and ra.isdigit() else 2.0 * (attempt + 1)
                            log.warning("429 from %s — backing off %.1fs", host, backoff)
                            await asyncio.sleep(min(backoff, 30.0))
                            continue
                        if resp.status >= 500:
                            log.warning("HTTP %d from %s (attempt %d/%d)",
                                        resp.status, host, attempt + 1, tries)
                            await asyncio.sleep(0.8 * (attempt + 1))
                            continue
                        if resp.status == 404:
                            return None
                        if resp.status != 200:
                            raise ApiError(f"HTTP {resp.status} from {host}")
                        text = await resp.text()
                        try:
                            return json.loads(text)
                        except json.JSONDecodeError:
                            if "<!DOCTYPE" in text[:64]:
                                raise ApiError(f"{host} returned HTML (endpoint missing?)")
                            raise
                except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                    last_exc = exc
                    log.debug("network error %s (%s), retry %d", host, exc, attempt + 1)
                    await asyncio.sleep(0.6 * (attempt + 1))
        raise ApiError(f"GET {url} failed after {tries} tries "
                       f"(last status {last_status}, last error {last_exc})")


def _f(x: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class GammaClient:
    def __init__(self, http: Http, series_slug: str = "btc-updown-5m"):
        self.http = http
        self.series = series_slug

    def slug_for(self, start_ts: int) -> str:
        return f"{self.series}-{int(start_ts)}"

    async def get_event(self, slug: str) -> Optional[dict]:
        data = await self.http.get_json(f"{GAMMA}/events", params={"slug": slug})
        if isinstance(data, list) and data:
            return data[0]
        return None

    async def market_for_start(self, start_ts: int, symbol: str = "btc",
                               fee_rate: float = 0.07) -> Optional[Market]:
        """Find the Up/Down market for the round starting at `start_ts`."""
        ev = await self.get_event(self.slug_for(start_ts))
        if not ev or not ev.get("markets"):
            return None
        m = ev["markets"][0]
        try:
            tokens = json.loads(m.get("clobTokenIds") or "[]")
            outcomes = json.loads(m.get("outcomes") or "[]")
        except json.JSONDecodeError:
            return None
        if len(tokens) != 2 or len(outcomes) != 2:
            return None
        tmap = {o.lower(): t for o, t in zip(outcomes, tokens)}
        if "up" not in tmap or "down" not in tmap:
            return None
        end_iso = m.get("endDate") or ev.get("endDate") or ""
        end_ts = start_ts + 300
        if end_iso:
            try:
                from datetime import datetime
                end_ts = datetime.fromisoformat(end_iso.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
        return Market(
            slug=m.get("slug") or ev.get("slug") or self.slug_for(start_ts),
            condition_id=m.get("conditionId", ""),
            question=m.get("question") or ev.get("title") or self.slug_for(start_ts),
            start_ts=float(start_ts),
            end_ts=float(end_ts),
            token_up=tmap["up"],
            token_down=tmap["down"],
            fee_rate=fee_rate,
            closed=bool(m.get("closed")),
        )

    async def resolved_outcome(self, slug: str) -> Optional[str]:
        """Ground-truth winner once Gamma marks the market closed."""
        ev = await self.get_event(slug)
        if not ev or not ev.get("markets"):
            return None
        m = ev["markets"][0]
        if not m.get("closed"):
            return None
        try:
            prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
            outcomes = json.loads(m.get("outcomes") or "[]")
        except (json.JSONDecodeError, ValueError):
            return None
        if len(prices) != 2 or len(outcomes) != 2:
            return None
        if max(prices) < 0.99:  # not settled yet
            return None
        return outcomes[0] if prices[0] >= prices[1] else outcomes[1]


class ClobClient:
    def __init__(self, http: Http):
        self.http = http
        self.book_cache: dict = {}

    async def book(self, token_id: str) -> Book:
        data = await self.http.get_json(f"{CLOB}/book", params={"token_id": token_id})
        if not isinstance(data, dict) or "bids" not in data:
            log.warning("bad book payload for %s…: %.160s", token_id[:12], data)
            raise ApiError(f"bad book payload for {token_id[:12]}…")
        bids = sorted((Level(_f(l["price"], 0) or 0, _f(l["size"], 0) or 0)
                       for l in data.get("bids") or []), key=lambda l: -l.price)
        asks = sorted((Level(_f(l["price"], 0) or 0, _f(l["size"], 0) or 0)
                       for l in data.get("asks") or []), key=lambda l: l.price)
        bids = [b for b in bids if b.price > 0 and b.size > 0]
        asks = [a for a in asks if a.price > 0 and a.price < 1 and a.size > 0]
        server_ts = None
        raw_ts = _f(data.get("timestamp"))
        if raw_ts and raw_ts > 1e12:      # ms epoch from CLOB
            server_ts = raw_ts / 1000.0
        b = Book(token_id=token_id, ts=time.time(), bids=bids, asks=asks,
                 server_ts=server_ts)
        self.book_cache[token_id] = b
        return b

    def get_cached_book(self, token_id: str) -> Optional[Book]:
        return self.book_cache.get(token_id)

    async def fee_rate(self, token_id: str) -> Optional[int]:
        try:
            data = await self.http.get_json(f"{CLOB}/fee-rate", params={"token_id": token_id},
                                            tries=2)
            if isinstance(data, dict):
                return int(data.get("base_fee") or 0)
        except ApiError:
            pass
        return None

    async def prices_history(self, token_id: str, start_ts: int, end_ts: int,
                             fidelity: int = 1) -> list:
        """[{t, p}] minute-ish granularity — used by backtest.py."""
        data = await self.http.get_json(
            f"{CLOB}/prices-history",
            params={"market": token_id, "startTs": start_ts, "endTs": end_ts,
                    "fidelity": fidelity}, tries=2)
        if isinstance(data, dict):
            return data.get("history") or []
        return []
