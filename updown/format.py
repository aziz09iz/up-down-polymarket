"""Message templates: Telegram HTML + Markdown reports.

Gaya: header satu baris (simbol arah/status hanya di header), body key-value
rata kolom di dalam <code>, tanpa emoji per-baris.
"""
from __future__ import annotations

import html
import time
from typing import Optional

from .models import Position


def esc(s) -> str:
    return html.escape(str(s))


def usd(x: float, sign: bool = False) -> str:
    return f"{'+' if sign and x >= 0 else ''}{x:,.2f}"


def utc_hm(ts: float) -> str:
    return time.strftime("%H:%M", time.gmtime(ts))


def utc_ymd(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def kv(pairs) -> str:
    """Baris key-value rata kolom, untuk dipakai di dalam <code>."""
    pairs = [(k, v) for k, v in pairs if v is not None]
    if not pairs:
        return ""
    w = max(len(k) for k, _ in pairs)
    return "\n".join(f"{k.ljust(w)}  {v}" for k, v in pairs)


def code_block(body: str) -> str:
    return f"<code>\n{body}\n</code>" if body else ""


def arrow(side: str) -> str:
    return "▲" if side == "Up" else "▼"


# ─────────────────────────── Telegram HTML ───────────────────────────

def entry_html(pos: Position, question: str, *, stable_for: float = 0.0,
               live: float = 0.0, balance: float = 0.0) -> str:
    fee_share = pos.fees / pos.shares if pos.shares else 0
    lead = f"{pos.lead_bps:+.1f} bps"
    if stable_for >= 1:
        lead += f" (stabil {stable_for:.0f}s)"
    ptb_row = f"{pos.ptb:,.2f}"
    if live:
        ptb_row += f" → {live:,.2f} live"
    body = kv([
        ("Sisa", f"{pos.t_left_s:.0f}s"),
        ("Lead", lead),
        ("PTB", ptb_row),
        ("Ask", f"{pos.best_ask_at_signal:.3f} → eff {pos.eff_price:.4f}"
                f" (fee/sh {fee_share:.4f})"),
        ("Fill", f"{pos.shares:.2f} sh @ {pos.avg_price:.4f}"),
        ("Stake", f"${usd(pos.stake_usdc)} · fee ${usd(pos.fees)}"
                  f" · slip ${usd(pos.slippage_cost)}"),
    ])
    return (f"{arrow(pos.side)} <b>ENTRY {esc(pos.side)}</b> · PAPER\n"
            f"{esc(question)}\n"
            f"{code_block(body)}\n"
            f"Balance ${usd(balance)}")


def resolution_html(pos: Position, winner: str, *, ptb: Optional[float],
                    close: Optional[float], res_source: str,
                    day: Optional[dict] = None, total: Optional[dict] = None,
                    balance: float = 0.0) -> str:
    head = ("✅ <b>WIN" if pos.status == "WON" else
            "➖ <b>EXIT" if pos.status == "SOLD" else "❌ <b>LOSS")
    win_s = f" · winner {esc(winner)}" if winner != pos.side else ""
    lines = [
        f"{head} — {esc(pos.side)}</b>{win_s}",
        f"{esc(pos.market_slug)} · entry {utc_hm(pos.entry_ts)} UTC",
    ]
    rows = []
    if ptb and close:
        move = (close - ptb) / ptb * 1e4
        rows.append(("PTB", f"{ptb:,.2f} → {close:,.2f} ({move:+.1f} bps)"
                            f" [{esc(res_source)}]"))
    rows.append(("Payout", f"${usd(pos.payout)}"))
    rows.append(("PnL", f"{usd(pos.pnl_net, sign=True)} USDC"
                        f" (fee ${usd(pos.fees)} · slip ${usd(pos.slippage_cost)})"))
    if day:
        rows.append(("Hari ini", f"{usd(day['net_pnl'], sign=True)} USDC"
                                 f" ({day['wins']}W/{day['losses']}L"
                                 f" · WR {day['win_rate']}%)"))
    if total:
        pf = "∞" if total["profit_factor"] == float("inf") \
            else f"{total['profit_factor']:.2f}"
        rows.append(("Total", f"{usd(total['net_pnl'], sign=True)} USDC"
                              f" ({total['n']} trades · WR {total['win_rate']}%"
                              f" · PF {pf})"))
    lines.append(code_block(kv(rows)))
    lines.append(f"Balance ${usd(balance)}")
    return "\n".join(lines)


def error_html(msg: str) -> str:
    return f"⚠ <b>Alert</b>\n{esc(msg)}"


def status_html(s: dict) -> str:
    """s = engine.status_snapshot()"""
    head = f"<b>Status</b> · {'PAUSED' if s['paused'] else 'RUNNING'}"
    rows = []
    if s.get("slug"):
        rows.append(("Round", esc(s["question"])))
        rows.append(("Sisa", f"{s['t_left']:.0f}s"
                             f" (window {s['win_end']}–{s['win_start']}s)"))
        if s.get("ptb"):
            rows.append(("PTB", f"{s['ptb']:,.2f} [{esc(s['ptb_source'])}]"))
            stable = f" (stabil {s['stable_for']:.0f}s)" if s.get("stable_for") else ""
            rows.append(("Live", f"{s['live']:,.2f} · lead {s['lead_bps']:+.1f} bps"
                                 f"{stable}"))
        if s.get("fav_ask") is not None:
            rows.append(("Favorit", f"{esc(s['fav_side'])} · ask {s['fav_ask']:.3f}"
                                    f" → eff {s['fav_eff']:.4f}"
                                    f" · spread {s['spread']}t"
                                    f" · depth {s['depth']:.0f} sh"))
    else:
        rows.append(("Round", "menunggu round berikutnya"))
    spot = (f"{esc(s['spot_source'])} · age {s['spot_age']:.1f}s"
            f" · vol {s['vol']:.2f} bps/√s")
    if s.get("divergence") is not None:
        spot += f" · Δ {s['divergence']:.1f} bps"
    rows.append(("Spot", spot))
    rows.append(("Feed", f"CLOB WS {'on' if s.get('ws_connected') else 'off'}"
                         f" · offset CL {s['cl_offset']:+.2f}"
                         f" ({s['offset_samples']}n)"
                         f" · clock {s['clock_off']:+.2f}s"))
    open_pos = s.get("open_positions") or []
    if open_pos:
        p = open_pos[0]
        rows.append(("Open", f"{arrow(p['side'])} {esc(p['side'])} {p['shares']:.1f} sh"
                             f" @ {p['avg_price']:.3f}"
                             f" · {esc(p['market_slug'])}"))
    tail = (f"Balance ${usd(s['balance'])} · equity ${usd(s['equity'])}"
            f" · hari ini {usd(s['day_pnl'], sign=True)}\n"
            f"Trades {s['closed']} closed · streak {s['loss_streak']}L"
            + (f" · pause {s['pause_rounds']} round" if s.get("pause_rounds") else ""))
    return "\n".join([head, code_block(kv(rows)), tail])


def trades_html(rows: list) -> str:
    if not rows:
        return "Belum ada trade."
    lines = ["<b>Trades — 10 terakhir</b>", "<code>",
             f"  {'round':<10} {'sd':<2} {'size':>5} @ {'avg':>5}"
             f" {'t':>4} {'lead':>5} {'pnl':>7}"]
    for r in rows[:10]:
        mark = {"WON": "W", "LOST": "L", "SOLD": "S", "OPEN": "O"}.get(r["status"], "·")
        side = "Up" if r["side"] == "Up" else "Dn"
        pnl = "—" if r["status"] == "OPEN" else f"{(r['pnl_net'] or 0):+.2f}"
        lines.append(
            f"{mark} {esc(r['market_slug'])[-10:]:<10}"
            f" {side:<2} {r['shares']:>5.1f} @ {r['avg_price']:>5.3f}"
            f" {r['t_left_s']:>4.0f} {r['lead_bps']:>+5.1f} {pnl:>7}")
    lines.append("</code>")
    lines.append("W/L/S/O = win/loss/exit/open · t = sisa detik saat entry"
                 " · lead bps · pnl USDC")
    return "\n".join(lines)


def rounds_html(rows: list) -> str:
    if not rows:
        return "Belum ada round tercatat."
    lines = ["<b>Rounds — 12 terakhir</b>", "<code>",
             f"  {'round':<10} {'win':<4} {'move':>6} {'kita':>4} {'pnl':>7}"]
    for r in rows[:12]:
        traded = bool(r.get("traded"))
        pnl_v = r.get("pnl") or 0
        if traded:
            mark = "W" if pnl_v > 0 else ("L" if pnl_v < 0 else "T")
        else:
            mark = "·"
        move = ""
        if r.get("ptb") and r.get("close_chainlink"):
            move = f"{(r['close_chainlink'] - r['ptb']) / r['ptb'] * 1e4:+.1f}"
        winner = (r.get("winner") or "?")[:4]
        ours = "—"
        if traded and r.get("our_side"):
            ours = "Up" if r["our_side"] == "Up" else "Dn"
        pnl = f"{pnl_v:+.2f}" if traded else ""
        note = ""
        if not traded and r.get("skipped_reason"):
            note = "  " + r["skipped_reason"][:26]
        lines.append(
            f"{mark} {esc(r['slug'])[-10:]:<10} {winner:<4} {move:>6}"
            f" {ours:>4} {pnl:>7}{note}")
    lines.append("</code>")
    return "\n".join(lines)


def stats_html(st: dict, title: Optional[str]) -> str:
    pf = "∞" if st["profit_factor"] == float("inf") else f"{st['profit_factor']:.2f}"
    body = kv([
        ("Trades", f"{st['n']} (W{st['wins']}/L{st['losses']}/S{st['sold']})"),
        ("Win rate", f"{st['win_rate']}%"),
        ("Net PnL", f"{usd(st['net_pnl'], sign=True)} USDC"
                    f" (ROI {st['roi_pct']:+.2f}%)"),
        ("PF", f"{pf} · expectancy {st['expectancy']:+.3f}/trade"),
        ("Avg W / L", f"{usd(st['avg_win'], sign=True)} / {usd(st['avg_loss'], sign=True)}"),
        ("Best / worst", f"{usd(st['best'], sign=True)} / {usd(st['worst'], sign=True)}"),
        ("Biaya", f"fee {usd(st['total_fees'])} · slip {usd(st['total_slippage'])}"
                  f" · staked {usd(st['total_staked'])}"),
        ("Avg entry", f"{st['avg_entry']:.3f} (eff {st['avg_eff']:.4f})"
                      f" · t-{st['avg_t_left']:.0f}s"
                      f" · lead {st['avg_lead_bps']:.1f} bps"),
        ("Max DD", f"{usd(st['max_drawdown'])} ({st['max_drawdown_pct']:.2f}%)"),
        ("Streak", f"max W{st['max_win_streak']}/L{st['max_loss_streak']}"
                   f" · now {st['cur_streak']}{st['cur_streak_type']}"),
    ])
    if title:
        return f"<b>{esc(title)}</b>\n{code_block(body)}"
    return code_block(body)


def config_html(cfg, overrides: dict, settables: list | None = None) -> str:
    s, e, r = cfg.strategy, cfg.execution, cfg.risk
    lines = [
        "<b>Config efektif</b>",
        "<pre>",
        f"window      : {s.window_start_s}s → {s.window_end_s}s",
        f"min_lead    : {s.min_lead_bps} bps (stabil {s.lead_stable_s}s)",
        f"max_eff     : {s.max_eff_price}  (max_ask {s.max_ask})",
        f"side_agree  : on (arah lead harus = favorit pasar)",
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
        lines.append("Runtime override: " + ", ".join(
            f"<code>{esc(k)}={esc(v)}</code>" for k, v in overrides.items()))
    if settables:
        lines.append("Key tersedia: " + ", ".join(
            f"<code>{esc(k)}</code>" for k in settables))
    lines.append("Ubah: <code>/set min_lead_bps 6.5</code> · reset: "
                 "<code>/unset min_lead_bps</code>")
    return "\n".join(lines)


def book_html(snap: dict) -> str:
    sides = snap.get("sides") or {}
    if not sides:
        return "Book belum tersedia (menunggu round / WS terhubung)."
    ws = "terhubung" if snap.get("connected") else "terputus"
    body = []
    for side, s in sides.items():
        body.append(f"{side.upper():<5} age {s['age']}s"
                    f"  bid/ask {s['best_bid']}/{s['best_ask']}"
                    f"  mid {s['mid']:.3f}")
        asks = "  ".join(f"{p:.2f}×{sz:.0f}" for p, sz in s["asks"][:3])
        bids = "  ".join(f"{p:.2f}×{sz:.0f}" for p, sz in s["bids"][:3])
        body.append(f"  ask  {asks or '—'}")
        body.append(f"  bid  {bids or '—'}")
        if s.get("last_trade"):
            lt = s["last_trade"]
            body.append(f"  last {esc(str(lt.get('side')))}"
                        f" {esc(str(lt.get('price')))} × {esc(str(lt.get('size')))}")
        body.append("")
    while body and not body[-1]:
        body.pop()
    return f"<b>Orderbook</b> · WS {ws}\n{code_block(chr(10).join(body))}"


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