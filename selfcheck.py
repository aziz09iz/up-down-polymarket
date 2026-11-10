"""Self-check offline: fee math, fill simulation, strategy rules, broker, store.

Jalankan:  python selfcheck.py
Tidak menyentuh jaringan.
"""
from __future__ import annotations

import math
import random
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from updown.chainlink import choose_ptb, hist_point_at
from updown.clobws import apply_price_changes, book_from_event
from updown.netguard import UnsafeUrlError, validate_url_sync
from updown import config as config_mod
from updown.config import Config, load, save_runtime
from updown.format import entry_html, status_html
from updown.models import Book, Level, Market, Position
from updown.paper import PaperBroker, simulate_sell, simulate_taker_order
from updown.stats import compute_stats
from updown.store import Store
from updown.strategy import (LateWindowStrategy, effective_price,
                             fee_per_share, norm_cdf)

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def approx(a, b, eps=1e-6):
    return abs(a - b) <= eps


def test_fees():
    print("[1] Fee formula vs tabel resmi docs.polymarket.com (Crypto, 100 shares)")
    table = {0.01: 0.07, 0.05: 0.33, 0.10: 0.63, 0.20: 1.12, 0.30: 1.47,
             0.40: 1.68, 0.50: 1.75, 0.60: 1.68, 0.70: 1.47, 0.80: 1.12,
             0.85: 0.89, 0.90: 0.63, 0.95: 0.33, 0.99: 0.07}
    for p, want in table.items():
        got = fee_per_share(p, 0.07) * 100
        check(f"p={p:.2f} fee=${want:.2f}", abs(got - want) < 0.005,
              f"got {got:.4f}")
    check("eff(0.87) < 0.88", effective_price(0.87, 0.07) < 0.88,
          f"{effective_price(0.87, 0.07):.4f}")
    check("norm_cdf(0) = 0.5", approx(norm_cdf(0.0), 0.5))
    check("norm_cdf(1.96) ≈ 0.975", abs(norm_cdf(1.96) - 0.975) < 1e-3)


def mkcfg(**over) -> Config:
    cfg = Config()
    for k, v in over.items():
        sec, _, fld = k.partition("__")
        setattr(getattr(cfg, sec), fld, v)
    return cfg


def test_fill_sim():
    print("[2] Simulasi fill (book-walk, participation cap, adverse move, fee)")
    cfg = mkcfg()
    asks = [Level(0.87, 100), Level(0.88, 5000), Level(0.89, 5000)]
    rng = random.Random("t1")
    plan = simulate_taker_order(asks, 25.0, 0.07, cfg, rng, is_buy=True)
    check("stake terpakai ≤ 25", plan.total_cost <= 25.0 + 1e-6, f"{plan.total_cost}")
    check("stake terpakai ≈ 25", plan.total_cost > 24.5, f"{plan.total_cost}")
    check("fee > 0", plan.fees > 0, f"{plan.fees}")
    check("avg price dalam [0.87, 0.90]", 0.87 - 1e-9 <= plan.avg_price <= 0.90,
          f"{plan.avg_price}")
    # participation cap: level 0.87 hanya 100 sh → maks 15 sh (15%)
    first = plan.fills[0]
    check("participation cap 15% level-1", first.shares <= 15.0 + 1e-9,
          f"{first.shares}")
    # fee per level konsisten formula
    for f in plan.fills:
        want = f.shares * 0.07 * f.price * (1 - f.price)
        check(f"fee level {f.price:.2f}", abs(f.fee - want) < 1e-9,
              f"{f.fee} vs {want}")
    # min order reject
    rng2 = random.Random("t2")
    tiny = simulate_taker_order([Level(0.87, 10)], 0.5, 0.07, cfg, rng2)
    check("order terlalu kecil ditolak", bool(tiny.rejected), tiny.rejected)
    # sell sim
    rng3 = random.Random("t3")
    bids = [Level(0.86, 5000), Level(0.85, 5000)]
    sp = simulate_sell(bids, 30.0, 0.07, cfg, rng3)
    check("sell fills semua shares", approx(sp.shares, 30.0, 0.01), f"{sp.shares}")
    check("sell proceeds > 0", sp.cash_spent > 0)


