"""Paper trading broker with realistic execution modeling.

Realism layers:
  • Fees        — official Polymarket crypto taker formula, applied per level:
                  fee = shares × fee_rate × p × (1 − p),  fee_rate = 0.07
                  (makers pay 0; redemption pays 0)
  • Slippage    — fills WALK the real CLOB order book; per-level participation
                  cap (default 15% of displayed size); if depth is exhausted a
                  penalty of N ticks is applied to the synthetic remainder.
  • Latency     — `latency_ms` between signal and arrival; modeled as an
                  adverse-move lottery: with probability `adverse_move_prob`
                  the whole ladder has shifted `adverse_move_ticks` against us
                  by the time the order lands.
  • Constraints — CLOB minimum order (5 shares), tick grid, price < 0.99 cap,
                  insufficient-balance handling, per-round position limit.

RNG is seeded deterministically per (market, side) so runs are reproducible.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import asdict
from typing import Optional

from .config import Config
from .models import Book, Fill, Market, Position
from .strategy import fee_per_share

log = logging.getLogger("updown.paper")

MAX_PRICE = 0.99  # synthetic fills never cross this


class FillPlan:
    def __init__(self):
        self.fills: list = []
        self.shares = 0.0
        self.cash_spent = 0.0     # excl. fees
        self.fees = 0.0
        self.rejected = ""

    @property
    def total_cost(self) -> float:
        return self.cash_spent + self.fees

    @property
    def avg_price(self) -> float:
        return self.cash_spent / self.shares if self.shares else 0.0

    @property
    def avg_eff(self) -> float:
        return self.total_cost / self.shares if self.shares else 0.0


def simulate_taker_order(levels: list, stake_usdc: float, fee_rate: float,
                         cfg: Config, rng: random.Random,
                         is_buy: bool = True) -> FillPlan:
    """Walk `levels` (asks for buy / bids for sell) with participation + adverse move.

    stake_usdc: cash budget for buys; for sells it is ignored (share count comes
    from `levels` caller side) — sells pass shares via `sell_shares`.
    """
    e = cfg.execution
    tick = cfg.market.tick_size
    plan = FillPlan()

    adverse = e.adverse_move_ticks * tick if rng.random() < e.adverse_move_prob else 0.0

    if is_buy:
        cash = stake_usdc
        queue = list(levels)
        idx = 0
        while cash > 1e-6:
            if idx < len(queue):
                lvl = queue[idx]
                price = min(lvl.price + adverse, MAX_PRICE)
                avail = lvl.size * e.participation
                idx += 1
            else:
                # depth exhausted: synthetic level with penalty ticks
                last = queue[-1].price if queue else 0.5
                price = min(last + adverse + e.exhausted_penalty_ticks * tick, MAX_PRICE)
                avail = stake_usdc * 10  # effectively unlimited at penalty price
            if price <= 0 or price >= MAX_PRICE + tick:
                break
            cost_per_share = price * (1.0 + fee_rate * (1.0 - price))  # price + fee/share
            shares = min(avail, cash / cost_per_share)
            shares = round(shares, 2)
            if shares <= 0:
                break
            gross = shares * price
            fee = shares * fee_per_share(price, fee_rate)
            if gross + fee > cash + 1e-9:
                shares = round(max(0.0, (cash) / cost_per_share), 2)
                if shares <= 0:
                    break
                gross = shares * price
                fee = shares * fee_per_share(price, fee_rate)
            cash -= gross + fee
            plan.fills.append(Fill(price=price, shares=shares, fee=fee,
                                   level_price=lvl.price if idx <= len(levels) else price))
            plan.shares += shares
            plan.cash_spent += gross
            plan.fees += fee
            if idx > len(queue) and price >= MAX_PRICE:
                break
        plan.shares = round(plan.shares, 2)
        if plan.shares < cfg.market.min_order_shares:
            plan.rejected = (f"fills {plan.shares:.2f} shares < minimum "
                             f"{cfg.market.min_order_shares:g}")
            plan.fills, plan.shares, plan.cash_spent, plan.fees = [], 0.0, 0.0, 0.0
    return plan


def simulate_sell(levels: list, shares_to_sell: float, fee_rate: float,
                  cfg: Config, rng: random.Random) -> FillPlan:
    """Sell `shares_to_sell` walking bids (adverse = ladder shifts DOWN)."""
    e = cfg.execution
    tick = cfg.market.tick_size
    plan = FillPlan()
    adverse = e.adverse_move_ticks * tick if rng.random() < e.adverse_move_prob else 0.0
    remaining = shares_to_sell
    queue = list(levels)
    idx = 0
    while remaining > 1e-6:
        if idx < len(queue):
            lvl = queue[idx]
            price = max(lvl.price - adverse, tick)
            avail = lvl.size * e.participation
            idx += 1
        else:
            last = queue[-1].price if queue else 0.5
            price = max(last - adverse - e.exhausted_penalty_ticks * tick, tick)
            avail = shares_to_sell * 10
        shares = round(min(avail, remaining), 2)
        if shares <= 0:
            break
        gross = shares * price
        fee = shares * fee_per_share(price, fee_rate)
        plan.fills.append(Fill(price=price, shares=shares, fee=fee, level_price=price))
        plan.shares += shares
        plan.cash_spent += gross      # = proceeds for sells
        plan.fees += fee
        remaining -= shares
    plan.shares = round(plan.shares, 2)
    return plan


class PaperBroker:
    def __init__(self, cfg: Config, store=None):
        self.cfg = cfg
        self.store = store
        self.balance = cfg.paper.start_balance
        self.open_positions: list = []
        self.closed_count = 0
        self.marks: dict = {}          # (slug, side) -> latest mid for equity marking
        self.loss_streak = 0
        self.pause_rounds_left = 0
        self._day = self._utc_day()
        self._day_start_balance = self.balance
        self._day_realized = 0.0

    @staticmethod
    def _utc_day() -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def _roll_day(self):
        day = self._utc_day()
        if day != self._day:
            self._day = day
            self._day_realized = 0.0

    # ── risk gates ──
    def can_trade(self) -> tuple:
        self._roll_day()
        if self.pause_rounds_left > 0:
            return False, f"pause {self.pause_rounds_left} round (loss streak)"
        r = self.cfg.risk
        if r.daily_loss_limit_usdc > 0 and self._day_realized <= -r.daily_loss_limit_usdc:
            return False, f"daily loss limit tercapai ({self._day_realized:.2f})"
        if len(self.open_positions) >= r.max_open_positions:
            return False, "posisi terbuka maks"
        if self.balance < 1.0:
            return False, "saldo habis"
        return True, ""

    def stake_for(self) -> float:
        ex = self.cfg.execution
        if ex.stake_mode == "equity_pct":
            stake = self.equity() * ex.stake_pct / 100.0
        else:
            stake = ex.stake_usdc
        stake = min(stake, ex.max_stake_usdc, max(0.0, self.balance - 0.5))
        return round(stake, 2)

    def equity(self) -> float:
        open_val = 0.0
        for p in self.open_positions:
            mark = self.marks.get((p.market_slug, p.side))
            open_val += p.shares * (mark if mark is not None else p.avg_price)
        return self.balance + open_val

    # ── trading ──
    def market_buy(self, market: Market, side: str, book: Book, stake: float,
                   *, t_left: float, lead_bps: float, best_ask_at_signal: float,
                   ptb: float, spot: float) -> Optional:
        if not book.asks:
            log.warning("buy aborted: empty ask side")
            return None
        if stake > self.balance:
            stake = round(self.balance - 0.01, 2)
        if stake < self.cfg.market.min_order_shares * book.asks[0].price:
            log.warning("buy aborted: stake %.2f too small", stake)
            return None
        rng = random.Random(f"{market.slug}|{side}")
        plan = simulate_taker_order(book.asks, stake, market.fee_rate, self.cfg, rng,
                                    is_buy=True)
        if plan.rejected:
            log.warning("order rejected: %s", plan.rejected)
            return None
        # latency: order lands `latency_ms` later
        entry_ts = time.time() + self.cfg.execution.latency_ms / 1000.0
        slippage = (plan.avg_price - best_ask_at_signal) * plan.shares
        pos = Position(
            market_slug=market.slug, side=side, stake_usdc=round(plan.cash_spent, 4),
            fees=round(plan.fees, 4), shares=plan.shares, avg_price=round(plan.avg_price, 6),
            entry_ts=entry_ts, t_left_s=round(t_left, 1), lead_bps=round(lead_bps, 2),
            ptb=ptb, spot_at_entry=spot, best_ask_at_signal=best_ask_at_signal,
            slippage_cost=round(max(0.0, slippage), 4),
            fills=[asdict(f) for f in plan.fills],
        )
        self.balance -= (pos.stake_usdc + pos.fees)   # rounded, ledger-consistent
        self.open_positions.append(pos)
        if self.store:
            pos.id = self.store.insert_position(pos, market)
        log.info("PAPER BUY %s %s: %.2f shares @ avg %.4f (eff %.4f), fee %.4f, "
                 "slip %.4f, stake %.2f", market.slug, side, pos.shares, pos.avg_price,
                 pos.eff_price, pos.fees, pos.slippage_cost, plan.total_cost)
        return pos

    def market_sell(self, pos: Position, book: Book, reason: str) -> bool:
        """Early exit (flip-exit). Sells whole position walking bids."""
        if not book.bids:
            return False
        rng = random.Random(f"{pos.market_slug}|{pos.side}|sell|{int(time.time())}")
        plan = simulate_sell(book.bids, pos.shares, self.cfg.market.fee_rate,
                             self.cfg, rng)
        if plan.shares <= 0:
            return False
        proceeds = plan.cash_spent - plan.fees
        self.balance += proceeds
        pos.status = "SOLD"
        pos.payout = round(plan.cash_spent, 4)
        pos.pnl_net = round(proceeds - pos.stake_usdc - pos.fees, 4)
        pos.exit_ts = time.time()
        pos.exit_reason = reason
        self.open_positions.remove(pos)
        self._register_close(pos)
        return True

    # ── resolution ──
    def resolve(self, slug: str, winner: str, resolution_source: str = "") -> list:
        settled = []
        for pos in [p for p in self.open_positions if p.market_slug == slug]:
            if pos.side == winner:
                pos.payout = round(pos.shares * 1.0, 4)
                pos.pnl_net = round(pos.payout - pos.stake_usdc - pos.fees, 4)
                pos.status = "WON"
            else:
                pos.payout = 0.0
                pos.pnl_net = round(-(pos.stake_usdc + pos.fees), 4)
                pos.status = "LOST"
            pos.exit_ts = time.time()
            pos.exit_reason = "resolution"
            self.balance += pos.payout
            self.open_positions.remove(pos)
            self._register_close(pos, winner, resolution_source)
            settled.append(pos)
        return settled

    def _register_close(self, pos: Position, winner: Optional[str] = None,
                        resolution_source: str = ""):
        self.closed_count += 1
        self._day_realized += pos.pnl_net
        if pos.status in ("WON",):
            self.loss_streak = 0
        elif pos.status in ("LOST", "SOLD"):
            if pos.pnl_net < 0:
                self.loss_streak += 1
                r = self.cfg.risk
                if r.loss_streak_pause and self.loss_streak >= r.loss_streak_pause:
                    self.pause_rounds_left = r.pause_rounds
                    self.loss_streak = 0
                    log.warning("loss streak %d → pause %d rounds",
                                r.loss_streak_pause, r.pause_rounds)
            else:
                self.loss_streak = 0
        if self.store:
            self.store.close_position(pos, winner, resolution_source)

    def on_round_end(self):
        if self.pause_rounds_left > 0:
            self.pause_rounds_left -= 1

    # ── restart recovery ──
    def bootstrap_from_store(self):
        """Restore balance/streaks/day-pnl from persisted history."""
        if not self.store:
            return
        closed = self.store.all_closed()
        open_pos = self.store.restore_open()
        self.balance = self.cfg.paper.start_balance \
            + sum(r["pnl_net"] or 0 for r in closed) \
            - sum((r["stake_usdc"] or 0) + (r["fees"] or 0) for r in open_pos)
        self.closed_count = len(closed)
        self.open_positions = open_pos
        streak = 0
        for r in closed:
            if (r["pnl_net"] or 0) < 0:
                streak += 1
            else:
                streak = 0
        self.loss_streak = streak
        day0 = time.strftime("%Y-%m-%d", time.gmtime())
        self._day_realized = sum(
            r["pnl_net"] or 0 for r in closed
            if r.get("exit_ts") and time.strftime("%Y-%m-%d", time.gmtime(r["exit_ts"])) == day0)
        log.info("bootstrap: balance=%.2f open=%d closed=%d streak=%d day_pnl=%.2f",
                 self.balance, len(open_pos), len(closed), streak, self._day_realized)
