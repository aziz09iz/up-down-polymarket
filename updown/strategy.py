"""Late-window momentum strategy for Polymarket BTC 5m Up/Down.

Spec (user):
  • JANGAN sniping detik awal / lead-lag super cepat.
  • Fokus menit terakhir: sisa waktu 90–30 detik, saat lead sudah jelas.
  • Masuk jika:
      1. t_left ≤ 90s (dan ≥ 30s)
      2. |lead| = |spot − price_to_beat| / PTB ≥ 6–8 bps
      3. harga tiket favorit (setelah fee) masih < 0.88–0.90
      4. beli sisi yang unggul
Plus guard realism: lead harus stabil beberapa detik, spread/depth book
masuk akal, feed sehat (fresh + divergence antar sumber), dan filter EV
opsional berbasis model Brownian (default off).
"""
from __future__ import annotations

import logging
import math
import time
from typing import Optional

from .config import Config
from .models import Book, Decision

log = logging.getLogger("updown.strategy")


def fee_per_share(price: float, fee_rate: float) -> float:
    """Official Polymarket crypto taker fee: fee = shares × rate × p × (1−p)."""
    return fee_rate * price * (1.0 - price)


def effective_price(price: float, fee_rate: float) -> float:
    return price + fee_per_share(price, fee_rate)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class LateWindowStrategy:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lead_ok_since: Optional[float] = None
        self._lead_side: Optional[str] = None
        self.last_decision: Optional[Decision] = None

    # ── fast path: call on every spot tick to track lead stability ──
    def observe(self, lead_bps: Optional[float], now: float) -> None:
        s = self.cfg.strategy
        if lead_bps is None:
            self._lead_ok_since = None
            self._lead_side = None
            return
        side = "Up" if lead_bps > 0 else "Down"
        if abs(lead_bps) >= s.min_lead_bps:
            if self._lead_side == side and self._lead_ok_since is not None:
                pass  # still stable
            else:
                self._lead_side = side
                self._lead_ok_since = now
        else:
            self._lead_ok_since = None
            self._lead_side = None

    def stable_for(self, now: float) -> float:
        if self._lead_ok_since is None:
            return 0.0
        return now - self._lead_ok_since

    # ── full evaluation on each book refresh inside the window ──
    def evaluate(self, *, t_left: float, ptb: Optional[float],
                 live_price: Optional[float], spot_age: float,
                 divergence_bps: Optional[float], now: float,
                 books: dict, vol_bps_sqrt_s: float) -> Decision:
        """books = {"Up": Book|None, "Down": Book|None}"""
        c = self.cfg.strategy
        f = self.cfg.feeds
        fee_rate = self.cfg.market.fee_rate
        reasons: dict = {}
        metrics: dict = {"t_left": round(t_left, 1)}

        def rule(name: str, ok: bool, detail: str):
            reasons[name] = (bool(ok), detail)
            return ok

        # 0) data sanity
        have_data = rule("data", ptb is not None and live_price is not None and ptb > 0,
                         f"ptb={ptb} live={live_price}")
        if not have_data:
            return self._finish("skip", None, reasons, metrics)
        fresh = rule("spot_fresh", spot_age <= 3.0, f"age={spot_age:.1f}s")
        metrics["spot_fresh"] = fresh
        if f.max_divergence_bps > 0 and divergence_bps is not None:
            rule("divergence", divergence_bps <= f.max_divergence_bps,
                 f"{divergence_bps:.1f}bps antara sumber")

        lead_bps = (live_price - ptb) / ptb * 1e4
        side = "Up" if lead_bps > 0 else "Down"
        metrics.update({"ptb": ptb, "live": round(live_price, 2),
                        "lead_bps": round(lead_bps, 2), "side": side})

        # 1) time window
        rule("window", c.window_end_s <= t_left <= c.window_start_s,
             f"t_left={t_left:.0f}s (izin {c.window_end_s}–{c.window_start_s}s)")

        # 2) lead magnitude
        rule("lead", abs(lead_bps) >= c.min_lead_bps,
             f"{lead_bps:+.1f}bps vs min {c.min_lead_bps}bps")

        # 3) lead stability
        stable = self.stable_for(now)
        metrics["stable_for"] = round(stable, 1)
        rule("stable", stable >= c.lead_stable_s and self._lead_side == side,
             f"stabil {stable:.1f}s/{c.lead_stable_s}s")

        book: Optional[Book] = books.get(side)
        if book is None or not book.asks:
            rule("book", False, "orderbook sisi favorit tidak tersedia")
            return self._finish("skip", side, reasons, metrics)
        book_age = time.time() - book.ts
        rule("book_fresh", book_age <= c.book_max_age_s, f"umur book {book_age:.1f}s")

        ask = book.best_ask or 1.0
        eff = effective_price(ask, fee_rate)
        spread = book.spread_ticks(self.cfg.market.tick_size)
        depth = book.ask_depth(c.max_ask)
        metrics.update({"ask": ask, "eff_price": round(eff, 4), "spread_ticks": spread,
                        "depth": round(depth, 1)})

        # 4) price caps (fee-aware, per spec "di bawah 0.88–0.90 setelah fee")
        rule("ask_cap", ask <= c.max_ask + 1e-9, f"ask={ask:.3f} max={c.max_ask}")
        rule("eff_cap", eff <= c.max_eff_price + 1e-9,
             f"eff={eff:.4f} max={c.max_eff_price} (fee/sh={fee_per_share(ask, fee_rate):.4f})")

        # 5) book quality
        if spread is not None:
            rule("spread", spread <= c.max_spread_ticks, f"{spread} tick")
        rule("depth", depth >= c.min_book_shares, f"{depth:.0f} shares ≤ {c.max_ask}")

        # 6) optional EV filter (Brownian bridge estimate)
        if c.use_vol_filter:
            sig = max(vol_bps_sqrt_s, 1e-6) * math.sqrt(max(t_left, 1.0))
            p_model = norm_cdf(abs(lead_bps) / sig) if sig > 0 else 0.5
            metrics.update({"p_model": round(p_model, 4), "vol_bps_sqrt_s": vol_bps_sqrt_s})
            rule("ev", p_model - eff >= c.min_edge,
                 f"p_model={p_model:.3f} eff={eff:.3f} edge_min={c.min_edge}")

        ok = all(v[0] for v in reasons.values())
        # "wait" = window belum/tidak lagi, tapi bukan karena aturan gagal
        action = "enter" if ok else ("skip" if reasons["window"][0] else "wait")
        return self._finish(action, side if ok else None, reasons, metrics)

    def _finish(self, action: str, side: Optional[str], reasons: dict,
                metrics: dict) -> Decision:
        reasons = {k: v for k, v in reasons.items() if v[1]}
        d = Decision(action=action, side=side, reasons=reasons, metrics=metrics)
        self.last_decision = d
        return d