def test_strategy():
    print("[3] Aturan strategi late-window (spec 90–30s, lead≥7bps, eff≤0.90)")
    cfg = mkcfg()
    st = LateWindowStrategy(cfg)
    now = time.time()
    ptb, live = 100000.0, 100080.0        # lead +8 bps
    book_up = Book("t", now, bids=[Level(0.86, 5000)],
                   asks=[Level(0.87, 3000), Level(0.88, 2000)])
    book_dn = Book("t2", now, bids=[Level(0.12, 5000)], asks=[Level(0.13, 3000)])

    # belum stabil
    st.observe(8.0, now)
    d = st.evaluate(t_left=60, ptb=ptb, live_price=live, spot_age=0.2,
                    divergence_bps=None, now=now,
                    books={"Up": book_up, "Down": book_dn}, vol_bps_sqrt_s=0.75)
    check("stabil < 4s → tidak entry", not d.ok and "stable" in d.reasons,
          str(d.failed_rules()))
    # stabil 5 detik (reset dulu agar observe mencatat waktu baru)
    st.observe(0.0, now - 6)
    st.observe(8.0, now - 5)
    d = st.evaluate(t_left=60, ptb=ptb, live_price=live, spot_age=0.2,
                    divergence_bps=None, now=now,
                    books={"Up": book_up, "Down": book_dn}, vol_bps_sqrt_s=0.75)
    check("semua aturan ok → ENTER Up", d.ok and d.side == "Up",
          str(d.failed_rules()))
    # luar window (t_left=120) → wait
    d2 = st.evaluate(t_left=120, ptb=ptb, live_price=live, spot_age=0.2,
                     divergence_bps=None, now=now,
                     books={"Up": book_up, "Down": book_dn}, vol_bps_sqrt_s=0.75)
    check("t_left 120s → wait", d2.action == "wait", d2.action)
    # t_left 20 (< 30) → wait (jangan sniping detik akhir)
    d3 = st.evaluate(t_left=20, ptb=ptb, live_price=live, spot_age=0.2,
                     divergence_bps=None, now=now,
                     books={"Up": book_up, "Down": book_dn}, vol_bps_sqrt_s=0.75)
    check("t_left 20s → wait", d3.action == "wait", d3.action)
    # lead kurang
    st.observe(4.0, now - 10)
    d4 = st.evaluate(t_left=60, ptb=ptb, live_price=100040.0, spot_age=0.2,
                     divergence_bps=None, now=now,
                     books={"Up": book_up, "Down": book_dn}, vol_bps_sqrt_s=0.75)
    check("lead 4bps → skip", not d4.ok, str(d4.failed_rules()))
    # harga kemahalan: ask 0.93 → eff > 0.90
    st.observe(8.0, now - 10)
    rich = Book("t", now, bids=[Level(0.92, 5000)], asks=[Level(0.93, 3000)])
    d5 = st.evaluate(t_left=60, ptb=ptb, live_price=live, spot_age=0.2,
                     divergence_bps=None, now=now,
                     books={"Up": rich, "Down": book_dn}, vol_bps_sqrt_s=0.75)
    check("ask 0.93 → skip (eff cap)", not d5.ok and "eff_cap" in d5.reasons,
          str(d5.failed_rules()))
    # sisi Down unggul
    st.observe(-9.0, now - 10)
    d6 = st.evaluate(t_left=70, ptb=ptb, live_price=99910.0, spot_age=0.2,
                     divergence_bps=None, now=now,
                     books={"Up": book_up, "Down": Book(
                         "t3", now, bids=[Level(0.85, 5000)],
                         asks=[Level(0.86, 3000)])}, vol_bps_sqrt_s=0.75)
    check("lead -9bps → ENTER Down", d6.ok and d6.side == "Down",
          str(d6.failed_rules()))
    # spot basi
    d7 = st.evaluate(t_left=60, ptb=ptb, live_price=live, spot_age=9.0,
                     divergence_bps=None, now=now,
                     books={"Up": book_up, "Down": book_dn}, vol_bps_sqrt_s=0.75)
    check("spot age 9s → skip", not d7.ok and "spot_fresh" in d7.reasons)
    # book basi
    stale_up = Book("t", now - 30, bids=[Level(0.86, 5000)],
                    asks=[Level(0.87, 3000)])
    st.observe(8.0, now - 10)
    d7b = st.evaluate(t_left=60, ptb=ptb, live_price=live, spot_age=0.2,
                      divergence_bps=None, now=now,
                      books={"Up": stale_up, "Down": book_dn}, vol_bps_sqrt_s=0.75)
    check("book age 30s → skip", not d7b.ok and "book_fresh" in d7b.reasons,
          str(d7b.failed_rules()))
    # vol filter on
    cfg.strategy.use_vol_filter = True
    st2 = LateWindowStrategy(cfg)
    st2.observe(8.0, now - 10)
    d8 = st2.evaluate(t_left=60, ptb=ptb, live_price=live, spot_age=0.2,
                      divergence_bps=None, now=now,
                      books={"Up": book_up, "Down": book_dn}, vol_bps_sqrt_s=3.0)
    check("vol tinggi (3bps/√s) + EV filter → skip", not d8.ok,
          str(d8.failed_rules()))
    cfg.strategy.use_vol_filter = False


