"""Backtest strategi late-window pada data riil Polymarket BTC 5m Up/Down.

Sumber data per round (semua publik):
  • Gamma events          — slug, token ids, outcome final (closed)
  • crypto-price          — openPrice (PTB) & closePrice final Chainlink
  • price-history         — path harga Chainlink di dalam window (lead saat entry)
  • CLOB prices-history   — harga token kedua sisi sekitar waktu entry (proksi ask)

Catatan / keterbatasan (baca README):
  • CLOB prices-history fidelity ~1 menit → harga entry adalah PROKSI
    (mid-ish, bukan ask sebenarnya). Slippage ditambahkan via --slippage-ticks.
  • Stabilitas lead (lead_stable_s) tidak bisa diverifikasi dari data historis;
    backtest bisa sedikit lebih optimis dibanding live.
  • API situs polymarket.com rate-limited → ±13 detik/round baru. Hasil di-cache
    di data/backtest_cache.json supaya re-run instan.

Contoh:
  python backtest.py --rounds 40
  python backtest.py --rounds 100 --min-lead 8 --max-eff 0.88 --stake 50
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import io
import json
import logging
import re
import sys
import time
from pathlib import Path

from updown.chainlink import ChainlinkFeed, hist_point_at
from updown.config import load
from updown.polymarket import ApiError, ClobClient, GammaClient, Http
from updown.stats import compute_stats, format_stats_md
from updown.strategy import effective_price, fee_per_share

log = logging.getLogger("backtest")

ROOT = Path(__file__).resolve().parent
SITE_HISTORY = "https://polymarket.com/api/crypto/price-history"
REPORT_DIRNAME = "reports"


class Cache:
    """JSON file cache keyed by round slug (fixed location under data/)."""

    def __init__(self, path: Path, use: bool = True):
        self.path = path
        self.use = use
        self.data = {}
        if use and path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                self.data = {}

    def get(self, key):
        return self.data.get(key) if self.use else None

    def put(self, key, val):
        if self.use:
            self.data[key] = val
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data), encoding="utf-8")


async def fetch_round(gamma: GammaClient, clob: ClobClient, cl: ChainlinkFeed,
                      http: Http, start: int, symbol: str, cache: Cache):
    """Gather all data for one historical round; returns dict or None."""
    slug = gamma.slug_for(start)
    key = f"round:{slug}"
    hit = cache.get(key)
    if hit is not None:
        return hit or None

    out = {"slug": slug, "start": start}
    mkt = None
    with contextlib.suppress(Exception):
        mkt = await gamma.market_for_start(start, symbol)
    if not mkt:
        cache.put(key, {})
        return None
    winner = None
    with contextlib.suppress(Exception):
        winner = await gamma.resolved_outcome(slug)
    if not winner:
        cache.put(key, {})
        return None
    out["winner"] = winner

    sample = await cl.fetch(start, force=True)
    if not (sample and sample.open_price and sample.close_price):
        cache.put(key, {})
        return None
    out["ptb"] = sample.open_price
    out["close"] = sample.close_price
    out["cl_winner"] = "Up" if sample.close_price >= sample.open_price else "Down"

    # chainlink path within the window (site API, throttled by Http)
    path = None
    with contextlib.suppress(Exception):
        data = await http.get_json(
            SITE_HISTORY, params={"symbol": symbol, "eventStartTime": start}, tries=2)
        if isinstance(data, list):
            path = data
    out["path"] = path

    # PTB cross-validated — sama dengan bot live: openPrice bisa basi (beku),
    # maka titik pertama price-history (= round open) jadi referensi yang benar.
    if isinstance(path, list):
        hist_open = hist_point_at(path, start)
        if hist_open:
            if (sample.open_price and
                    abs(sample.open_price - hist_open) / hist_open * 1e4 <= 5.0):
                out["ptb"] = sample.open_price
                out["ptb_source"] = "chainlink"
            else:
                out["ptb"] = hist_open
                out["ptb_source"] = "chainlink_hist"
        else:
            out["ptb_source"] = "chainlink"
    else:
        out["ptb_source"] = "chainlink"

    # token price history (CLOB, lenient) for entry-ask proxy — BOTH sides,
    # the side is chosen later from the lead (no lookahead).
    hist = {}
    for side, tok in (("Up", mkt.token_up), ("Down", mkt.token_down)):
        with contextlib.suppress(Exception):
            hist[side] = await clob.prices_history(tok, start - 5, start + 320, 1)
    out["hist"] = hist
    cache.put(key, out)
    return out


def pick_path_point(path, target_ts: float, lo: float, hi: float):
    """Closest chainlink point to target_ts within [lo, hi]."""
    if not path:
        return None
    best = None
    for pt in path:
        ts = pt["timestamp"] / 1000.0
        if lo <= ts <= hi:
            d = abs(ts - target_ts)
            if best is None or d < best[0]:
                best = (d, pt["value"])
    return best[1] if best else None


def pick_hist_point(hist, target_ts: float, lo: float, hi: float):
    if not hist:
        return None
    best = None
    for pt in hist:
        ts = pt.get("t", 0)
        if lo <= ts <= hi:
            d = abs(ts - target_ts)
            if best is None or d < best[0]:
                best = (d, pt.get("p"))
    return best[1] if best else None


def simulate(r: dict, a) -> dict | None:
    """Apply the strategy to one round's data. Returns trade dict or None."""
    start, end = r["start"], r["start"] + 300
    entry_ts = end - a.entry_offset
    lo, hi = end - a.window_start, end - a.window_end
    px = pick_path_point(r.get("path"), entry_ts, lo, hi)
    if px is None or not r.get("ptb"):
        return None
    lead = (px - r["ptb"]) / r["ptb"] * 1e4
    if abs(lead) < a.min_lead:
        return {"skipped": "lead", "lead": lead}
    side = "Up" if lead > 0 else "Down"
    hist = r.get("hist") or {}
    p_up = pick_hist_point(hist.get("Up"), entry_ts, lo - 30, hi + 30)
    p_dn = pick_hist_point(hist.get("Down"), entry_ts, lo - 30, hi + 30)
    if p_up is None or p_dn is None:
        return {"skipped": "no_token_hist", "lead": lead}
    # side_agree (sama seperti live): arah lead harus = favorit pasar
    # (favorit = token dengan harga >= 0.5). Kalau beda, skip — jangan beli underdog.
    market_fav = "Up" if p_up >= p_dn else "Down"
    if side != market_fav:
        return {"skipped": "side_agree", "lead": lead}
    p_tok = p_up if side == "Up" else p_dn
    tick = 0.01
    ask = min(0.99, round(p_tok + tick / 2 + a.slippage_ticks * tick, 3))
    if ask > a.max_ask:
        return {"skipped": "ask_cap", "lead": lead, "ask": ask}
    eff = effective_price(ask, a.fee_rate)
    if eff > a.max_eff:
        return {"skipped": "eff_cap", "lead": lead, "ask": ask, "eff": eff}
    fee_sh = fee_per_share(ask, a.fee_rate)
    shares = round(a.stake / (ask + fee_sh), 2)
    if shares < 5:
        return {"skipped": "min_order", "lead": lead}
    gross = shares * ask
    fee = shares * fee_sh
    won = side == r["winner"]
    payout = shares if won else 0.0
    pnl = payout - gross - fee
    return {
        "skipped": None, "slug": r["slug"], "start": start, "side": side,
        "winner": r["winner"], "lead": round(lead, 2), "ask": ask,
        "eff": round(eff, 4), "shares": shares, "stake": round(gross, 2),
        "fee": round(fee, 4), "payout": round(payout, 2), "pnl": round(pnl, 4),
        "status": "WON" if won else "LOST",
        "ptb": r["ptb"], "close": r["close"], "entry_px": px,
    }


