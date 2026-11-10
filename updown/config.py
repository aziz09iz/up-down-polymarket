"""Configuration loading: config.yaml + .env overrides + runtime overrides.

Precedence (highest last): config.yaml defaults  ->  .env  ->  data/runtime.json
Runtime overrides are written by the Telegram `/set` command and survive restarts.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

try:  # optional, keeps the bot runnable without python-dotenv
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_a, **_k):  # type: ignore
        return False

log = logging.getLogger("updown.config")

ROOT = Path(__file__).resolve().parent.parent


# ────────────────────────────── dataclasses ──────────────────────────────

@dataclass
class PaperCfg:
    start_balance: float = 1000.0
    data_dir: str = "data"


@dataclass
class MarketCfg:
    series_slug: str = "btc-updown-5m"
    symbol: str = "btc"
    tick_size: float = 0.01
    min_order_shares: float = 5.0
    fee_rate: float = 0.07


@dataclass
class FeedsCfg:
    spot_primary: str = "binance"
    spot_ws: bool = True
    spot_poll_sec: float = 1.0
    spot_secondary: str = "coinbase"
    spot_secondary_poll_sec: float = 3.0
    max_divergence_bps: float = 0.0   # 0 = off (exchange beda produk wajar selisih)
    chainlink_poll_sec: float = 20.0
    book_poll_sec: float = 1.5
    book_idle_poll_sec: float = 6.0
    clock_sync: bool = True


@dataclass
class StrategyCfg:
    window_start_s: int = 90
    window_end_s: int = 30
    min_lead_bps: float = 7.0
    lead_stable_s: float = 4.0
    max_eff_price: float = 0.90
    max_ask: float = 0.90
    max_spread_ticks: int = 3
    min_book_shares: float = 150.0
    book_max_age_s: float = 6.0
    use_vol_filter: bool = False
    vol_default_bps_sqrt_s: float = 0.75
    vol_min_bps_sqrt_s: float = 0.30
    vol_max_bps_sqrt_s: float = 3.00
    min_edge: float = 0.005
    flip_exit_bps: float = 0.0


@dataclass
class ExecutionCfg:
    latency_ms: int = 300
    adverse_move_prob: float = 0.5
    adverse_move_ticks: int = 1
    participation: float = 0.15
    exhausted_penalty_ticks: int = 2
    stake_mode: str = "fixed"          # fixed | equity_pct
    stake_usdc: float = 25.0
    stake_pct: float = 2.5
    max_stake_usdc: float = 100.0


@dataclass
class RiskCfg:
    daily_loss_limit_usdc: float = 150.0
    loss_streak_pause: int = 4
    pause_rounds: int = 6
    max_open_positions: int = 1


@dataclass
class TelegramCfg:
    enabled: str = "auto"              # auto | true | false
    notify_entry: bool = True
    notify_fill: bool = True
    notify_resolution: bool = True
    notify_errors: bool = True
    notify_skips: bool = False
    summary_utc_hour: int = 0


@dataclass
class Secrets:
    telegram_bot_token: str = ""
    telegram_chat_ids: list = field(default_factory=list)
    log_level: str = "INFO"


@dataclass
class Config:
    paper: PaperCfg = field(default_factory=PaperCfg)
    market: MarketCfg = field(default_factory=MarketCfg)
    feeds: FeedsCfg = field(default_factory=FeedsCfg)
    strategy: StrategyCfg = field(default_factory=StrategyCfg)
    execution: ExecutionCfg = field(default_factory=ExecutionCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    telegram: TelegramCfg = field(default_factory=TelegramCfg)
    secrets: Secrets = field(default_factory=Secrets)

    # paths (filled in load())
    root: Path = ROOT
    data_dir: Path = ROOT / "data"
    runtime_path: Path = ROOT / "data" / "runtime.json"

    # live clock offset (polymarket_ts - local_ts), maintained by feeds
    clock_offset_s: float = 0.0

    def now(self) -> float:
        """Clock-synced current time (epoch seconds)."""
        import time
        return time.time() + self.clock_offset_s


# ────────────────────────────── loading ──────────────────────────────

def _apply_dict(obj: Any, data: dict) -> None:
    """Recursively apply a plain dict onto a dataclass tree, ignoring unknown keys."""
    if not is_dataclass(obj) or not isinstance(data, dict):
        return
    valid = {f.name for f in fields(obj)}
    for k, v in data.items():
        if k not in valid:
            log.warning("config: unknown key %r ignored", k)
            continue
        cur = getattr(obj, k)
        if is_dataclass(cur) and isinstance(v, dict):
            _apply_dict(cur, v)
        else:
            setattr(obj, k, v)


def _coerce(target: Any, raw: str) -> Any:
    """Coerce a string from env/runtime to the type of `target`."""
    if isinstance(target, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(target, int) and not isinstance(target, bool):
        return int(float(raw))
    if isinstance(target, float):
        return float(raw)
    if isinstance(target, list):
        return [s.strip() for s in raw.split(",") if s.strip()]
    return raw


# flat env/runtime key -> (section, field)
_FLAT_KEYS = {
    "START_BALANCE": ("paper", "start_balance"),
    "SPOT_SOURCE": ("feeds", "spot_primary"),
    "STAKE_USDC": ("execution", "stake_usdc"),
    "STAKE_MODE": ("execution", "stake_mode"),
    "MIN_LEAD_BPS": ("strategy", "min_lead_bps"),
    "MAX_EFF_PRICE": ("strategy", "max_eff_price"),
    "MAX_ASK": ("strategy", "max_ask"),
    "WINDOW_START_S": ("strategy", "window_start_s"),
    "WINDOW_END_S": ("strategy", "window_end_s"),
    "LEAD_STABLE_S": ("strategy", "lead_stable_s"),
    "MAX_SPREAD_TICKS": ("strategy", "max_spread_ticks"),
    "MIN_BOOK_SHARES": ("strategy", "min_book_shares"),
    "USE_VOL_FILTER": ("strategy", "use_vol_filter"),
    "MIN_EDGE": ("strategy", "min_edge"),
    "FLIP_EXIT_BPS": ("strategy", "flip_exit_bps"),
    "FEE_RATE": ("market", "fee_rate"),
    "STAKE_PCT": ("execution", "stake_pct"),
    "MAX_STAKE_USDC": ("execution", "max_stake_usdc"),
    "ADVERSE_MOVE_PROB": ("execution", "adverse_move_prob"),
    "PARTICIPATION": ("execution", "participation"),
    "LATENCY_MS": ("execution", "latency_ms"),
    "DAILY_LOSS_LIMIT": ("risk", "daily_loss_limit_usdc"),
    "LOSS_STREAK_PAUSE": ("risk", "loss_streak_pause"),
    "PAUSE_ROUNDS": ("risk", "pause_rounds"),
    "NOTIFY_SKIPS": ("telegram", "notify_skips"),
    "NOTIFY_ENTRY": ("telegram", "notify_entry"),
    "NOTIFY_RESOLUTION": ("telegram", "notify_resolution"),
    "CHAINLINK_POLL_SEC": ("feeds", "chainlink_poll_sec"),
    "BOOK_POLL_SEC": ("feeds", "book_poll_sec"),
    "LOG_LEVEL": ("secrets", "log_level"),
    "TELEGRAM_BOT_TOKEN": ("secrets", "telegram_bot_token"),
    "TELEGRAM_CHAT_IDS": ("secrets", "telegram_chat_ids"),
}


def _apply_flat(obj: Any, key: str, raw: Any) -> bool:
    """Apply a flat override like 'strategy.min_lead_bps', 'min_lead_bps' or
    an env-style name ('MIN_LEAD_BPS'). Matching is case-insensitive."""
    ku = str(key).upper()
    if ku in _FLAT_KEYS:
        section, fname = _FLAT_KEYS[ku]
        sub = getattr(obj, section)
        cur = getattr(sub, fname)
        setattr(sub, fname, raw if not isinstance(raw, str) else _coerce(cur, raw))
        return True
    if "." in key:
        section, _, fname = key.partition(".")
        sub = getattr(obj, section.strip().lower(), None)
        if sub is None:
            sub = getattr(obj, section.strip(), None)
        if sub is not None:
            valid = {f.name for f in fields(sub)} if is_dataclass(sub) else set()
            match = fname if fname in valid else next(
                (f for f in valid if f.lower() == fname.strip().lower()), None)
            if match:
                setattr(sub, match,
                        _coerce(getattr(sub, match), str(raw)))
                return True
    return False


def load(config_path: str | Path | None = None, env_file: str | Path | None = None) -> Config:
    cfg = Config()
    cfg.root = ROOT
    load_dotenv(env_file or ROOT / ".env")

    # 1) yaml
    path = Path(config_path) if config_path else ROOT / "config.yaml"
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        for section in ("paper", "market", "feeds", "strategy", "execution", "risk", "telegram"):
            if section in data:
                _apply_dict(getattr(cfg, section), data[section])
        if "secrets" in data:
            _apply_dict(cfg.secrets, data["secrets"])
    else:
        log.warning("config file %s not found, using defaults", path)

    # 2) env vars
    for env_name in _FLAT_KEYS:
        val = os.environ.get(env_name)
        if val not in (None, ""):
            _apply_flat(cfg, env_name, val)

    # 3) paths
    cfg.data_dir = Path(cfg.paper.data_dir)
    if not cfg.data_dir.is_absolute():
        cfg.data_dir = ROOT / cfg.data_dir
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "logs").mkdir(exist_ok=True)
    (cfg.data_dir / "reports").mkdir(exist_ok=True)
    cfg.runtime_path = cfg.data_dir / "runtime.json"

    # 4) runtime overrides (written by /set)
    if cfg.runtime_path.exists():
        try:
            rt = json.loads(cfg.runtime_path.read_text(encoding="utf-8"))
            for k, v in rt.items():
                if not _apply_flat(cfg, k, v):
                    log.warning("runtime override %r unknown, ignored", k)
        except Exception as exc:  # noqa: BLE001
            log.error("runtime.json unreadable: %s", exc)

    if not cfg.secrets.telegram_bot_token:
        cfg.secrets.telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not cfg.secrets.telegram_chat_ids:
        raw = os.environ.get("TELEGRAM_CHAT_IDS", "")
        cfg.secrets.telegram_chat_ids = [s.strip() for s in raw.split(",") if s.strip()]
    return cfg


def save_runtime(cfg: Config, key: str, value: str) -> bool:
    """Persist a runtime override (flat key) and apply it immediately."""
    if not _apply_flat(cfg, key, value):
        return False
    rt: dict = {}
    if cfg.runtime_path.exists():
        try:
            rt = json.loads(cfg.runtime_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            rt = {}
    rt[key] = value
    cfg.runtime_path.write_text(json.dumps(rt, indent=2), encoding="utf-8")
    return True


def runtime_overrides(cfg: Config) -> dict:
    if cfg.runtime_path.exists():
        try:
            return json.loads(cfg.runtime_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _lookup_target(cfg: Config, key: str):
    """Resolve 'section.field' or a flat env-style key → (sub, field_name)."""
    ku = str(key).upper()
    if ku in _FLAT_KEYS:
        section, fname = _FLAT_KEYS[ku]
        return getattr(cfg, section), fname
    if "." in key:
        section, _, fname = key.partition(".")
        for name in (section.strip().lower(), section.strip()):
            if hasattr(cfg, name):
                sub = getattr(cfg, name)
                if is_dataclass(sub):
                    valid = {f.name for f in fields(sub)}
                    match = fname if fname in valid else next(
                        (f for f in valid if f.lower() == fname.strip().lower()), None)
                    if match:
                        return sub, match
    return None, None


def unset_runtime(cfg: Config, key: str) -> bool:
    """Hapus runtime override & kembalikan ke default (config.yaml/env)."""
    rt = runtime_overrides(cfg)
    found = key in rt
    if found:
        del rt[key]
        cfg.runtime_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.runtime_path.write_text(json.dumps(rt, indent=2), encoding="utf-8")
    target, fname = _lookup_target(cfg, key)
    if target is None:
        return found
    dflt_cfg = Config()
    dflt_target, _ = _lookup_target(dflt_cfg, key)
    if dflt_target is not None:
        setattr(target, fname, getattr(dflt_target, fname))
    return found or True