def test_broker_store():
    print("[4] Broker paper + store SQLite (buy → resolve → PnL & streak)")
    tmp = Path(tempfile.mkdtemp(prefix="updown-test-"))
    cfg = mkcfg()
    cfg.data_dir = tmp
    store = Store(tmp / "test.db")
    broker = PaperBroker(cfg, store)
    check("saldo awal 1000", approx(broker.balance, 1000.0))
    mkt = Market(slug="btc-updown-5m-1700000000", condition_id="0x1",
                 question="test", start_ts=1700000000, end_ts=1700000300,
                 token_up="tu", token_down="td")
    book = Book("tu", time.time(), bids=[Level(0.86, 5000)],
                asks=[Level(0.87, 3000), Level(0.88, 5000)])
    pos = broker.market_buy(mkt, "Up", book, 25.0, t_left=60, lead_bps=8.0,
                            best_ask_at_signal=0.87, ptb=100000, spot=100080)
    check("posisi terbuka", pos is not None and pos.shares > 0)
    expect_bal = 1000.0 - pos.stake_usdc - pos.fees
    check("saldo terpotong stake+fee", approx(broker.balance, expect_bal, 1e-6),
          f"{broker.balance} vs {expect_bal}")
    pid = store.insert_position(pos, mkt) if pos.id is None else pos.id
    check("posisi tersimpan di db", pid is not None and pid > 0)
    # win
    settled = broker.resolve(mkt.slug, "Up", "test")
    check("resolve WIN", settled and settled[0].status == "WON")
    pnl_want = settled[0].shares - settled[0].stake_usdc - settled[0].fees
    check("PnL win = shares − stake − fee", approx(settled[0].pnl_net, pnl_want, 1e-6),
          f"{settled[0].pnl_net} vs {pnl_want}")
    check("saldo kembali + pnl",
          approx(broker.balance, 1000.0 + pnl_want, 1e-6))
    # loss streak & pause
    cfg.risk.loss_streak_pause = 2
    cfg.risk.pause_rounds = 3
    for i in range(2):
        b2 = Book("tu", time.time(), bids=[Level(0.86, 5000)],
                  asks=[Level(0.87, 5000)])
        p2 = broker.market_buy(mkt, "Up", b2, 25.0, t_left=60, lead_bps=8.0,
                               best_ask_at_signal=0.87, ptb=100000, spot=100080)
        broker.resolve(mkt.slug, "Down", "test")
    can, why = broker.can_trade()
    check("2 loss beruntun → pause", not can and "pause" in why, why)
    broker.on_round_end()
    check("pause berkurang per round", broker.pause_rounds_left == 2)
    # store roundtrip
    store.upsert_round(slug=mkt.slug, start_ts=mkt.start_ts, end_ts=mkt.end_ts,
                       ptb=100000, ptb_source="chainlink", winner="Up",
                       resolution_source="test", traded=1, our_side="Up",
                       pnl=1.23)
    r = store.get_round(mkt.slug)
    check("round tersimpan", r and r["winner"] == "Up" and approx(r["pnl"], 1.23))
    rows = store.all_closed()
    check("3 posisi closed di db", len(rows) == 3, str(len(rows)))
    st = compute_stats(rows)
    check("stats: 1W 2L", st["wins"] == 1 and st["losses"] == 2, str(st))
    check("stats: net pnl cocok",
          approx(st["net_pnl"], sum(x["pnl_net"] for x in rows), 0.01))
    # equity snapshot
    store.snapshot_equity(1000.0, 990.0, 10.0)
    check("equity series", len(store.equity_series()) >= 1)
    store.close()


