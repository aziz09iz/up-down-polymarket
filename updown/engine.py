"""Round orchestrator.

Lifecycle per 5-minute round:
  discovery (T-100s)  →  PTB fetch (T+0)  →  monitor (books + calibrated spot)
  →  entry window (E-90s..E-30s)  →  resolution (chainlink completed / gamma closed)
  →  payout + notifications.  Rounds run as concurrent tasks so slow resolution
never blocks the next round. Restart mid-round can still join (PTB is stable).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Optional

from . import format as F
from .chainlink import ChainlinkFeed, hist_point_at
from .clobws import ClobWs
from .config import Config
from .models import Book, Market
from .paper import PaperBroker
from .polymarket import ApiError, ClobClient, GammaClient, Http
from .spot import SpotManager
from .stats import compute_stats
from .store import Store
from .strategy import LateWindowStrategy, effective_price

log = logging.getLogger("updown.engine")

CALIB_SCHEDULE = (120, 165, 210, 240, 265)   # seconds after round start


class Engine:
    def __init__(self, cfg: Config, notifier=None):
        self.cfg = cfg
        self.notifier = notifier
        self.store = Store(cfg.data_dir / "updown.db")
        self.broker = PaperBroker(cfg, self.store)
        self.http = Http()
        self.gamma = GammaClient(self.http, cfg.market.series_slug)
        self.clob = ClobClient(self.http)
        self.chainlink = ChainlinkFeed(self.http, cfg.market.symbol,
                                       cfg.feeds.chainlink_poll_sec)
        self.spot = SpotManager(cfg.feeds.spot_primary, cfg.feeds.spot_ws,
                                cfg.feeds.spot_poll_sec, cfg.feeds.spot_secondary,
                                cfg.feeds.spot_secondary_poll_sec)
        self.clob_ws = ClobWs()
        self.strategy = LateWindowStrategy(cfg)
        self.paused = False
        self.running = True
        self.current: Optional[Market] = None
        self.status_extra = {}
        self._round_tasks: list = []
        self._bg_tasks: list = []
        self._last_skip_notify = 0.0
        self.rounds_spawned = 0
        self.rounds_done = 0

    # ── time helpers ──
    def now(self) -> float:
        return self.cfg.now()

    async def sleep_until(self, ts: float):
        while self.running:
            d = ts - self.now()
            if d <= 0:
                return
            await asyncio.sleep(min(d, 5.0))

    async def notify(self, text: str):
        if self.notifier:
            with contextlib.suppress(Exception):
                await self.notifier.send(text)

    async def alert(self, msg: str):
        log.error(msg)
        if self.cfg.telegram.notify_errors:
            await self.notify(F.error_html(msg))

    def set_paused(self, v: bool):
        self.paused = v
        log.info("engine %s", "PAUSED" if v else "RESUMED")

    # ── main loop ──
    async def run(self, rounds_limit: Optional[int] = None):
        log.info("═══ Polymarket BTC 5m Up/Down — PAPER bot start ═══")
        log.info("balance=%.2f stake=%.2f window=%d-%ds lead≥%.1fbps eff≤%.3f fee=%.2f",
                 self.broker.balance, self.broker.stake_for(),
                 self.cfg.strategy.window_start_s, self.cfg.strategy.window_end_s,
                 self.cfg.strategy.min_lead_bps, self.cfg.strategy.max_eff_price,
                 self.cfg.market.fee_rate)
        await self.spot.start()
        self.broker.bootstrap_from_store()
        # sanity: stake harus cukup utk memenuhi min order di harga favorit
        min_stake = self.cfg.market.min_order_shares * self.cfg.strategy.max_eff_price
        eff_stake = self.broker.stake_for()
        if eff_stake < min_stake:
            log.error("STAKE terlalu kecil: stake=%.2f < min=%.2f (5 share × %.2f). "
                      "Entry sisi favorit akan selalu ditolak!", eff_stake, min_stake,
                      self.cfg.strategy.max_eff_price)
            await self.alert(f"STAKE_USDC terlalu kecil ({eff_stake:.2f}) — beli "
                             f"favorit ≤ {self.cfg.strategy.max_eff_price} butuh "
                             f"≥ {min_stake:.2f} (min {self.cfg.market.min_order_shares} "
                             "share). Naikkan STAKE_USDC atau turunkan max_eff_price.")
        self._bg_tasks.append(asyncio.create_task(self._background_resolver(),
                                                  name="bg-resolver"))
        self._bg_tasks.append(asyncio.create_task(self._daily_summary_loop(),
                                                  name="daily-summary"))
        try:
            while self.running and (rounds_limit is None
                                    or self.rounds_spawned < rounds_limit):
                now = self.now()
                start = int(now // 300) * 300
                if now - start > 200:          # terlalu telat untuk join round ini
                    start += 300
                await self.sleep_until(start - 100)
                if not self.running or (rounds_limit is not None
                                        and self.rounds_spawned >= rounds_limit):
                    break
                self.rounds_spawned += 1
                t = asyncio.create_task(self.process_round(start),
                                        name=f"round-{start}")
                self._round_tasks.append(t)
                self._round_tasks = [x for x in self._round_tasks if not x.done()]
                await self.sleep_until(start + 200)
        except asyncio.CancelledError:
            self.running = False
            raise
        finally:
            log.info("menunggu %d round task selesai…", len(self._round_tasks))
            await asyncio.gather(*self._round_tasks, return_exceptions=True)
            for t in self._bg_tasks:
                t.cancel()
            # sapuan final: jangan tinggalkan posisi menggantung saat berhenti
            if self.broker.open_positions:
                sweep_deadline = time.time() + 240
                while self.broker.open_positions and time.time() < sweep_deadline:
                    log.info("final sweep: %d posisi menunggu resolusi…",
                             len(self.broker.open_positions))
                    try:
                        n = await self._resolve_pending_once()
                    except Exception:  # noqa: BLE001
                        log.exception("final sweep error")
                        n = 0
                    if not n and self.broker.open_positions:
                        await asyncio.sleep(25)
                if self.broker.open_positions:
                    log.warning("%d posisi masih OPEN — akan diselesaikan pada "
                                "start berikutnya (background resolver)",
                                len(self.broker.open_positions))
            await self.spot.stop()
            await self.clob_ws.stop()
            await self.http.close()
            self.store.close()
            log.info("═══ engine stopped ═══")

    def stop(self):
        self.running = False

    # ── one round ──
    async def process_round(self, start_ts: int):
        slug = self.gamma.slug_for(start_ts)
        end_ts = start_ts + 300
        mkt: Optional[Market] = None
        observe_task: Optional[asyncio.Task] = None
        try:
            # 1) discovery
            deadline = start_ts + 120
            while self.now() < deadline and self.running:
                with contextlib.suppress(ApiError):
                    mkt = await self.gamma.market_for_start(
                        start_ts, self.cfg.market.symbol, self.cfg.market.fee_rate)
                if mkt:
                    break
                await asyncio.sleep(5)
            if not mkt:
                await self.alert(f"Market {slug} tidak ditemukan (Gamma).")
                return
            mkt.end_ts = max(mkt.end_ts, end_ts)
            end_ts = mkt.end_ts
            self.current = mkt
            log.info("round %s ditemukan (end %s)", mkt.slug,
                     time.strftime("%H:%M:%S", time.gmtime(end_ts)))
            base_fee = await self.clob.fee_rate(mkt.token_up)
            if base_fee is not None:
                log.debug("CLOB base_fee=%s bps (formula docs: rate=%.2f)",
                          base_fee, mkt.fee_rate)
            # WebSocket book = sumber utama; REST /book hanya fallback
            await self.clob_ws.resubscribe([mkt.token_up, mkt.token_down])

            # 2) tunggu open + PTB (cross-validated: openPrice vs price-history)
            await self.sleep_until(start_ts)
            ptb, ptb_src, ptb_warn = await self.chainlink.wait_ptb(
                start_ts, start_ts + 60,
                live_ref_fn=lambda: (self.spot.primary.calibrated_mid
                                     or self.spot.primary.mid))
            if ptb:
                mkt.ptb, mkt.ptb_source = ptb, ptb_src
                if ptb_warn:
                    # quirk umum API Polymarket (openPrice telat/basi) dan sudah
                    # ditangani otomatis — cukup log, jangan spam Telegram.
                    log.warning("PTB %s: %s (dipakai %.2f)", mkt.slug, ptb_warn, ptb)
                s = self.chainlink.cached(start_ts)
                if s and s.close_price:
                    self._calibrate(s.close_price, s.ts_ms)
            else:
                if self.spot.primary.mid:
                    mkt.ptb = self.spot.primary.mid
                    mkt.ptb_source = "spot_fallback"
                    await self.alert(f"PTB Chainlink {slug} tidak terbit — "
                                     f"pakai snapshot spot {mkt.ptb:,.2f} "
                                     "(fidelity turun).")
                else:
                    await self.alert(f"Round {slug}: tidak ada PTB maupun spot — skip.")
                    return
            self.store.upsert_round(slug=mkt.slug, start_ts=start_ts, end_ts=end_ts,
                                    ptb=mkt.ptb, ptb_source=mkt.ptb_source,
                                    observed_ts=time.time())
            log.info("PTB %s = %.2f (%s) | offset CL−ex %.2f | spot %s age %.1fs",
                     mkt.slug, mkt.ptb, mkt.ptb_source, self.spot.primary.offset,
                     self.spot.primary.last.source if self.spot.primary.last else "-",
                     self.spot.primary.age_s)

            # 3) monitor loop
            observe_task = asyncio.create_task(self._observe_lead(mkt))
            traded = await self._monitor(mkt)

            # 4) resolution
            winner, src, close_cl = await self._resolve(mkt)
            if winner:
                await self._settle(mkt, winner, src, close_cl, traded)
            else:
                # background resolver mengirim notifikasi WIN/LOSS saat settle —
                # tidak perlu alert ke Telegram di sini.
                log.info("Resolusi %s tertunda — background resolver", slug)
            self.broker.on_round_end()
            self._snapshot_equity()
            self.rounds_done += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("round %s error", slug)
            await self.alert(f"Error round {slug}: {type(exc).__name__}: {exc}")
        finally:
            if observe_task:
                observe_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await observe_task
            if self.current and self.current.slug == slug:
                self.current = None
                self.status_extra = {}

    async def _observe_lead(self, mkt: Market):
        """Fast task: track lead stability at ~2.5Hz using calibrated spot."""
        while self.running and self.now() < mkt.end_ts:
            live = self.spot.primary.calibrated_mid
            if mkt.ptb and live:
                self.strategy.observe((live - mkt.ptb) / mkt.ptb * 1e4, self.now())
            else:
                self.strategy.observe(None, self.now())
            await asyncio.sleep(0.4)

    async def _monitor(self, mkt: Market) -> bool:
        """Poll books, evaluate strategy in the entry window. Returns traded?"""
        s = self.cfg.strategy
        traded = False
        skip_reason = ""
        books: dict = {"Up": None, "Down": None}
        last_other = 0.0
        calib_idx = 0
        next_snap = 0.0
        fav_side_hint = None
        book_errs = 0

        async def fetch_book(token: str) -> Optional[Book]:
            nonlocal book_errs
            for attempt in (1, 2):
                try:
                    return await self.clob.book(token)
                except ApiError as exc:
                    book_errs += 1
                    if book_errs <= 3 or book_errs % 25 == 0:
                        log.warning("book fetch gagal (%d×): %s", book_errs, exc)
                    if attempt == 1:
                        await asyncio.sleep(0.3)
            return None

        async def take_book(side: str) -> Optional[Book]:
            """WS book dulu (real-time), fallback REST /book."""
            b = self.clob_ws.get_book(mkt.token(side), max_age=3.0)
            return b or await fetch_book(mkt.token(side))

        while self.running:
            now = self.now()
            t_left = mkt.end_ts - now
            if t_left <= 3:
                break
            in_win = s.window_end_s <= t_left <= s.window_start_s
            near_win = t_left <= s.window_start_s + 15
            interval = self.cfg.feeds.book_poll_sec if near_win \
                else self.cfg.feeds.book_idle_poll_sec

            live = self.spot.primary.calibrated_mid
            lead = (live - mkt.ptb) / mkt.ptb * 1e4 if (live and mkt.ptb) else None
            fav = None
            if lead is not None:
                fav = "Up" if lead > 0 else "Down"
                fav_side_hint = fav

            # ── book fetching (WS utama, REST /book fallback) ──
            if near_win and fav:
                b = await take_book(fav)
                if b:
                    books[fav] = b
                if now - last_other > 10:
                    other = "Down" if fav == "Up" else "Up"
                    b2 = await take_book(other)
                    if b2:
                        books[other] = b2
                    last_other = now
            elif now - last_other > self.cfg.feeds.book_idle_poll_sec * 2:
                side = fav_side_hint or "Up"
                b = await take_book(side)
                if b:
                    books[side] = b
                last_other = now

            self._clock_sync(books)
            self._update_marks(mkt, books)

            # ── chainlink calibration samples ──
            while calib_idx < len(CALIB_SCHEDULE) and \
                    now >= mkt.start_ts + CALIB_SCHEDULE[calib_idx]:
                sample = await self.chainlink.fetch(mkt.start_ts, force=True)
                if sample and sample.close_price:
                    self._calibrate(sample.close_price, sample.ts_ms)
                calib_idx += 1

            # ── flip exit: jual early jika lead berbalik melawan posisi ──
            pos = next((p for p in self.broker.open_positions
                        if p.market_slug == mkt.slug), None)
            if pos and self.strategy.should_flip_exit(pos.side, lead, t_left):
                signed = lead if pos.side == "Up" else -lead
                # refresh buku sisi POSISI (WS fresh), fallback buku terakhir
                book = await take_book(pos.side) or books.get(pos.side)
                if book and book.bids and \
                        self.broker.market_sell(pos, book, "flip_exit"):
                    await self.notify(
                        f"➖ <b>FLIP EXIT {F.esc(pos.side)}</b>\n"
                        f"Lead berbalik {signed:+.1f} bps"
                        f" (batas -{s.flip_exit_bps}) · t-{t_left:.0f}s\n"
                        f"PnL {F.usd(pos.pnl_net, sign=True)} USDC")
                    log.info("flip exit %s pnl=%.2f", mkt.slug, pos.pnl_net)
                elif not book or not book.bids:
                    log.warning("flip exit %s ditahan: buku sisi %s kosong",
                                mkt.slug, pos.side)

            # ── entry evaluation ──
            if in_win and not traded and fav:
                if self.paused:
                    skip_reason = "paused (manual)"
                else:
                    can, why = self.broker.can_trade()
                    if not can:
                        skip_reason = why
                    else:
                        # refresh buku KEDUA sisi (WS fresh) utk cek lead vs pasar
                        for s2 in ("Up", "Down"):
                            b2 = self.clob_ws.get_book(mkt.token(s2), max_age=3.0)
                            if b2 is None:
                                b2 = await take_book(s2)
                            if b2:
                                books[s2] = b2
                        d = self.strategy.evaluate(
                            t_left=t_left, ptb=mkt.ptb, live_price=live,
                            spot_age=self.spot.primary.age_s,
                            divergence_bps=self.spot.divergence_bps,
                            now=now, books=books,
                            vol_bps_sqrt_s=self.spot.primary.vol_bps_sqrt_s(
                                s.vol_default_bps_sqrt_s, s.vol_min_bps_sqrt_s,
                                s.vol_max_bps_sqrt_s))
                        if d.ok:
                            stake = self.broker.stake_for()
                            fresh = await take_book(d.side)
                            book = fresh or books.get(d.side)
                            pos = self.broker.market_buy(
                                mkt, d.side, book, stake,
                                t_left=t_left, lead_bps=d.metrics.get("lead_bps", 0),
                                best_ask_at_signal=d.metrics.get("ask", 0),
                                ptb=mkt.ptb, spot=live or 0)
                            if pos:
                                traded = True
                                skip_reason = ""
                                self.store.upsert_round(
                                    slug=mkt.slug, start_ts=mkt.start_ts,
                                    end_ts=mkt.end_ts, ptb=mkt.ptb,
                                    ptb_source=mkt.ptb_source, traded=1,
                                    our_side=pos.side, observed_ts=mkt.start_ts)
                                if self.cfg.telegram.notify_entry:
                                    await self.notify(F.entry_html(
                                        pos, mkt.question,
                                        stable_for=d.metrics.get("stable_for", 0),
                                        live=live or 0,
                                        balance=self.broker.balance))
                            else:
                                skip_reason = "order paper ditolak (min size/depth)"
                        elif d.action == "skip":
                            skip_reason = "; ".join(d.failed_rules()[:3])
                            if self.cfg.telegram.notify_skips and \
                                    now - self._last_skip_notify > 20:
                                self._last_skip_notify = now
                                await self.notify(
                                    f"Skip <code>{F.esc(mkt.slug[-9:])}</code>"
                                    f" · t-{t_left:.0f}s — {F.esc(skip_reason)}")
                if not traded and live is None:
                    skip_reason = "spot feed tidak tersedia"
            elif not traded and t_left < s.window_end_s and not skip_reason:
                skip_reason = "window lewat tanpa sinyal"

            # ── status + equity ──
            self._update_status(mkt, t_left, live, lead, books, fav, skip_reason)
            if now >= next_snap:
                self._snapshot_equity()
                next_snap = now + 30
            await asyncio.sleep(interval)

        if not traded and skip_reason:
            self.store.upsert_round(slug=mkt.slug, start_ts=mkt.start_ts,
                                    end_ts=mkt.end_ts, ptb=mkt.ptb,
                                    ptb_source=mkt.ptb_source, traded=0,
                                    skipped_reason=skip_reason[:120],
                                    observed_ts=mkt.start_ts)
            log.info("round %s: no trade — %s", mkt.slug, skip_reason)
        return traded

    # ── resolution ──
    async def _resolve(self, mkt: Market, deadline_extra: float = 300.0):
        """Returns (winner, source, chainlink_close). First definitive wins."""
        deadline = mkt.end_ts + deadline_extra
        winner_cl = close_cl = None
        tried_gamma = 0.0
        last_cl = 0.0
        while self.now() < deadline:
            # chainlink dipolling jarang (host situs rate-limited); gamma tiap 12s
            if self.now() - last_cl >= 20:
                last_cl = self.now()
                cl = await self.chainlink.fetch(mkt.start_ts, force=True)
                if cl and cl.completed and cl.open_price and cl.close_price:
                    close_cl = cl.close_price
                    winner_cl = "Up" if cl.close_price >= cl.open_price else "Down"
            if self.now() - tried_gamma > 12:
                tried_gamma = self.now()
                with contextlib.suppress(ApiError):
                    gam = await self.gamma.resolved_outcome(mkt.slug)
                if gam:
                    if winner_cl and gam != winner_cl:
                        log.warning("gamma(%s) != chainlink(%s) utk %s — pakai gamma",
                                    gam, winner_cl, mkt.slug)
                    if not close_cl:
                        cl2 = self.chainlink.cached(mkt.start_ts)
                        close_cl = cl2.close_price if cl2 else None
                    return gam, "gamma", close_cl
            if winner_cl:
                # beri gamma kesempatan singkat utk konfirmasi, tapi jangan lama
                await asyncio.sleep(3)
                return winner_cl, "chainlink", close_cl
            await asyncio.sleep(6)
        # fallback terakhir: price-history (point di open vs point di end)
        hist = await self.chainlink.fetch_history(mkt.start_ts, max_age=2.0)
        p_open = hist_point_at(hist, mkt.start_ts)
        p_end = hist_point_at(hist, mkt.end_ts, lo=-25.0, hi=25.0)
        if p_open and p_end:
            w = "Up" if p_end >= p_open else "Down"
            log.warning("resolusi %s via price-history fallback → %s", mkt.slug, w)
            return w, "chainlink_hist", p_end
        return winner_cl, ("chainlink" if winner_cl else ""), close_cl

    async def _settle(self, mkt: Market, winner: str, src: str,
                      close_cl: Optional[float], traded: bool):
        mkt.winner, mkt.close_chainlink, mkt.resolution_source = winner, close_cl, src
        settled = self.broker.resolve(mkt.slug, winner, src)
        pnl = sum(p.pnl_net for p in settled)
        self.store.upsert_round(slug=mkt.slug, start_ts=mkt.start_ts,
                                end_ts=mkt.end_ts, ptb=mkt.ptb,
                                ptb_source=mkt.ptb_source, close_chainlink=close_cl,
                                winner=winner, resolution_source=src,
                                traded=1 if traded else 0,
                                our_side=settled[0].side if settled else None,
                                pnl=round(pnl, 4), observed_ts=mkt.start_ts)
        log.info("round %s → %s (%s)%s", mkt.slug, winner, src,
                 f" pnl {pnl:+.2f}" if settled else "")
        if self.cfg.telegram.notify_resolution:
            for pos in settled:
                day_rows = self.store.positions_since(self._day_start())
                day = compute_stats(day_rows)
                total = compute_stats(self.store.all_closed(),
                                      self.store.equity_series(),
                                      self.cfg.paper.start_balance)
                await self.notify(F.resolution_html(
                    pos, winner, ptb=mkt.ptb, close=close_cl, res_source=src,
                    day=day, total=total, balance=self.broker.balance))

    async def _resolve_pending_once(self) -> int:
        """Satu sapuan resolusi utk posisi yang round-nya sudah lewat."""
        resolved = 0
        for pos in list(self.broker.open_positions):
            try:
                start = int(pos.market_slug.rsplit("-", 1)[1])
            except (IndexError, ValueError):
                continue
            if self.now() < start + 300 + 45:
                continue
            gam = None
            with contextlib.suppress(ApiError):
                gam = await self.gamma.resolved_outcome(pos.market_slug)
            if not gam:
                cl = await self.chainlink.fetch(start, force=True)
                if cl and cl.completed and cl.open_price and cl.close_price:
                    gam = "Up" if cl.close_price >= cl.open_price else "Down"
            if not gam:
                hist = await self.chainlink.fetch_history(start, max_age=2.0)
                p_open = hist_point_at(hist, start)
                p_end = hist_point_at(hist, start + 300, lo=-25.0, hi=25.0)
                if p_open and p_end:
                    gam = "Up" if p_end >= p_open else "Down"
            if gam:
                settled = self.broker.resolve(pos.market_slug, gam, "background")
                for p in settled:
                    self._merge_round(p.market_slug, start, winner=gam,
                                      our_side=p.side, pnl=p.pnl_net,
                                      resolution_source="background")
                    await self.notify(F.resolution_html(
                        p, gam, ptb=p.ptb, close=None,
                        res_source="background", balance=self.broker.balance))
                    resolved += 1
                log.info("background resolved %s → %s", pos.market_slug, gam)
        return resolved

    def _merge_round(self, slug: str, start: int, **kw):
        """upsert_round tanpa menimpa kolom existing dengan None (merge)."""
        row = self.store.get_round(slug) or {}
        merged = {
            "slug": slug,
            "start_ts": row.get("start_ts") or start,
            "end_ts": row.get("end_ts") or start + 300,
            "ptb": row.get("ptb"),
            "ptb_source": row.get("ptb_source") or "",
            "close_chainlink": row.get("close_chainlink"),
            "winner": row.get("winner"),
            "resolution_source": row.get("resolution_source") or "",
            "traded": row.get("traded") or 0,
            "our_side": row.get("our_side"),
            "pnl": row.get("pnl") or 0.0,
            "skipped_reason": row.get("skipped_reason") or "",
            "observed_ts": row.get("observed_ts"),
        }
        merged.update({k: v for k, v in kw.items() if v is not None})
        self.store.upsert_round(**merged)

    async def _background_resolver(self):
        while self.running:
            try:
                await asyncio.sleep(45)
                await self._resolve_pending_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("background resolver error")

    # ── daily summary ──
    async def _daily_summary_loop(self):
        hour = self.cfg.telegram.summary_utc_hour
        if hour < 0:
            return
        while self.running:
            try:
                now = time.time()
                # next occurrence of `hour` UTC
                base = int(now // 86400) * 86400 + hour * 3600
                nxt = base if base > now else base + 86400
                await asyncio.sleep(max(30.0, nxt - now))
                path = self.write_daily_report()
                day_rows = self.store.positions_since(self._day_start() - 86400)
                st = compute_stats(day_rows)
                await self.notify(
                    f"<b>Ringkasan harian — {F.utc_ymd(time.time() - 3600)}</b>\n"
                    + F.stats_html(st, None)
                    + f"\nLaporan: <code>{F.esc(path)}</code>")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("daily summary error")
                await asyncio.sleep(3600)

    @staticmethod
    def _day_start() -> float:
        return time.time() - (time.time() % 86400)

    def write_daily_report(self) -> str:
        day = F.utc_ymd(time.time())
        day_rows = self.store.positions_since(self._day_start())
        all_rows = self.store.all_closed()
        st_day = compute_stats(day_rows)
        st_all = compute_stats(all_rows, self.store.equity_series(),
                               self.cfg.paper.start_balance)
        md = F.daily_report_md(day, st_day, st_all, day_rows or all_rows[-30:],
                               self.store.recent_rounds(30))
        path = self.cfg.data_dir / "reports" / f"{day}.md"
        path.write_text(md, encoding="utf-8")
        return str(path)

    # ── small helpers ──
    def _calibrate(self, cl_price: float, cl_ts_ms: int):
        delta = self.spot.primary.calibrate(cl_price, cl_ts_ms)
        if delta is not None:
            log.debug("calibrate: chainlink %.2f vs ex → offset %.2f (n=%d)",
                      cl_price, self.spot.primary.offset,
                      self.spot.primary.offset_samples)

    def _clock_sync(self, books: dict):
        if not self.cfg.feeds.clock_sync:
            return
        for b in books.values():
            if b and b.server_ts:
                off = b.server_ts - b.ts
                if abs(off) < 30:
                    self.cfg.clock_offset_s = 0.8 * self.cfg.clock_offset_s + 0.2 * off
                break

    def _update_marks(self, mkt: Market, books: dict):
        for side, b in books.items():
            if b and b.mid:
                self.broker.marks[(mkt.slug, side)] = b.mid

    def _snapshot_equity(self):
        eq = self.broker.equity()
        self.store.snapshot_equity(eq, self.broker.balance, eq - self.broker.balance)

    def _update_status(self, mkt: Market, t_left: float, live, lead,
                       books: dict, fav, skip_reason: str):
        fav_book: Optional[Book] = books.get(fav) if fav else None
        st = {
            "slug": mkt.slug, "question": mkt.question, "t_left": t_left,
            "win_start": self.cfg.strategy.window_start_s,
            "win_end": self.cfg.strategy.window_end_s,
            "ptb": mkt.ptb, "ptb_source": mkt.ptb_source, "live": live or 0,
            "lead_bps": lead if lead is not None else 0,
            "stable_for": self.strategy.stable_for(self.now()),
            "fav_side": fav or "?",
            "fav_ask": fav_book.best_ask if fav_book else None,
            "fav_eff": effective_price(fav_book.best_ask, mkt.fee_rate)
            if fav_book and fav_book.best_ask else 0,
            "spread": fav_book.spread_ticks(self.cfg.market.tick_size)
            if fav_book else None,
            "depth": fav_book.ask_depth(self.cfg.strategy.max_ask) if fav_book else 0,
            "book_age": (time.time() - fav_book.ts) if fav_book else None,
            "ws_connected": self.clob_ws.connected,
            "cl_offset": self.spot.primary.offset,
            "offset_samples": self.spot.primary.offset_samples,
            "clock_off": self.cfg.clock_offset_s,
            "spot_source": self.spot.primary.last.source if self.spot.primary.last else "-",
            "spot_age": self.spot.primary.age_s,
            "vol": self.spot.primary.vol_bps_sqrt_s(
                self.cfg.strategy.vol_default_bps_sqrt_s),
            "divergence": self.spot.divergence_bps,
            "paused": self.paused,
            "open_positions": [{"side": p.side, "shares": p.shares,
                                "avg_price": p.avg_price, "market_slug": p.market_slug}
                               for p in self.broker.open_positions],
            "balance": self.broker.balance, "equity": self.broker.equity(),
            "day_pnl": self.broker._day_realized,
            "closed": self.broker.closed_count,
            "loss_streak": self.broker.loss_streak,
            "pause_rounds": self.broker.pause_rounds_left,
            "skip_reason": skip_reason,
        }
        self.status_extra = st

    def status_snapshot(self) -> dict:
        base = {
            "slug": None, "question": "(menunggu round)", "t_left": 0,
            "win_start": self.cfg.strategy.window_start_s,
            "win_end": self.cfg.strategy.window_end_s,
            "ptb": None, "ptb_source": "-", "live": 0, "lead_bps": 0,
            "stable_for": 0, "fav_side": "?", "fav_ask": None, "fav_eff": 0,
            "spread": None, "depth": 0,
            "book_age": None,
            "ws_connected": self.clob_ws.connected,
            "cl_offset": self.spot.primary.offset,
            "offset_samples": self.spot.primary.offset_samples,
            "clock_off": self.cfg.clock_offset_s,
            "spot_source": self.spot.primary.last.source if self.spot.primary.last else "-",
            "spot_age": self.spot.primary.age_s,
            "vol": self.spot.primary.vol_bps_sqrt_s(
                self.cfg.strategy.vol_default_bps_sqrt_s),
            "divergence": self.spot.divergence_bps,
            "paused": self.paused,
            "open_positions": [{"side": p.side, "shares": p.shares,
                                "avg_price": p.avg_price, "market_slug": p.market_slug}
                               for p in self.broker.open_positions],
            "balance": self.broker.balance, "equity": self.broker.equity(),
            "day_pnl": self.broker._day_realized,
            "closed": self.broker.closed_count,
            "loss_streak": self.broker.loss_streak,
            "pause_rounds": self.broker.pause_rounds_left,
        }
        base.update(self.status_extra)
        return base

    def books_snapshot(self) -> dict:
        """Kedalaman book saat ini untuk perintah /book."""
        out = {"connected": self.clob_ws.connected, "sides": {}}
        mkt = self.current
        if not mkt:
            return out
        for side in ("Up", "Down"):
            b = self.clob_ws.get_book(mkt.token(side), max_age=3600) \
                or self.clob.get_cached_book(mkt.token(side))
            if not b:
                continue
            out["sides"][side] = {
                "best_bid": b.best_bid, "best_ask": b.best_ask, "mid": b.mid,
                "age": round(time.time() - b.ts, 1),
                "asks": [(l.price, l.size) for l in b.asks[:5]],
                "bids": [(l.price, l.size) for l in b.bids[:5]],
                "last_trade": self.clob_ws.last_trade.get(mkt.token(side)),
            }
        return out
