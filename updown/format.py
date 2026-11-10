"""Message templates: Telegram HTML + Markdown reports."""
from __future__ import annotations

import html
import time
from typing import Optional

from .models import Position
from .stats import compute_stats


def esc(s) -> str:
    return html.escape(str(s))


def usd(x: float, sign: bool = False) -> str:
    return f"{'+' if sign and x >= 0 else ''}{x:,.2f}"


def utc_hm(ts: float) -> str:
    return time.strftime("%H:%M", time.gmtime(ts))


def utc_ymd(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


# ─────────────────────────── Telegram HTML ───────────────────────────

def entry_html(pos: Position, question: str, *, stable_for: float = 0.0,
               live: float = 0.0, balance: float = 0.0) -> str:
    emoji = "🟢" if pos.side == "Up" else "🔴"
    fee_share = pos.fees / pos.shares if pos.shares else 0
    return (
        f"{emoji} <b>ENTRY — {esc(pos.side)}</b>\n"
        f"📈 {esc(question)}\n"
        f"⏱️ Sisa <b>{pos.t_left_s:.0f}s</b> | Lead <b>{pos.lead_bps:+.1f} bps</b>"
        f" (stabil {stable_for:.0f}s)\n"
        f"🎯 PTB {pos.ptb:,.2f} → Live {live:,.2f}\n"
        f"💵 Ask {pos.best_ask_at_signal:.3f} → eff <b>{pos.eff_price:.4f}</b>"
        f" (fee/sh {fee_share:.4f})\n"
        f"📦 {pos.shares:.2f} shares @ {pos.avg_price:.4f} | stake ${usd(pos.stake_usdc)}\n"
        f"🧾 Fee ${usd(pos.fees)} | slippage ${usd(pos.slippage_cost)}\n"
        f"💰 Balance ${usd(balance)}"
    )


def resolution_html(pos: Position, winner: str, *, ptb: Optional[float],
                    close: Optional[float], res_source: str,
                    day: Optional[dict] = None, total: Optional[dict] = None,
                    balance: float = 0.0) -> str:
    won = pos.status == "WON"
    head = ("✅ <b>WIN</b>" if won else
            "➖ <b>EXIT</b>" if pos.status == "SOLD" else "❌ <b>LOSS</b>")
    lines = [
        f"🏁 <b>RESOLVED: {esc(winner)}</b> — {head}",
        f"📈 {esc(pos.market_slug)} | sisi kita: <b>{esc(pos.side)}</b>"
        f" (entry {utc_hm(pos.entry_ts)} UTC)",
    ]
    if ptb and close:
        move = (close - ptb) / ptb * 1e4
        lines.append(f"🎯 PTB {ptb:,.2f} → Close {close:,.2f} ({move:+.1f} bps)"
                     f" <i>[{esc(res_source)}]</i>")
    lines.append(
        f"💵 Payout ${usd(pos.payout)} | PnL <b>{usd(pos.pnl_net, sign=True)}</b>"
        f" (fee ${usd(pos.fees)}, slip ${usd(pos.slippage_cost)})")
    if day:
        lines.append(
            f"📅 Hari ini: {usd(day['net_pnl'], sign=True)} USDC"
            f" ({day['wins']}W/{day['losses']}L, WR {day['win_rate']}%)")
    if total:
        pf = "∞" if total["profit_factor"] == float("inf") else f"{total['profit_factor']:.2f}"
        lines.append(
            f"📊 Total: {usd(total['net_pnl'], sign=True)} USDC"
            f" ({total['n']} trades, WR {total['win_rate']}%, PF {pf})")
    lines.append(f"💰 Balance ${usd(balance)}")
    return "\n".join(lines)


def error_html(msg: str) -> str:
    return f"⚠️ <b>Bot alert</b>\n{esc(msg)}"


def status_html(s: dict) -> str:
    """s = engine.status_snapshot()"""
    rows = [f"🤖 <b>Status</b> — {'⏸ PAUSED' if s['paused'] else '▶️ RUNNING'}"]
    if s.get("slug"):
        rows.append(f"📈 {esc(s['question'])}")
        rows.append(f"⏱️ t_left <b>{s['t_left']:.0f}s</b>"
                    f" | window {s['win_start']}–{s['win_end']}s")
        if s.get("ptb"):
            rows.append(f"🎯 PTB {s['ptb']:,.2f} ({esc(s['ptb_source'])})"
                        f" → Live {s['live']:,.2f}"
                        f" | lead <b>{s['lead_bps']:+.1f} bps</b>"
                        f" (stabil {s['stable_for']:.0f}s)")
        if s.get("fav_ask") is not None:
            rows.append(f"💵 Fav {esc(s['fav_side'])}: ask {s['fav_ask']:.3f}"
                        f" eff {s['fav_eff']:.4f} | spread {s['spread']}t"
                        f" | depth {s['depth']:.0f}sh")
        rows.append(f"🔗 offset CL-ex: {s['cl_offset']:+.2f}"
                    f" ({s['offset_samples']} samples) | clock {s['clock_off']:+.2f}s")
    else:
        rows.append("Menunggu round berikutnya…")
    rows.append(f"📡 Spot: {esc(s['spot_source'])} age {s['spot_age']:.1f}s"
                f" | vol {s['vol']:.2f} bps/√s"
                + (f" | Δ sumber {s['divergence']:.1f}bps" if s.get('divergence') is not None else ""))
    open_pos = s.get("open_positions") or []
    if open_pos:
        p = open_pos[0]
        rows.append(f"📂 Open: {esc(p['side'])} {p['shares']:.1f}sh @ {p['avg_price']:.3f}"
                    f" ({esc(p['market_slug'])})")
    rows.append(f"💰 Balance ${usd(s['balance'])} | Equity ${usd(s['equity'])}"
                f" | hari ini {usd(s['day_pnl'], sign=True)}")
    rows.append(f"🧮 Trades: {s['closed']} closed"
                f" | streak: {s['loss_streak']}L"
                + (f" | pause {s['pause_rounds']} round" if s.get('pause_rounds') else ""))
    return "\n".join(rows)


def trades_html(rows: list) -> str:
    if not rows:
        return "Belum ada trade."
    lines = ["🧾 <b>Trades terakhir</b>"]
    for r in rows[:10]:
        ico = {"WON": "✅", "LOST": "❌", "SOLD": "➖", "OPEN": "📂"}.get(r["status"], "•")
        pnl = r["pnl_net"] or 0
        pnl_s = f"{usd(pnl, sign=True)}" if r["status"] != "OPEN" else "—"
        lines.append(
            f"{ico} <code>{esc(r['market_slug'])[-14:]}</code>"
            f" {esc(r['side']):4s} {r['shares']:.0f}sh @ {r['avg_price']:.3f}"
            f" t-{r['t_left_s']:.0f}s lead {r['lead_bps']:+.1f}"
            f" → <b>{pnl_s}</b>")
    return "\n".join(lines)


def rounds_html(rows: list) -> str:
    if not rows:
        return "Belum ada round tercatat."
    lines = ["🕐 <b>Rounds terakhir</b>"]
    for r in rows[:12]:
        ico = "✅" if r.get("traded") and (r.get("pnl") or 0) > 0 else \
              "❌" if r.get("traded") else "👀"
        move = ""
        if r.get("ptb") and r.get("close_chainlink"):
            move = f" ({(r['close_chainlink'] - r['ptb']) / r['ptb'] * 1e4:+.1f}bps)"
        skip = f" — <i>{esc(r['skipped_reason'][:38])}</i>" if r.get("skipped_reason") and not r.get("traded") else ""
        lines.append(
            f"{ico} <code>{esc(r['slug'])[-10:]}</code>"
            f" → {esc(r.get('winner') or '?')}{move}"
            f"{' PnL ' + usd(r['pnl'], sign=True) if r.get('traded') else ''}{skip}")
    return "\n".join(lines)


def stats_html(st: dict, title: str) -> str:
    pf = "∞" if st["profit_factor"] == float("inf") else f"{st['profit_factor']:.2f}"
    return (
        f"📊 <b>{esc(title)}</b>\n"
        f"Trades: {st['n']} (W{st['wins']}/L{st['losses']}/S{st['sold']})"
        f" — WR <b>{st['win_rate']}%</b>\n"
        f"Net PnL: <b>{usd(st['net_pnl'], sign=True)}</b> USDC"
        f" (ROI {st['roi_pct']:+.2f}%) | PF {pf}\n"
        f"Expectancy {st['expectancy']:+.3f}/trade"
        f" | avg W {usd(st['avg_win'], sign=True)} / avg L {usd(st['avg_loss'], sign=True)}\n"
        f"Best {usd(st['best'], sign=True)} / worst {usd(st['worst'], sign=True)}\n"
        f"🧾 Fees ${usd(st['total_fees'])} | slippage ${usd(st['total_slippage'])}"
        f" | staked ${usd(st['total_staked'])}\n"
        f"🎯 Avg entry {st['avg_entry']:.3f} (eff {st['avg_eff']:.4f})"
        f" | t_left {st['avg_t_left']:.0f}s @ |lead| {st['avg_lead_bps']:.1f}bps\n"
        f"📉 MaxDD ${usd(st['max_drawdown'])} ({st['max_drawdown_pct']:.2f}%)"
        f" | streaks W{st['max_win_streak']}/L{st['max_loss_streak']}"
        f" (now {st['cur_streak']}{st['cur_streak_type']})"
    )


def config_html(cfg, overrides: dict, settables: list | None = None) -> str:
    s, e, r = cfg.strategy, cfg.execution, cfg.risk
    lines = [
        "⚙️ <b>Config efektif</b> (runtime override ditandai *)",
        "<pre>",
        f"window      : {s.window_start_s}s → {s.window_end_s}s",
        f"min_lead    : {s.min_lead_bps} bps (stabil {s.lead_stable_s}s)",
        f"max_eff     : {s.max_eff_price}  (max_ask {s.max_ask})",
        f"fee_rate    : {cfg.market.fee_rate}  vol_filter: {s.use_vol_filter}",
        f"stake       : {e.stake_mode} {e.stake_usdc if e.stake_mode=='fixed' else str(e.stake_pct)+'%'}"
        f" (max {e.max_stake_usdc})",
        f"slippage    : adv {e.adverse_move_prob:.0%}×{e.adverse_move_ticks}tick"
        f" part {e.participation:.0%} lat {e.latency_ms}ms",
        f"risk        : daily -{r.daily_loss_limit_usdc} | streak {r.loss_streak_pause}"
        f"→pause {r.pause_rounds}",
        f"feeds       : spot {cfg.feeds.spot_primary}"
        f"+{cfg.feeds.spot_secondary or '∅'} | CL poll {cfg.feeds.chainlink_poll_sec}s",
        "</pre>",
    ]
    if overrides:
        lines.append("Runtime: " + ", ".join(f"<code>{esc(k)}={esc(v)}</code>"
                                             for k, v in overrides.items()))
    if settables:
        lines.append("Keys bisa diset: " + ", ".join(
            f"<code>{esc(k)}</code>" for k in settables))
    lines.append("Set: <code>/set min_lead_bps 6.5</code> — reset: "
                 "<code>/unset min_lead_bps</code>")
    return "\n".join(lines)


def book_html(snap: dict) -> str:
    sides = snap.get("sides") or {}
    if not sides:
        return "📭 Book belum tersedia (menunggu round / WS terhubung)."
    icon = "🟢" if snap.get("connected") else "🔴"
    lines = [f"📖 <b>Orderbook</b> — WS {icon}"]
    for side, s in sides.items():
        lines.append(
            f"— <b>{esc(side)}</b> (age {s['age']}s) "
            f"best {s['best_bid']}/{s['best_ask']} mid {s['mid']:.3f}")
        asks = "  ".join(f"{p:.2f}×{sz:.0f}" for p, sz in s["asks"][:3])
        bids = "  ".join(f"{p:.2f}×{sz:.0f}" for p, sz in s["bids"][:3])
        lines.append(f"  ask: {asks}")
        lines.append(f"  bid: {bids}")
        if s.get("last_trade"):
            lt = s["last_trade"]
            lines.append(f"  last trade: {esc(str(lt.get('side')))} "
                         f"{esc(str(lt.get('price')))} × {esc(str(lt.get('size')))}")
    return "\n".join(lines)


# ─────────────────────────── Markdown report ───────────────────────────

def daily_report_md(day: str, st_day: dict, st_all: dict, rows: list,
                    rounds: list) -> str:
    md = [
        f"# Daily Report — {day} (UTC)",
        "",
        "## Ringkasan hari ini",
        f"- Trades: **{st_day['n']}** (W{st_day['wins']}/L{st_day['losses']}/S{st_day['sold']})"
        f" — win rate **{st_day['win_rate']}%**",
        f"- Net PnL: **{st_day['net_pnl']:+,.2f} USDC** (ROI {st_day['roi_pct']:+.2f}%)",
        f"- Fees: {st_day['total_fees']:.2f} | slippage: {st_day['total_slippage']:.2f}",
        f"- Max drawdown: {st_day['max_drawdown']:.2f} ({st_day['max_drawdown_pct']:.2f}%)",
        "",
        "## Kumulatif",
        f"- Net PnL: **{st_all['net_pnl']:+,.2f} USDC** dari {st_all['n']} trades"
        f" — WR {st_all['win_rate']}%, PF "
        + ("∞" if st_all["profit_factor"] == float("inf") else f"{st_all['profit_factor']:.2f}"),
        f"- Expectancy: {st_all['expectancy']:+.3f} USDC/trade"
        f" | avg entry {st_all['avg_entry']:.3f} (eff {st_all['avg_eff']:.4f})",
        "",
        "## Trades",
        "",
        "| Round | Side | Shares | Entry | Eff | t_left | Lead | Status | PnL |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        md.append(
            f"| {r['market_slug'][-14:]} | {r['side']} | {r['shares']:.1f} "
            f"| {r['avg_price']:.3f} | {r['eff_price'] or 0:.4f} | {r['t_left_s']:.0f}s "
            f"| {r['lead_bps']:+.1f} | {r['status']} | {(r['pnl_net'] or 0):+.2f} |")
    md += ["", "## Rounds (observasi terakhir)", "",
           "| Slug | Winner | PTB | Close | Move (bps) | Kita | PnL |",
           "|---|---|---|---|---|---|---|"]
    for r in rounds:
        move = ((r["close_chainlink"] - r["ptb"]) / r["ptb"] * 1e4
                if r.get("ptb") and r.get("close_chainlink") else None)
        md.append(
            f"| {r['slug'][-14:]} | {r.get('winner') or '?'} "
            f"| {r.get('ptb') or 0:,.2f} | {r.get('close_chainlink') or 0:,.2f} "
            f"| {move:+.1f} | {'—' if not r.get('traded') else (r.get('our_side') or '')} "
            f"| {(r.get('pnl') or 0):+.2f} |" if move is not None else
            f"| {r['slug'][-14:]} | {r.get('winner') or '?'} | — | — | — | — | — |")
    md += ["", f"_Generated {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}_"]
    return "\n".join(md)