def test_config_runtime():
    print("[5] Config: yaml + env + runtime override (/set)")
    cfg = load()
    check("config.yaml kebaca", cfg.strategy.min_lead_bps == 7.0,
          str(cfg.strategy.min_lead_bps))
    check("window 90/30", cfg.strategy.window_start_s == 90
          and cfg.strategy.window_end_s == 30)
    check("fee 0.07", approx(cfg.market.fee_rate, 0.07))
    tmp = Path(tempfile.mkdtemp(prefix="updown-rt-")) / "runtime.json"
    cfg.runtime_path = tmp
    ok = save_runtime(cfg, "min_lead_bps", "6.5")
    check("/set min_lead_bps 6.5", ok and approx(cfg.strategy.min_lead_bps, 6.5))
    cfg2 = load()
    cfg2.runtime_path = tmp
    # reload harus mengambil override dari runtime.json
    import json
    rt = json.loads(tmp.read_text(encoding="utf-8"))
    check("runtime.json tertulis", rt.get("min_lead_bps") == "6.5", str(rt))
    ok2 = save_runtime(cfg, "bukan.key", "1")
    check("key invalid ditolak", not ok2)


def test_chainlink_helpers():
    print("[7] PTB cross-validation (openPrice vs price-history)")
    S = 1794330000.0
    pts = [{"timestamp": int(S * 1000), "value": 72974.83},
           {"timestamp": int((S + 60) * 1000), "value": 72980.1},
           {"timestamp": int((S + 300) * 1000), "value": 73010.5}]
    check("hist point di open", hist_point_at(pts, S) == 72974.83)
    check("hist point di end",
          hist_point_at(pts, S + 300, lo=-25.0, hi=25.0) == 73010.5)
    check("hist point di luar window → None",
          hist_point_at(pts, S + 150, lo=-25.0, hi=25.0) is None)
    p, src, w = choose_ptb(72974.83, 72975.0, 72976.0)
    check("sumber setuju → openPrice", src == "chainlink" and not w, f"{src} {w}")
    p, src, w = choose_ptb(72880.63, 72985.9, 72999.42)
    check("beda 14bps, live dekat hist → chainlink_hist",
          src == "chainlink_hist" and p == 72985.9 and w, f"{src} {p} {w}")
    p, src, w = choose_ptb(72880.63, 72985.9, 72880.0)
    check("beda, live dekat openPrice → chainlink",
          src == "chainlink" and p == 72880.63 and w, f"{src} {p}")
    p, src, w = choose_ptb(72880.63, None, None)
    check("hanya openPrice", src == "chainlink" and p == 72880.63)
    p, src, w = choose_ptb(None, 72985.9, None)
    check("hanya hist", src == "chainlink_hist" and p == 72985.9)
    p, src, w = choose_ptb(None, None, None)
    check("tidak ada sumber", p is None and w)


