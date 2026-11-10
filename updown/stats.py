"""Performance statistics computed from closed paper positions."""
from __future__ import annotations

import math
from typing import Optional


def compute_stats(rows: list, equity_series: Optional[list] = None,
                  start_balance: float = 1000.0) -> dict:
    """rows: dicts from store.all_closed() / positions_since()."""
    st = {
        "n": len(rows), "wins": 0, "losses": 0, "sold": 0,
        "win_rate": 0.0, "net_pnl": 0.0, "gross_win": 0.0, "gross_loss": 0.0,
        "profit_factor": 0.0, "total_fees": 0.0, "total_slippage": 0.0,
        "total_staked": 0.0, "avg_entry": 0.0, "avg_eff": 0.0,
        "avg_win": 0.0, "avg_loss": 0.0, "expectancy": 0.0,
        "best": 0.0, "worst": 0.0, "roi_pct": 0.0,
        "max_drawdown": 0.0, "max_drawdown_pct": 0.0,
        "cur_streak": 0, "cur_streak_type": "", "max_win_streak": 0,
        "max_loss_streak": 0, "avg_t_left": 0.0, "avg_lead_bps": 0.0,
    }
    if not rows:
        return st

    wins = [r for r in rows if r["status"] == "WON"]
    losses = [r for r in rows if r["status"] == "LOST"]
    sold = [r for r in rows if r["status"] == "SOLD"]
    st.update({"wins": len(wins), "losses": len(losses), "sold": len(sold)})

    net = sum(r["pnl_net"] for r in rows)
    gw = sum(r["pnl_net"] for r in rows if r["pnl_net"] > 0)
    gl = -sum(r["pnl_net"] for r in rows if r["pnl_net"] < 0)
    st["net_pnl"] = round(net, 2)
    st["gross_win"] = round(gw, 2)
    st["gross_loss"] = round(gl, 2)
    st["profit_factor"] = round(gw / gl, 3) if gl > 1e-9 else float("inf") if gw > 0 else 0.0
    st["total_fees"] = round(sum(r["fees"] or 0 for r in rows), 2)
    st["total_slippage"] = round(sum(r["slippage_cost"] or 0 for r in rows), 2)
    st["total_staked"] = round(sum(r["stake_usdc"] or 0 for r in rows), 2)
    st["win_rate"] = round(len(wins) / len(rows) * 100, 1)

    prices = [r["avg_price"] for r in rows if r["avg_price"]]
    effs = [r["eff_price"] for r in rows if r.get("eff_price")]
    st["avg_entry"] = round(sum(prices) / len(prices), 4) if prices else 0.0
    st["avg_eff"] = round(sum(effs) / len(effs), 4) if effs else 0.0
    st["avg_win"] = round(sum(r["pnl_net"] for r in wins) / len(wins), 2) if wins else 0.0
    st["avg_loss"] = round(sum(r["pnl_net"] for r in losses + sold) / len(losses + sold), 2) \
        if (losses or sold) else 0.0
    st["expectancy"] = round(net / len(rows), 3)
    pnls = [r["pnl_net"] for r in rows]
    st["best"] = round(max(pnls), 2)
    st["worst"] = round(min(pnls), 2)
    st["roi_pct"] = round(net / st["total_staked"] * 100, 2) if st["total_staked"] else 0.0
    st["avg_t_left"] = round(sum(r["t_left_s"] or 0 for r in rows) / len(rows), 1)
    st["avg_lead_bps"] = round(sum(abs(r["lead_bps"] or 0) for r in rows) / len(rows), 1)

    # streaks
    cw = cl = mw = ml = 0
    for r in rows:
        if r["pnl_net"] >= 0:
            cw += 1; cl = 0; mw = max(mw, cw)
        else:
            cl += 1; cw = 0; ml = max(ml, cl)
    st["max_win_streak"], st["max_loss_streak"] = mw, ml
    if rows[-1]["pnl_net"] >= 0:
        st["cur_streak"], st["cur_streak_type"] = cw, "W"
    else:
        st["cur_streak"], st["cur_streak_type"] = cl, "L"

    # drawdown from equity curve (fallback: cumulative pnl)
    if equity_series and len(equity_series) > 1:
        vals = [e for _, e in equity_series]
    else:
        vals, acc = [], 0.0
        for r in rows:
            acc += r["pnl_net"]
            vals.append(start_balance + acc)
    peak = -1e18
    mdd = mdd_pct = 0.0
    for v in vals:
        peak = max(peak, v)
        mdd = max(mdd, peak - v)
        mdd_pct = max(mdd_pct, (peak - v) / peak * 100 if peak > 0 else 0.0)
    st["max_drawdown"] = round(mdd, 2)
    st["max_drawdown_pct"] = round(mdd_pct, 2)
    return st


def format_stats_md(st: dict, title: str = "Performance") -> str:
    pf = "∞" if st["profit_factor"] == float("inf") else f"{st['profit_factor']:.2f}"
    return (
        f"**{title}**\n"
        f"Trades: {st['n']} (W{st['wins']}/L{st['losses']}/S{st['sold']}) — "
        f"win rate {st['win_rate']}%\n"
        f"Net PnL: {st['net_pnl']:+.2f} USDC (ROI {st['roi_pct']:+.2f}%)\n"
        f"Profit factor: {pf} — expectancy {st['expectancy']:+.3f}/trade\n"
        f"Avg win {st['avg_win']:+.2f} / avg loss {st['avg_loss']:+.2f} — "
        f"best {st['best']:+.2f} / worst {st['worst']:+.2f}\n"
        f"Fees paid: {st['total_fees']:.2f} — slippage: {st['total_slippage']:.2f}\n"
        f"Avg entry {st['avg_entry']:.3f} (eff {st['avg_eff']:.4f}) — "
        f"avg t_left {st['avg_t_left']:.0f}s @ |lead| {st['avg_lead_bps']:.1f}bps\n"
        f"Max drawdown: {st['max_drawdown']:.2f} ({st['max_drawdown_pct']:.2f}%) — "
        f"streaks W{st['max_win_streak']}/L{st['max_loss_streak']}"
    )
