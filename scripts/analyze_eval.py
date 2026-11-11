"""Analisis DB hasil run VPS (read-only). Pakai: python scripts/analyze_eval.py

Lokasi DB ditentukan oleh konstanta `_DEFAULT_DB` di bawah (bukan dari argv),
untuk memastikan tidak ada input tak-terpercaya yang menjadi argumen koneksi.
Semua pernyataan SQL adalah literal statis dengan placeholder `?` dan koneksi
dibuka read-only (query_only + uri=False).
"""
import re
import sys
import sqlite3
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from updown.stats import compute_stats  # noqa: E402

_ALLOWED_SUFFIX = (".db", ".sqlite", ".sqlite3")
_DB_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_DEFAULT_DB = "data/updown.db"


def _open_readonly(db: str) -> sqlite3.Connection:
    """Validate lalu buka sqlite read-only. Tolak karakter URI, suffix asing, atau
    nama file di luar pola [A-Za-z0-9._-]. Koneksi pakai uri=False (path murni)."""
    dbp = Path(db).resolve()
    if "?" in db or "#" in db:
        sys.exit("Path DB tidak boleh mengandung '?' atau '#' (bukan URI)")
    if dbp.suffix.lower() not in _ALLOWED_SUFFIX:
        sys.exit(f"Ekstensi DB tidak diizinkan: {dbp.suffix!r}")
    if not _DB_NAME_RE.fullmatch(dbp.name):
        sys.exit(f"Nama file DB tidak valid (hanya [A-Za-z0-9._-]): {dbp.name}")
    if not dbp.is_file():
        sys.exit(f"DB tidak ditemukan: {dbp}")
    con = sqlite3.connect(str(dbp), uri=False)
    con.execute("PRAGMA query_only = ON")
    con.row_factory = sqlite3.Row
    return con


def run(db: str) -> None:
    con = _open_readonly(db)

    pos = con.execute("SELECT * FROM positions ORDER BY id").fetchall()
    rounds = con.execute("SELECT * FROM rounds ORDER BY start_ts").fetchall()
    eq = con.execute("SELECT ts, equity FROM equity ORDER BY ts").fetchall()

    print("=" * 70)
    print(f"POSISI: total={len(pos)}")
    st = {"WON": 0, "LOST": 0, "SOLD": 0, "OPEN": 0}
    for r in pos:
        st[r["status"]] = st.get(r["status"], 0) + 1
    print(f"  per status: {st}")
    closed = [dict(r) for r in pos if r["status"] != "OPEN"]
    openp = [r for r in pos if r["status"] == "OPEN"]
    if closed:
        qq = compute_stats(closed)
        print(f"  closed={len(closed)}  win_rate={qq['win_rate']}%  net_pnl={qq['net_pnl']:+.2f}"
              f"  fees={qq['total_fees']:.2f}  slip={qq['total_slippage']:.2f}"
              f"  expectancy={qq['expectancy']:+.3f}  roi={qq['roi_pct']:+.2f}%")
        print(f"  avg_entry={qq['avg_entry']:.4f} eff={qq['avg_eff']:.4f}"
              f"  t_left={qq['avg_t_left']:.0f}s  lead={qq['avg_lead_bps']:.1f}bps"
              f"  maxDD={qq['max_drawdown']:.2f}")
    if openp:
        print(f"  WARNING OPEN positions: {len(openp)}")
    for side in ("Up", "Down"):
        rows = [dict(r) for r in closed if r["side"] == side]
        if rows:
            s = compute_stats(rows)
            print(f"  [{side}] n={s['n']} win_rate={s['win_rate']}% net={s['net_pnl']:+.2f}"
                  f" avg_lead={s['avg_lead_bps']:.1f}bps")
    print("-" * 70)
    print("TRADE DETAIL (10 terakhir):")
    for r in closed[-10:]:
        print(f"  {r['market_slug'][-12:]} {r['side']:4s} {r['shares']:6.1f}sh"
              f" @{r['avg_price']:.3f} (eff {r.get('eff_price') or 0:.4f})"
              f" t-{r['t_left_s']:.0f}s lead {r['lead_bps']:+.1f}"
              f" → {r['status']:4s} pnl {r['pnl_net']:+.2f} fee {r['fees']:.3f}"
              f" slip {r['slippage_cost']:.3f} [{r['resolution_source']}]")

    print("=" * 70)
    print(f"ROUNDS: total={len(rounds)}")
    traded = [r for r in rounds if r["traded"]]
    print(f"  ditrade={len(traded)}  dilewati={len(rounds) - len(traded)}")
    winners = Counter(r["winner"] for r in rounds if r["winner"])
    print(f"  winner distribution: {dict(winners)}")
    res_src = Counter(r["resolution_source"] for r in rounds if r["resolution_source"])
    print(f"  resolution_source: {dict(res_src)}")
    ptb_src = Counter(r["ptb_source"] for r in rounds if r["ptb_source"])
    print(f"  ptb_source: {dict(ptb_src)}")
    skip = Counter(r["skipped_reason"] for r in rounds if r["skipped_reason"])
    print("  skip reasons (top):")
    for k, v in skip.most_common(15):
        print(f"    {v:4d}  {k}")
    print("-" * 70)
    print("ROUND DETAIL (12 terakhir):")
    for r in rounds[-12:]:
        move = ""
        if r["ptb"] and r["close_chainlink"]:
            move = f" {(r['close_chainlink'] - r['ptb']) / r['ptb'] * 1e4:+.1f}bps"
        trd = ""
        if r["traded"]:
            trd = f" TRADED {r['our_side'] or ''} pnl {r['pnl']:+.2f}"
        print(f"  {r['slug'][-12:]} → {r['winner'] or '?':4s}{move}"
              f" ptb={r['ptb'] or 0:,.2f} src={r['ptb_source'] or '-'}{trd}")

    print("=" * 70)
    if eq:
        print(f"EQUITY: {len(eq)} snapshot")
        print(f"  awal={eq[0][1]:.2f}  akhir={eq[-1][1]:.2f}"
              f"  min={min(e[1] for e in eq):.2f}  max={max(e[1] for e in eq):.2f}")
    con.close()


if __name__ == "__main__":
    run(_DEFAULT_DB)