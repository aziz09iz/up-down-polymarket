"""Telegram bot: notifications (HTML) + interactive commands.

Headless mode: tanpa TELEGRAM_BOT_TOKEN bot tetap jalan; semua notifikasi
dicetak ke log dan perintah tentu saja tidak tersedia.
"""
from __future__ import annotations

import contextlib
import logging
import re
import time
from typing import Optional

from . import format as F
from .config import (Config, _FLAT_KEYS, runtime_overrides, save_runtime,
                     unset_runtime)
from .stats import compute_stats

log = logging.getLogger("updown.telegram")

try:
    from telegram import BotCommand, Update
    from telegram.constants import ParseMode
    from telegram.ext import (Application, CommandHandler, ContextTypes,
                              MessageHandler, filters)
    PTB_OK = True
except ImportError:  # pragma: no cover
    PTB_OK = False


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


class TelegramNotifier:
    def __init__(self, cfg: Config, engine=None, store=None, broker=None):
        self.cfg = cfg
        self.engine = engine
        self.store = store
        self.broker = broker
        self.chat_ids = set(str(c) for c in cfg.secrets.telegram_chat_ids)
        self.app: Optional["Application"] = None
        self.headless = True
        self._started = False

    # ── lifecycle ──
    def _should_enable(self) -> bool:
        mode = str(self.cfg.telegram.enabled).lower()
        if mode == "false":
            return False
        if mode == "true":
            return bool(self.cfg.secrets.telegram_bot_token)
        return bool(self.cfg.secrets.telegram_bot_token)  # auto

    async def start(self):
        if not self._should_enable():
            log.info("Telegram headless (no token / disabled) — notifikasi ke log saja")
            return
        if not PTB_OK:
            log.error("python-telegram-bot belum terinstall — mode headless")
            return
        self.app = Application.builder().token(self.cfg.secrets.telegram_bot_token).build()
        self._register_handlers()
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        me = await self.app.bot.get_me()
        await self._set_menu()
        self.headless = False
        self._started = True
        log.info("Telegram aktif: @%s (chat_ids=%s)", me.username,
                 self.chat_ids or "auto-learn")
        await self.send(f"<b>Bot aktif</b> · PAPER\n"
                        f"Polymarket BTC 5m Up/Down\n"
                        f"Saldo awal {F.usd(self.cfg.paper.start_balance)} USDC\n"
                        f"/help — daftar perintah")

    async def stop(self):
        if self._started and self.app:
            with contextlib.suppress(Exception):
                await self.app.updater.stop()
            with contextlib.suppress(Exception):
                await self.app.stop()
            with contextlib.suppress(Exception):
                await self.app.cleanup()

    # ── sending ──
    async def send(self, html_text: str):
        if self.headless or not self.app:
            log.info("[TG] %s", _strip_tags(html_text))
            return
        if not self.chat_ids:
            log.debug("[TG drop, no chat yet] %s", _strip_tags(html_text))
            return
        dead = []
        for chat in list(self.chat_ids):
            try:
                await self.app.bot.send_message(
                    chat_id=chat, text=html_text[:4000],
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("TG send ke %s gagal: %s", chat, exc)
                if "bot was blocked" in str(exc) or "chat not found" in str(exc):
                    dead.append(chat)
        for chat in dead:
            self.chat_ids.discard(chat)

    # ── auth ──
    async def _set_menu(self):
        cmds = [
            BotCommand("status", "Round & feed live"),
            BotCommand("book", "Orderbook (WS)"),
            BotCommand("pnl", "PnL hari ini & total"),
            BotCommand("stats", "Statistik lengkap"),
            BotCommand("trades", "10 trade terakhir"),
            BotCommand("positions", "Posisi terbuka"),
            BotCommand("rounds", "Hasil round terakhir"),
            BotCommand("equity", "Saldo & equity"),
            BotCommand("pause", "Hentikan entry baru"),
            BotCommand("resume", "Lanjutkan entry"),
            BotCommand("set", "Ubah parameter runtime"),
            BotCommand("unset", "Reset parameter"),
            BotCommand("config", "Config efektif"),
            BotCommand("report", "Laporan harian"),
            BotCommand("help", "Daftar perintah"),
        ]
        with contextlib.suppress(Exception):
            await self.app.bot.set_my_commands(cmds)

    def _authorized(self, update: "Update") -> bool:
        chat = str(update.effective_chat.id)
        if chat in self.chat_ids:
            return True
        if not self.chat_ids:
            # belajar dari chat pertama (hanya jika TELEGRAM_CHAT_IDS kosong)
            self.chat_ids.add(chat)
            log.info("chat %s didaftarkan otomatis (TELEGRAM_CHAT_IDS kosong)", chat)
            return True
        log.warning("chat tak dikenal %s ditolak", chat)
        return False

    # ── handlers ──
    def _register_handlers(self):
        app = self.app
        cmds = {
            "start": self.cmd_start, "help": self.cmd_help, "status": self.cmd_status,
            "book": self.cmd_book, "pnl": self.cmd_pnl, "stats": self.cmd_stats,
            "trades": self.cmd_trades, "positions": self.cmd_positions,
            "rounds": self.cmd_rounds, "pause": self.cmd_pause, "resume": self.cmd_resume,
            "set": self.cmd_set, "unset": self.cmd_unset, "config": self.cmd_config,
            "report": self.cmd_report, "equity": self.cmd_equity,
        }
        for name, fn in cmds.items():
            app.add_handler(CommandHandler(name, fn))
        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND, self.cmd_unknown))
        app.add_error_handler(self.on_error)

    async def _reply(self, update: "Update", text: str):
        await update.effective_message.reply_text(
            text[:4000], parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    async def cmd_unknown(self, update: "Update", ctx: "ContextTypes.DEFAULT_TYPE"):
        if not self._authorized(update):
            return
        await self._reply(update, "Perintah tidak dikenal. Ketik /help")

    async def on_error(self, update, ctx):
        log.error("TG handler error: %s", ctx.error)

    async def cmd_start(self, update: "Update", ctx):
        if not self._authorized(update):
            return
        s = self.cfg.strategy
        await self._reply(update,
                          "<b>Polymarket BTC 5m Up/Down — Paper Bot</b>\n\n"
                          f"Strategi: entry saat sisa waktu {s.window_end_s}–"
                          f"{s.window_start_s}s, lead ≥ {s.min_lead_bps} bps, "
                          f"sisi unggul, harga efektif ≤ {s.max_eff_price}.\n"
                          "Mode PAPER — tidak ada order sungguhan.\n\n"
                          "/help — daftar perintah")

    async def cmd_help(self, update: "Update", ctx):
        if not self._authorized(update):
            return
        await self._reply(update, (
            "<b>Perintah</b>\n\n"
            "<i>Pantau</i>\n"
            "/status — round &amp; feed live\n"
            "/book — orderbook (WS)\n"
            "/positions — posisi terbuka\n"
            "/rounds — hasil round terakhir\n"
            "/trades — 10 trade terakhir\n\n"
            "<i>Performa</i>\n"
            "/pnl — hari ini &amp; total\n"
            "/stats — statistik lengkap\n"
            "/equity — saldo &amp; equity\n"
            "/report — laporan harian (file)\n\n"
            "<i>Kontrol</i>\n"
            "/pause · /resume — stop/lanjut entry\n"
            "/set &lt;key&gt; &lt;value&gt; — ubah parameter runtime\n"
            "/unset &lt;key&gt; — reset ke default\n"
            "/config — config efektif + daftar key"))

    async def cmd_status(self, update: "Update", ctx):
        if not self._authorized(update):
            return
        if not self.engine:
            return await self._reply(update, "Engine belum aktif.")
        await self._reply(update, F.status_html(self.engine.status_snapshot()))

    async def cmd_book(self, update: "Update", ctx):
        if not self._authorized(update):
            return
        if not self.engine:
            return await self._reply(update, "Engine belum aktif.")
        await self._reply(update, F.book_html(self.engine.books_snapshot()))

    def _day_rows(self):
        day0 = time.time() - (time.time() % 86400)
        return self.store.positions_since(day0) if self.store else []

    async def cmd_pnl(self, update: "Update", ctx):
        if not self._authorized(update):
            return
        day = compute_stats(self._day_rows(), start_balance=self.cfg.paper.start_balance)
        allr = self.store.all_closed() if self.store else []
        total = compute_stats(allr, self.store.equity_series() if self.store else None,
                              self.cfg.paper.start_balance)
        body = F.kv([
            ("Hari ini", f"{F.usd(day['net_pnl'], sign=True)} USDC"
                         f" ({day['n']} trades · WR {day['win_rate']}%)"),
            ("Total", f"{F.usd(total['net_pnl'], sign=True)} USDC"
                      f" ({total['n']} trades · WR {total['win_rate']}%)"),
            ("Balance", F.usd(self.broker.balance)),
            ("Equity", F.usd(self.broker.equity())),
        ])
        await self._reply(update, f"<b>PnL</b>\n{F.code_block(body)}")

    async def cmd_stats(self, update: "Update", ctx):
        if not self._authorized(update):
            return
        allr = self.store.all_closed() if self.store else []
        st = compute_stats(allr, self.store.equity_series() if self.store else None,
                           self.cfg.paper.start_balance)
        await self._reply(update, F.stats_html(st, "Statistik keseluruhan"))

    async def cmd_trades(self, update: "Update", ctx):
        if not self._authorized(update):
            return
        rows = self.store.recent_positions(10) if self.store else []
        await self._reply(update, F.trades_html(rows))

    async def cmd_positions(self, update: "Update", ctx):
        if not self._authorized(update):
            return
        if not self.broker.open_positions:
            return await self._reply(update, "Tidak ada posisi terbuka.")
        lines = ["<b>Posisi terbuka</b>"]
        for p in self.broker.open_positions:
            mark = self.broker.marks.get((p.market_slug, p.side))
            mv = f" · mark {mark:.3f}" if mark else ""
            body = F.kv([
                ("Size", f"{p.shares:.1f} sh @ {p.avg_price:.3f}"
                         f" (eff {p.eff_price:.4f}){mv}"),
                ("Stake", f"${F.usd(p.stake_usdc)} · fee ${F.usd(p.fees)}"),
                ("Round", F.esc(p.market_slug)),
            ])
            lines.append(f"{F.arrow(p.side)} <b>{p.side}</b>\n{F.code_block(body)}")
        await self._reply(update, "\n".join(lines))

    async def cmd_rounds(self, update: "Update", ctx):
        if not self._authorized(update):
            return
        rows = self.store.recent_rounds(12) if self.store else []
        await self._reply(update, F.rounds_html(rows))

    async def cmd_pause(self, update: "Update", ctx):
        if not self._authorized(update):
            return
        if self.engine:
            self.engine.set_paused(True)
        await self._reply(update, "<b>PAUSED</b> — entry baru dihentikan"
                                  " (observasi &amp; resolusi tetap jalan).")

    async def cmd_resume(self, update: "Update", ctx):
        if not self._authorized(update):
            return
        if self.engine:
            self.engine.set_paused(False)
        await self._reply(update, "<b>RESUMED</b> — bot mencari entry lagi.")

    async def cmd_set(self, update: "Update", ctx):
        if not self._authorized(update):
            return
        args = ctx.args or []
        if len(args) != 2:
            return await self._reply(update,
                                     "Pakai: <code>/set &lt;key&gt; &lt;value&gt;</code>\n"
                                     "Contoh: <code>/set min_lead_bps 6.5</code>")
        key, val = args[0], args[1]
        ok = save_runtime(self.cfg, key, val)
        if ok:
            await self._reply(update, f"✅ <b>{F.esc(key)} = {F.esc(val)}</b>"
                                      " — disimpan ke runtime.json")
        else:
            await self._reply(update, f"Key <code>{F.esc(key)}</code> tidak dikenal"
                                      " — /config untuk daftar key.")

    async def cmd_unset(self, update: "Update", ctx):
        if not self._authorized(update):
            return
        args = ctx.args or []
        if len(args) != 1:
            return await self._reply(update, "Pakai: <code>/unset &lt;key&gt;</code>")
        key = args[0]
        ok = unset_runtime(self.cfg, key)
        if ok:
            await self._reply(update, f"♻ <b>{F.esc(key)}</b> direset ke default.")
        else:
            await self._reply(update, f"Key <code>{F.esc(key)}</code> tidak dikenal"
                                      " / tidak ada override.")

    async def cmd_config(self, update: "Update", ctx):
        if not self._authorized(update):
            return
        settables = [k.lower() for k in _FLAT_KEYS
                     if not k.startswith("TELEGRAM_") and k not in ("LOG_LEVEL",)]
        await self._reply(update, F.config_html(self.cfg, runtime_overrides(self.cfg),
                                                settables))

    async def cmd_report(self, update: "Update", ctx):
        if not self._authorized(update):
            return
        if self.engine:
            path = self.engine.write_daily_report()
            await self._reply(update, f"Laporan harian: <code>{F.esc(path)}</code>")
            with contextlib.suppress(Exception):
                await update.effective_message.reply_document(open(path, "rb"))
        else:
            await self._reply(update, "Engine belum aktif.")

    async def cmd_equity(self, update: "Update", ctx):
        if not self._authorized(update):
            return
        b = self.broker
        body = F.kv([
            ("Balance", F.usd(b.balance)),
            ("Equity", f"{F.usd(b.equity())} (open {F.usd(b.equity() - b.balance)})"),
            ("Trades", f"{b.closed_count} closed · streak {b.loss_streak}L"),
        ])
        await self._reply(update, f"<b>Equity</b>\n{F.code_block(body)}")
