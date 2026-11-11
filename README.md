# 🎯 Polymarket BTC 5m Up/Down — Paper Trading Bot

Bot **paper trading** (simulasi, tanpa uang sungguhan) untuk market
[Polymarket](https://polymarket.com) *Bitcoin Up or Down* 5-menit, dengan strategi
**late-window momentum**, simulasi eksekusi realistis (fee resmi, slippage, latensi,
depth orderbook), integrasi **Telegram** (notifikasi + kontrol, format HTML), laporan
harian **Markdown**, dan **backtester** berbasis data riil.

> ⚠️ **PAPER MODE SAJA.** Bot ini tidak pernah mengirim order ke Polymarket —
> hanya membaca data publik. Untuk edukasi/riset; bukan nasihat keuangan.

---

## ✨ Fitur

| Area | Detail |
|---|---|
| 📡 Data live | Chainlink PTB via API Polymarket, **orderbook via CLOB WebSocket** (fallback REST), spot BTC via Binance WS (fallback REST; Bybit/Coinbase/OKX) |
| 🧠 Strategi | Aturan late-window sesuai spec (lihat bawah) + guard `side_agree` (arah lead harus = favorit pasar), flip exit, dan guard realisme opsional (stabilitas lead, spread, depth, divergence antar sumber, filter EV) |
| 💵 Eksekusi paper | Fee resmi Polymarket `shares × 0.07 × p × (1−p)`, book-walk + participation cap, adverse-move latency model, minimum order 5 shares |
| 🏁 Resolusi | Otomatis dari Chainlink (`close ≥ open → Up`) dan/atau Gamma (`outcomePrices`), background resolver bila tertunda |
| 📲 Telegram | Notifikasi entry/fill/resolusi/error (HTML) + perintah `/status /pnl /trades /pause /set …` |
| 📊 Persistensi | SQLite (posisi, round, equity) + laporan harian Markdown di `data/reports/` |
| 🛡 Risk | Daily loss limit, pause setelah loss-streak, max 1 posisi, stake tetap / % equity |
| 🔁 Backtest | `backtest.py` mereplay round historis riil (harga Chainlink + harga token CLOB) |
| ✅ Self-check | `selfcheck.py` — 92 test offline (fee vs tabel docs, fill sim, aturan strategi, flip exit, broker, store) |

---

## 🧠 Strategi (sesuai spec)

> Jangan sniping detik-detik awal atau lead-lag super cepat.
> Fokus di menit terakhir ketika lead sudah jelas.

Entry **hanya** di window sisa waktu **90 → 30 detik**, beli sisi yang unggul:

1. **Window** — `30s ≤ t_left ≤ 90s` (di luar itu: tidak ada order, hanya observasi)
2. **Lead** — `|spot − price-to-beat| / PTB ≥ min_lead_bps` (default **7 bps**, rentang spec 6–8)
3. **Harga tiket favorit (setelah fee)** — `ask + fee_per_share ≤ max_eff_price` (default **0.90**)
   - contoh nyata: ask `0.87` → fee/sh `0.07×0.87×0.13 = 0.0079` → eff `0.8779` ✅
   - ask `0.90` → eff `0.9063` ❌ (lewat cap) — break-even win-rate ≈ 90.6%
4. **Beli sisi unggul** (Up jika spot > PTB, sebaliknya Down) **dan harus searah
   favorit pasar** (`side_agree`: mid Up ≥ mid Down → hanya boleh beli Up).
   Jika arah lead ≠ favorit pasar, round di-skip — bot tidak pernah membeli
   underdog murah.

**Flip exit** (default **7 bps**, cermin aturan entry): setelah posisi terbuka,
jika lead berbalik ≥ `flip_exit_bps` melawan posisi (misal posisi Up, lead jadi
−7 bps), tiket dijual early alih-alih hold sampai resolusi — memotong loss penuh
saat momentum batal. Jendela exit `10s ≤ t_left ≤ window_start + 30s` (terlalu
mepet resolusi: book tipis, harga hampir final). Set `0` untuk hold-to-maturity.

Guard tambahan (opsional, default off kecuali disebut):
- **Stabilitas lead** ≥ `lead_stable_s` (default **0 = off**) — hindari masuk saat lead baru flicker
- **Kualitas book** — `max_spread_ticks` / `min_book_shares` (default 0 = off)
- **Kesehatan feed** — spot age ≤ 3s; umur orderbook ≤ `book_max_age_s` (10s,
  WS realtime biasanya ~0s); opsional divergence antar sumber exchange
  (default off — Binance BTCUSDT vs Coinbase BTC-USD wajar berbeda > 10 bps
  karena produknya beda)
- **EV filter opsional** (`use_vol_filter`, default off) — estimasi probabilitas model
  Brownian `p = Φ(lead / (σ·√t_left))` dengan σ real-time dari tick exchange;
  entry hanya jika `p − eff_price ≥ min_edge`

**Kenapa harga Chainlink, bukan Binance?** Market resolve pakai *Chainlink BTC/USD data
stream*. Bot memakai `openPrice` Chainlink sebagai PTB (sumber yang sama dengan UI
Polymarket), dan mengkalibrasi feed cepat exchange terhadap Chainlink
(offset EWMA `chainlink − exchange`) sehingga lead dihitung pada skala Chainlink.

**PTB cross-validation.** PTB diambil dari dua sumber dan saling dicek:
`crypto-price.openPrice` (sumber UI) vs titik pertama `price-history` pada
`timestamp == start`. Jika selisih > 5 bps, dipakai yang lebih dekat ke harga
live yang **segar**. `openPrice` di lingkungan tertentu bisa basi (beku, nilai
identik antar round) — jika terdeteksi identik dengan round sebelumnya, bot
langsung pakai `price-history`. Semua kejadian ini dicatat di `bot.log`
(`ptb_source` per round: `chainlink` / `chainlink_hist` / `spot_fallback`);
hanya kegagalan total (tidak ada PTB sama sekali) yang dikirim sebagai alert
Telegram.

---

## 🏗 Arsitektur & sumber data

```
                ┌────────────────────────────────────────────────┐
                │                   engine.py                    │
                │  discovery → PTB → monitor → entry → resolve   │
                └───────┬────────────┬───────────────┬───────────┘
        ┌───────────────┤            │               ├──────────────────┐
        ▼               ▼            ▼               ▼                  ▼
  polymarket.py   chainlink.py    spot.py        strategy.py        paper.py
  Gamma: event    crypto-price    Binance WS     aturan 90–30s,     broker paper:
  slug/tokens/    (PTB + close,   bookTicker +   lead ≥ 7bps,       book-walk, fee
  outcome; CLOB:  completed)      REST fallback  eff ≤ 0.90,        0.07·p·(1−p),
  /book /fee-rate                 + kalibrasi    side_agree,        slippage, latensi
  /prices-history                 offset Chainlink  flip exit          │
        │                                                            ▼
        ▼                                                       store.py (SQLite)
  data-api publik Polymarket                                    stats.py, format.py
                                                                     │
                                                            telegram_bot.py (HTML)
```

| Kebutuhan | Endpoint (publik, terverifikasi live) |
|---|---|
| Discovery market round kini/berikut | `GET gamma-api.polymarket.com/events?slug=btc-updown-5m-{start_epoch}` |
| Token Up/Down, outcome final | field `clobTokenIds`, `outcomes`, `outcomePrices`, `closed` |
| **Price-to-beat** + harga Chainlink live/final | `GET polymarket.com/api/crypto/crypto-price?symbol=btc&eventStartTime={start}` → `openPrice`, `closePrice`, `completed` |
| Path harga Chainlink (chart) | `GET polymarket.com/api/crypto/price-history?symbol=btc&eventStartTime={start}` |
| Orderbook real | `wss://ws-subscriptions-clob.polymarket.com/ws/market` (subscribe `{"type":"market","assets_ids":[…]}`) → events `book` / `price_change` / `last_trade_price`; fallback `GET clob.polymarket.com/book?token_id=…` |
| Fee | `GET clob.polymarket.com/fee-rate?token_id=…` + [docs resmi](https://docs.polymarket.com/trading/fees) |
| Riwayat harga token (backtest) | `GET clob.polymarket.com/prices-history?market={token}&startTs&endTs&fidelity=1` |
| Spot cepat | `wss://stream.binance.com:9443/ws/btcusdt@bookTicker` / REST Binance, Bybit, Coinbase, OKX |

Aturan resolusi (dari deskripsi market): **"Up" jika harga akhir ≥ harga awal**
(sama/seri = Up). Sumber: Chainlink BTC/USD data stream.

**Rate limit:** API situs `polymarket.com` agresif (429 setelah ~10 call cepat).
Bot menjaga jarak ≥ 6 detik per call (jadwal: PTB saat open, kalibrasi di
`T+120/165/210/240/265s`, resolusi setelah close). CLOB & Gamma lebih longgar.

---

## 💵 Realisme eksekusi (paper "like a real")

- **Fee taker resmi** (kategori Crypto, diverifikasi cocok dengan tabel docs):
  `fee = shares × 0.07 × p × (1−p)` — dihitung **per level** saat book-walk.
  Maker = 0 (bot tidak pasang limit order). Redeem pemenang = 0 fee.
- **Slippage** — order mensimulasikan *walk* pada orderbook CLOB asli:
  participation cap per level (default 15% size tampil); jika depth habis,
  sisa diisi pada level sintetis + penalti `exhausted_penalty_ticks`.
- **Latensi** — `latency_ms` (default 300 ms) dimodelkan sebagai *adverse-move
  lottery*: dengan probabilitas `adverse_move_prob` (default 50%) seluruh ladder
  bergeser `adverse_move_ticks` (1 tick = $0.01) melawan kita saat order tiba.
- **Batasan nyata** — minimum order 5 shares, tick 0.01, cap harga 0.99,
  saldo harus cukup (fee ikut memotong saldo).
- **Resolusi** — payout winner `shares × $1`; sumber dicatat
  (`chainlink`/`gamma`/`background`), ketidakcocokan antar sumber di-log.
- **RNG deterministik** per (market, side) — hasil simulasi bisa direproduksi.

---

## 🚀 Setup

```bash
# 1. Python 3.11+ (diuji 3.12, Windows/Linux)
cd up-down-beta
python -m pip install -r requirements.txt

# 2. (opsional) Telegram
copy .env.example .env        # Linux/macOS: cp .env.example .env
# isi TELEGRAM_BOT_TOKEN (dari @BotFather) dan TELEGRAM_CHAT_IDS (id chat kamu)
# kosongkan keduanya → bot tetap jalan headless (notifikasi ke log)

# 3. Verifikasi offline (92 test, tanpa jaringan)
python selfcheck.py

# 4. Jalankan
python main.py                       # paper trading terus-menerus
python main.py --rounds 6            # berhenti setelah 6 round (uji coba)
python main.py --no-telegram         # headless
python main.py --report              # tulis laporan harian lalu keluar
python main.py -v                    # debug log
```

Dapatkan `TELEGRAM_CHAT_IDS`: chat bot kamu lalu pakai bot `@userinfobot`,
atau biarkan kosong — chat pertama yang mengirim `/start` otomatis didaftarkan
(hanya jika `TELEGRAM_CHAT_IDS` kosong).

---

## 📲 Telegram

**Notifikasi (HTML):** header satu baris + blok data rata kolom (monospace),
tanpa emoji per-baris.

- ▲/▼ **ENTRY** — sisi, sisa waktu, lead bps, PTB → live, ask → harga efektif,
  fill, stake + fee/slip, saldo
- ✅/❌ **WIN / LOSS** (➖ **EXIT** untuk flip exit) — PTB → close (bps),
  payout, PnL net (fee & slippage dirinci), statistik hari ini + kumulatif
- ➖ **FLIP EXIT** — lead berbalik melawan posisi, dijual early
- ⚠ **Alert** — hanya masalah serius: PTB gagal total (fallback spot), round
  di-skip tanpa data, stake terlalu kecil untuk min order, error.
  (Warning rutin seperti fallback PTB / resolusi tertunda → `bot.log` saja)
- **Ringkasan harian** (default 00:00 UTC) + file Markdown

**Perintah:**

| Command | Fungsi |
|---|---|
| `/status` | Kondisi live: round, t_left, PTB, lead, book favorit, offset kalibrasi, feed |
| `/book` | Orderbook sisi favorit real-time (`book`/`price_change` + last trade) |
| `/pnl` | PnL hari ini & total, balance/equity |
| `/stats` | Statistik lengkap (WR, PF, expectancy, MaxDD, streak, fee & slippage total) |
| `/trades` | 10 posisi terakhir |
| `/positions` | Posisi terbuka (+ mark price) |
| `/rounds` | Hasil round-round terakhir (termasuk yang tidak di-trade + alasan skip) |
| `/pause` `/resume` | Hentikan/lanjutkan entry baru (observasi & resolusi tetap jalan) |
| `/set key value` | Ubah parameter runtime & simpan — cth `/set min_lead_bps 6.5`, `/set strategy.max_eff_price 0.88`, `/set stake_usdc 50` |
| `/unset key` | Reset parameter ke default (hapus override runtime) |
| `/config` | Config efektif + daftar key yang bisa di-`/set` |
| `/report` | Tulis & kirim laporan harian Markdown |
| `/equity` | Balance, equity, open value |

Semua command otomatis didaftarkan ke menu Telegram (`set_my_commands`) saat bot
start, sehingga muncul sebagai tombol `/` di klien.

---

## ⚙️ Konfigurasi

Semua di [`config.yaml`](config.yaml) (komentar lengkap). Hierarki:
`config.yaml` → `.env` → `data/runtime.json` (via `/set`, persist antar restart).

Parameter strategi utama:

| Key | Default | Arti |
|---|---|---|
| `strategy.window_start_s` / `window_end_s` | 90 / 30 | batas window entry (detik tersisa) |
| `strategy.min_lead_bps` | 7.0 | lead minimum (spec: 6–8) |
| `strategy.flip_exit_bps` | 7.0 | jual early jika lead balik ≤ −X bps melawan posisi (0 = hold to maturity) |
| `strategy.lead_stable_s` | 0 (off) | lead harus stabil selama N detik |
| `strategy.max_eff_price` | 0.90 | cap harga **setelah fee** |
| `strategy.max_ask` | 0.90 | cap ask mentah |
| `strategy.use_vol_filter` | false | filter EV Brownian opsional |
| `execution.stake_usdc` / `stake_mode` | 5 / fixed | ukuran posisi — **minimal ~4.50** agar min order 5 share favorit terisi (`equity_pct` = % equity) |
| `risk.daily_loss_limit_usdc` | 150 | stop harian |
| `risk.loss_streak_pause` / `pause_rounds` | 4 / 6 | pause setelah loss beruntun |

---

## 🔁 Backtest (data riil)

```bash
python backtest.py --rounds 40                          # 40 trade terakhir yang lolos sinyal
python backtest.py --rounds 100 --min-lead 8 --max-eff 0.88 --stake 50
python backtest.py --rounds 30 --ago-hours 24 --entry-offset 45
```

Per round historis diambil: outcome final (Gamma), `openPrice/closePrice`
(Chainlink), path harga dalam window (`price-history`), dan harga token kedua
sisi sekitar waktu entry (CLOB `prices-history`) → strategi disimulasikan dengan
fee & slippage-ticks yang sama. Hasil: ringkasan statistik + CSV di
`data/reports/`. Data di-cache (`data/backtest_cache.json`) — re-run instan.

**Keterbatasan (jujur):**
- `prices-history` CLOB fidelity ~1 menit dan bersifat mid-ish → harga entry
  adalah **proksi** (ditambah `--slippage-ticks`, default 1 tick + half-spread).
- **Flip exit tidak dimodelkan** — backtest hold-to-maturity, jadi hasil live
  dengan `flip_exit_bps > 0` bisa berbeda (umumnya loss per trade lebih kecil,
  win rate sedikit lebih rendah).
- Stabilitas lead (`lead_stable_s`) tak bisa diverifikasi dari data historis →
  backtest bisa sedikit **lebih optimis** daripada live.
- API situs rate-limited: ±13 detik per round baru (cache membantu).

---

## 📁 Struktur

```
up-down-beta/
├── main.py               # entrypoint bot live (paper)
├── backtest.py           # backtest data riil
├── selfcheck.py          # 92 test offline
├── config.yaml           # semua parameter
├── .env.example          # secrets (Telegram)
├── deploy/
│   └── updown-bot.service  # unit systemd utk VPS
├── updown/
│   ├── engine.py         # orkestrator round (concurrent per round)
│   ├── strategy.py       # aturan late-window + side_agree + flip exit
│   ├── paper.py          # broker paper: fee/slippage/latensi/book-walk
│   ├── polymarket.py     # klien Gamma + CLOB (throttle/retry per host)
│   ├── chainlink.py      # PTB & harga Chainlink (crypto-price, anti-429)
│   ├── spot.py           # Binance WS + REST multi-sumber + kalibrasi offset
│   ├── store.py          # SQLite (posisi/round/equity/kv)
│   ├── stats.py          # metrik performa
│   ├── format.py         # template HTML Telegram + laporan Markdown
│   ├── telegram_bot.py   # perintah & notifikasi
│   ├── config.py         # yaml + env + runtime override
│   └── models.py         # dataclass (Market, Book, Position, …)
└── data/                 # sqlite, logs, reports, cache (dibuat otomatis)
```

---

## 🖥 Deploy di VPS (Linux, systemd)

WebSocket keluar (port 443) harus dibuka oleh firewall. Pastikan **jam sistem
tersinkron** (`systemd-timesyncd` / `chrony`) — timing window bergantung jam
yang benar (bot juga menyinkronkan ke clock server CLOB).

### Install

```bash
# 1. Dependensi & user khusus
sudo apt-get update && sudo apt-get install -y python3.12 python3.12-venv git
sudo useradd -r -s /usr/sbin/nologin -m updown

# 2. Clone & milik user updown
sudo git clone https://github.com/aziz09iz/up-down-polymarket /opt/up-down-polymarket
sudo chown -R updown:updown /opt/up-down-polymarket
cd /opt/up-down-polymarket

# 3. venv + deps
sudo -u updown python3.12 -m venv .venv
sudo -u updown .venv/bin/pip install -r requirements.txt

# 4. Konfigurasi
sudo -u updown cp .env.example .env
sudo -u updown nano .env          # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS, STAKE_USDC

# 5. Verifikasi
sudo -u updown .venv/bin/python selfcheck.py

# 6. Install & start service
sudo cp deploy/updown-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now updown-bot
```

### Operasional (systemctl)

```bash
sudo systemctl status updown-bot          # cek status service
sudo systemctl restart updown-bot         # restart
sudo systemctl stop updown-bot            # stop
sudo journalctl -u updown-bot -f          # log live (jurnal systemd)
tail -f /opt/up-down-polymarket/data/logs/bot.log   # log aplikasi

# update ke commit terbaru
cd /opt/up-down-polymarket
sudo -u updown git pull
sudo -u updown .venv/bin/pip install -r requirements.txt   # bila deps berubah
sudo systemctl restart updown-bot
```

### Catatan feed di VPS

- **Binance WS** biasanya terhubung normal dari VPS (SG/US/JP). Dari jaringan
  rumah/kantor tertentu bisa di-refuse (geo) — bot otomatis fallback REST 1s,
  atau set sumber lain: `SPOT_SOURCE=bybit` (atau `/set spot_primary bybit`).
- **Fee**: tanggal pasar ini, event `last_trade_price` menunjukkan `fee_rate_bps: "0"`
  di sebagian perioda. Default tetap `fee_rate=0.07` (dokumen resmi, konservatif);
  jika fill aktual nol-fee, set `FEE_RATE=0` lewat `.env` atau `/set fee_rate 0`.

---

## 🧯 Troubleshooting

| Gejala | Penyebab umum | Solusi |
|---|---|---|
| `binance WS error … refused` berulang | Port 9443 diblokir firewall/jaringan/geo | Otomatis fallback REST 1s; atau set `feeds.spot_primary: bybit` / `okx` |
| `429 from polymarket.com` | API situs rate-limited (jangan dipolling cepat) | Bot sudah backoff + jeda ≥6s/call; jangan jalankan backtest berbarengan dengan bot live |
| Skip `book: orderbook sisi favorit tidak tersedia` | Sisi ask book kosong/tipis (likuiditas sepi) | Normal — bot memang tak bisa jadi taker tanpa ask; tunggu round lebih ramai |
| Skip `book_fresh` | Fetch book gagal beberapa detik | Periksa koneksi ke `clob.polymarket.com` |
| `Resolusi … tertunda` (di log) | Gamma/Chainlink belum mempublikasikan hasil | Normal — background resolver menyelesaikan & mengirim WIN/LOSS kemudian (hingga ~4 menit setelah stop; sisanya di start berikutnya) |
| Warning PTB `sumber beda Xbps` (di log) | `openPrice` basi/telat vs `price-history` | Bot otomatis pilih sumber yang benar (deteksi basi: nilai identik antar round); tidak dikirim ke Telegram |
| Posisi ditutup `➖ FLIP EXIT` | Lead berbalik ≥ `flip_exit_bps` melawan posisi | Perilaku normal strategi; matikan dengan `/set flip_exit_bps 0` |
| Tidak ada entry sama sekali | Lead tak pernah ≥ threshold / harga favorit selalu > cap | Cek `/rounds` (alasan skip per round), longgarkan via `/set min_lead_bps 6` atau `/set strategy.max_eff_price 0.92` |

---

## ⚠️ Disclaimer & catatan

- Ini **riset/edukasi**. Paper ≠ live: fills riil bisa lebih buruk (antrian,
  sebagian fill, book bergerak), dan strategi lead-based punya risiko basis
  antara exchange spot vs Chainlink.
- Baca ToS Polymarket & regulasi lokal sebelum mempertimbangkan trading riil.
- Bot hanya memakai **endpoint publik read-only**; tidak ada private key,
  tidak ada order sungguhan.
- Clock: bot menyinkronkan jam ke timestamp server CLOB (`clock_sync`);
  pastikan jam sistem tidak melenceng jauh.