def _reports_dir() -> Path:
    d = (ROOT / "data" / REPORT_DIRNAME).resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _csv_to_text(trades: list) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(trades[0]))
    writer.writeheader()
    writer.writerows(trades)
    return buf.getvalue()


async def run_backtest(a):
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    cfg = load(a.config)
    http = Http()
    gamma = GammaClient(http, cfg.market.series_slug)
    clob = ClobClient(http)
    cl = ChainlinkFeed(http, cfg.market.symbol, poll_sec=6.5)
    cache = Cache(ROOT / "data" / "backtest_cache.json", not a.no_cache)

    now = time.time()
    start0 = int(now // 300) * 300 - 300 - a.ago_hours * 3600
    trades, skipped, missing, attempts = [], {}, 0, 0
    cur = start0
    max_attempts = a.rounds * 6
    while len(trades) < a.rounds and attempts < max_attempts:
        attempts += 1
        cur -= 300  # mundur ke round sebelumnya
        key = f'round:{gamma.slug_for(cur)}'
        was_cached = cache.get(key) is not None
        if a.verbose:
            log.info("fetch %s (%d/%d trades)%s", gamma.slug_for(cur),
                     len(trades), a.rounds, " [cache]" if was_cached else "")
        try:
            r = await fetch_round(gamma, clob, cl, http, cur, cfg.market.symbol, cache)
        except ApiError as exc:
            log.warning("api error: %s", exc)
            r = None
        if not r:
            missing += 1
        else:
            res = simulate(r, a)
            if res is None:
                missing += 1
            elif res.get("skipped"):
                skipped[res["skipped"]] = skipped.get(res["skipped"], 0) + 1
            else:
                trades.append(res)
                log.info("#%02d %s %s lead%+.1f ask%.3f → %s pnl %+.2f",
                         len(trades), res["slug"][-9:], res["side"], res["lead"],
                         res["ask"], res["status"], res["pnl"])
        if not was_cached:
            await asyncio.sleep(1.0)   # be nice to the APIs
    await http.close()

    # ── report ──
    rows = [{
        "market_slug": t["slug"], "side": t["side"], "shares": t["shares"],
        "avg_price": t["ask"], "eff_price": t["eff"], "stake_usdc": t["stake"],
        "fees": t["fee"], "slippage_cost": 0.0, "status": t["status"],
        "pnl_net": t["pnl"], "payout": t["payout"], "t_left_s": a.entry_offset,
        "lead_bps": t["lead"], "exit_ts": t["start"] + 300,
        "created_ts": t["start"],
    } for t in trades]
    st = compute_stats(rows, start_balance=a.start_balance)
    print("\n" + "=" * 62)
    print(f"BACKTEST {attempts} round dicoba | {len(trades)} trade | "
          f"{missing} round tanpa data | skip: {skipped}")
    print("=" * 62)
    print(format_stats_md(st, f"Hasil (stake ${a.stake}/trade, "
                              f"lead≥{a.min_lead}bps, eff≤{a.max_eff}, "
                              f"entry T-{a.entry_offset}s)"))
    bal = a.start_balance + st["net_pnl"]
    print(f"Saldo akhir: {bal:,.2f} USDC "
          f"({(bal / a.start_balance - 1) * 100:+.2f}%)")

    if trades:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if not re.fullmatch(r"\d{8}-\d{6}", stamp):
            raise ValueError("unexpected timestamp format")
        outdir = _reports_dir()
        csv_path = outdir / f"backtest-{stamp}.csv"
        csv_path.write_text(_csv_to_text(trades), encoding="utf-8")
        print(f"CSV: {csv_path}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rounds", type=int, default=30, help="jumlah trade disimulasikan")
    ap.add_argument("--ago-hours", type=int, default=0, help="mundur N jam dari sekarang")
    ap.add_argument("--entry-offset", type=int, default=60,
                    help="detik sebelum end saat entry dievaluasi (default 60)")
    ap.add_argument("--window-start", type=int, default=90)
    ap.add_argument("--window-end", type=int, default=30)
    ap.add_argument("--min-lead", type=float, default=7.0)
    ap.add_argument("--max-eff", type=float, default=0.90)
    ap.add_argument("--max-ask", type=float, default=0.90)
    ap.add_argument("--fee-rate", type=float, default=0.07)
    ap.add_argument("--slippage-ticks", type=int, default=1)
    ap.add_argument("--stake", type=float, default=25.0)
    ap.add_argument("--start-balance", type=float, default=1000.0)
    ap.add_argument("--config", default=None)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    sys.exit(asyncio.run(run_backtest(args)))


if __name__ == "__main__":
    main()