def test_clobws_and_netguard():
    print("[8] CLOB WS parsing + SSRF guard")
    evt = {"asset_id": "A", "timestamp": "1799999999000",
           "bids": [{"price": "0.85", "size": "100"}, {"price": "0.86", "size": "50"}],
           "asks": [{"price": "0.87", "size": "200"}]}
    b = book_from_event(evt)
    check("book_from_event: token/ts", b.token_id == "A" and b.server_ts == 1799999999.0)
    check("book_from_event: sorted", b.best_bid == 0.86 and b.best_ask == 0.87
          and len(b.bids) == 2 and len(b.asks) == 1)
    books = {"A": b}
    pc = {"price_changes": [
        {"asset_id": "A", "price": "0.87", "size": "250", "side": "SELL"},
        {"asset_id": "A", "price": "0.86", "size": "0", "side": "BUY"},
        {"asset_id": "A", "price": "0.84", "size": "10", "side": "BUY"},
    ]}
    apply_price_changes(books, pc)
    check("delta apply: ask size update", abs(b.asks[0].size - 250) < 1e-9)
    check("delta apply: bid 0.86 dihapus", all(l.price != 0.86 for l in b.bids))
    check("delta apply: bid 0.84 masuk", any(abs(l.price - 0.84) < 1e-9
                                              for l in b.bids))
    # SSRF guard (IP literal — tanpa perlu DNS)
    for bad in ("http://127.0.0.1/x", "http://10.1.2.3/x", "http://169.254.1.1/x",
                "http://192.168.0.1/x", "http://localhost/x", "ftp://x.io/a"):
        try:
            validate_url_sync(bad)
            check(f"tolak {bad}", False)
        except UnsafeUrlError:
            check(f"tolak {bad}", True)
    try:
        validate_url_sync("https://api.binance.com/api/v3/ping")
        check("izinkan host publik (binance)", True)
    except UnsafeUrlError as e:
        check("izinkan host publik (binance)", False, str(e))


def test_format():
    print("[6] Format pesan (smoke)")
    pos = Position(market_slug="btc-updown-5m-1700000000", side="Up",
                   stake_usdc=25.0, fees=0.21, shares=28.4, avg_price=0.8803,
                   entry_ts=time.time(), t_left_s=62, lead_bps=8.1,
                   ptb=73000.0, spot_at_entry=73059.0,
                   best_ask_at_signal=0.87, slippage_cost=0.03)
    h = entry_html(pos, "Bitcoin Up or Down — test", stable_for=5.2,
                   live=73059.0, balance=974.79)
    check("entry_html ok", "ENTRY" in h and "0.870" in h, h[:80])
    s = status_html({"slug": "x", "question": "q?", "t_left": 55.0, "win_start": 90,
                     "win_end": 30, "ptb": 73000.0, "ptb_source": "chainlink",
                     "live": 73059.0, "lead_bps": 8.1, "stable_for": 5.0,
                     "fav_side": "Up", "fav_ask": 0.87, "fav_eff": 0.8779,
                     "spread": 1, "depth": 3000.0, "cl_offset": 1.2,
                     "offset_samples": 3, "clock_off": 0.1, "spot_source": "binance-ws",
                     "spot_age": 0.2, "vol": 0.71, "divergence": 3.0,
                     "paused": False, "open_positions": [], "balance": 974.79,
                     "equity": 1000.4, "day_pnl": 12.0, "closed": 7,
                     "loss_streak": 0, "pause_rounds": 0})
    check("status_html ok", "Status" in s and "RUNNING" in s)


if __name__ == "__main__":
    t0 = time.time()
    test_fees()
    test_fill_sim()
    test_strategy()
    test_broker_store()
    test_config_runtime()
    test_chainlink_helpers()
    test_clobws_and_netguard()
    test_format()
    print(f"\n{'='*50}\nHASIL: {PASS} pass, {FAIL} fail  ({time.time()-t0:.1f}s)")
    sys.exit(1 if FAIL else 0)
