"""Polymarket CLOB market WebSocket — sumber data book utama (real-time).

Protokol ( diverifikasi live ):
  connect → wss://ws-subscriptions-clob.polymarket.com/ws/market
  subscribe → {"type":"market","assets_ids":["<token>", ...]}
  events  → [{"event_type":"book", asset_id, bids[], asks[], timestamp(ms), hash},
             {"event_type":"price_change", price_changes:[{asset_id,price,size,side}]},
             {"event_type":"last_trade_price", asset_id, price, size, side, fee_rate_bps}]
  keepalive → teks "PING"/"PONG" dua arah.

`book` = snapshot penuh (frekuensi tinggi di market 5m); `price_change` = delta
per level (size 0 = hapus). Keduanya dipakai: snapshot menimpa, delta menambal.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Optional

import aiohttp

from .models import Book, Level
from .netguard import validate_url

log = logging.getLogger("updown.clobws")

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def _levels(raw: list, side: str) -> list:
    out = []
    for l in raw or []:
        try:
            p = float(l.get("price", 0))
            s = float(l.get("size", 0))
        except (TypeError, ValueError):
            continue
        if s <= 0 or p <= 0:
            continue
        if side == "asks" and p >= 1.0:
            continue
        out.append(Level(p, s))
    out.sort(key=lambda x: -x.price if side == "bids" else x.price)
    return out


def book_from_event(evt: dict) -> Optional[Book]:
    aid = evt.get("asset_id")
    if not aid:
        return None
    server_ts = None
    try:
        raw_ts = float(evt.get("timestamp") or 0)
        if raw_ts > 1e12:
            server_ts = raw_ts / 1000.0
    except (TypeError, ValueError):
        pass
    return Book(token_id=str(aid), ts=time.time(),
                bids=_levels(evt.get("bids"), "bids"),
                asks=_levels(evt.get("asks"), "asks"),
                server_ts=server_ts)


def apply_price_changes(books: dict, evt: dict) -> int:
    """Apply delta levels in-place. Returns number of applied changes."""
    n = 0
    for ch in evt.get("price_changes") or []:
        aid = str(ch.get("asset_id") or "")
        b = books.get(aid)
        if b is None:
            continue
        try:
            price = float(ch.get("price", 0))
            size = float(ch.get("size", 0))
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        side = (ch.get("side") or "").upper()
        levels = b.bids if side == "BUY" else b.asks if side == "SELL" else None
        if levels is None:
            continue
        for i, l in enumerate(levels):
            if abs(l.price - price) < 1e-9:
                if size <= 0:
                    levels.pop(i)
                else:
                    l.size = size
                break
        else:
            if size > 0 and not (side == "SELL" and price >= 1.0):
                levels.append(Level(price, size))
                levels.sort(key=lambda x: -x.price if side == "BUY" else x.price)
        b.ts = time.time()
        n += 1
    return n


class ClobWs:
    def __init__(self):
        self.books: dict = {}          # asset_id -> Book (live)
        self.last_trade: dict = {}     # asset_id -> dict(price,size,side,ts)
        self.assets: list = []
        self.connected = False
        self.reconnects = 0
        self.events = 0
        self._task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._ws = None          # koneksi aktif (untuk update subscribe in-place)
        self._stopping = False

    # ── lifecycle ──
    async def resubscribe(self, assets: list):
        assets = [str(a) for a in assets]
        if self.connected and self._ws is not None:
            # Langganan baru di koneksi yang sama — TANPA putus/reconnect.
            # Reconnect tiap round mengosongkan buku (self.books={}) dan membuat
            # sisi favorit sering "tidak tersedia" di awal round.
            self.assets = assets
            with contextlib.suppress(Exception):
                await self._ws.send_str(json.dumps(
                    {"type": "market", "assets_ids": assets}))
            return
        await self.stop()
        self.assets = assets
        self.books = {}
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="clob-ws")

    async def stop(self):
        self._stopping = True
        for t in (self._ping_task, self._task):
            if t:
                t.cancel()
        for t in (self._ping_task, self._task):
            if t:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        self._task = self._ping_task = None
        self.connected = False

    async def _run(self):
        backoff = 1.0
        while not self._stopping:
            try:
                await validate_url(WS_URL)
                async with aiohttp.ClientSession() as sess:
                    async with sess.ws_connect(
                            WS_URL, heartbeat=25, max_msg_size=16 * 1024 * 1024,
                            timeout=aiohttp.ClientWSTimeout(ws_receive=30)) as ws:
                        await ws.send_str(json.dumps(
                            {"type": "market", "assets_ids": self.assets}))
                        self.connected = True
                        self._ws = ws
                        backoff = 1.0
                        log.info("CLOB WS connected (%d assets)", len(self.assets))
                        self._ping_task = asyncio.create_task(self._pinger(ws))
                        try:
                            async for msg in ws:
                                if self._stopping:
                                    break
                                if msg.type == aiohttp.WSMsgType.TEXT:
                                    data = msg.data
                                    if data == "PING":
                                        await ws.send_str("PONG")
                                        continue
                                    if data == "PONG":
                                        continue
                                    self._handle(data)
                                elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                                  aiohttp.WSMsgType.CLOSE,
                                                  aiohttp.WSMsgType.ERROR):
                                    log.warning("CLOB WS closed (%s)", msg.type)
                                    break
                        finally:
                            self._ws = None
                            if self._ping_task:
                                self._ping_task.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("CLOB WS error (%s): %s", type(exc).__name__, exc)
            self.connected = False
            if self._stopping:
                break
            self.reconnects += 1
            await asyncio.sleep(min(backoff, 30.0))
            backoff *= 2
        log.info("CLOB WS stopped")

    async def _pinger(self, ws):
        try:
            while True:
                await asyncio.sleep(10)
                await ws.send_str("PING")
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    # ── events ──
    def _handle(self, data: str):
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return
        events = payload if isinstance(payload, list) else [payload]
        for evt in events:
            if not isinstance(evt, dict):
                continue
            et = evt.get("event_type")
            self.events += 1
            if et == "book":
                b = book_from_event(evt)
                if b:
                    self.books[b.token_id] = b
            elif et == "price_change":
                apply_price_changes(self.books, evt)
            elif et == "last_trade_price":
                aid = str(evt.get("asset_id") or "")
                if aid:
                    self.last_trade[aid] = {
                        "price": evt.get("price"), "size": evt.get("size"),
                        "side": evt.get("side"), "ts": time.time(),
                        "fee_rate_bps": evt.get("fee_rate_bps")}

    # ── reads ──
    def get_book(self, asset_id: str, max_age: float = 3.0) -> Optional[Book]:
        b = self.books.get(str(asset_id))
        if b and (time.time() - b.ts) <= max_age and (b.bids or b.asks):
            return b
        return None

    def book_age(self, asset_id: str) -> float:
        b = self.books.get(str(asset_id))
        return time.time() - b.ts if b else 1e9
