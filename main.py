"""Polymarket BTC 5m Up/Down — PAPER trading bot.

Run:
    python main.py                 # live paper trading + telegram (jika ada token)
    python main.py --rounds 6      # berhenti setelah 6 round (uji coba)
    python main.py --no-telegram   # headless
    python main.py --report        # tulis laporan harian lalu keluar

SEMUA TRADE ADALAH PAPER — tidak ada order sungguhan ke Polymarket.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import sys
from pathlib import Path

from updown.config import load
from updown.engine import Engine
from updown.telegram_bot import TelegramNotifier


def setup_logging(cfg, verbose: bool = False):
    # Windows console default cp1252 — emoji/unicode di log akan crash
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    level = logging.DEBUG if verbose else getattr(
        logging, cfg.secrets.log_level.upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-16s %(message)s",
                            datefmt="%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = logging.handlers.RotatingFileHandler(
        cfg.data_dir / "logs" / "bot.log", maxBytes=5_000_000, backupCount=3,
        encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s %(message)s"))
    root.addHandler(fh)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


async def amain(args) -> int:
    cfg = load(args.config)
    if args.no_telegram:
        cfg.telegram.enabled = "false"
    setup_logging(cfg, args.verbose)

    engine = Engine(cfg)
    if args.report:
        path = engine.write_daily_report()
        print(f"report ditulis: {path}")
        engine.store.close()
        return 0

    notifier = TelegramNotifier(cfg, engine=engine, store=engine.store,
                                broker=engine.broker)
    engine.notifier = notifier
    await notifier.start()
    try:
        await engine.run(rounds_limit=args.rounds)
    finally:
        await notifier.stop()
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None, help="path config.yaml")
    ap.add_argument("--rounds", type=int, default=None,
                    help="berhenti setelah N round (default: terus)")
    ap.add_argument("--no-telegram", action="store_true",
                    help="headless (notifikasi ke log)")
    ap.add_argument("--report", action="store_true",
                    help="tulis laporan harian markdown lalu keluar")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = ap.parse_args()
    try:
        sys.exit(asyncio.run(amain(args)))
    except KeyboardInterrupt:
        print("\nbye.")


if __name__ == "__main__":
    main()
