"""Configuration loading: YAML file + environment secrets."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional

import yaml
from dotenv import load_dotenv


@dataclass
class Config:
    # connection
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    chain_id: int = 137
    signature_type: int = 1
    funder: str = ""

    # mode
    dry_run: bool = True

    # markets
    symbols: List[str] = field(default_factory=lambda: ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"])
    durations_minutes: List[int] = field(default_factory=lambda: [5, 15])
    duration_tolerance_seconds: int = 90
    min_seconds_to_resolution: int = 25
    max_seconds_to_resolution: int = 900   # full 15m window visible/eligible

    # price feed
    price_sources: List[str] = field(default_factory=lambda: ["binance", "coinbase", "kraken"])
    vol_window_seconds: int = 300
    vol_floor_annual: float = 0.40
    vol_ceiling_annual: float = 2.50

    # strategy
    strategy: str = "momentum"
    min_edge: float = 0.05
    max_price: float = 0.95
    min_price: float = 0.05
    taker_fee_bps: float = 0.0

    # risk
    bankroll_usd: float = 200.0
    kelly_fraction: float = 0.25
    max_position_usd: float = 25.0
    max_total_exposure_usd: float = 0.0  # 0 = unlimited (daily stop-loss is the money cap)
    max_open_positions: int = 0          # 0 = unlimited; exposure/daily-stop are the real caps
    max_trades_per_market: int = 1
    daily_loss_limit_usd: float = 50.0   # absolute daily loss halt (used if pct <= 0)
    daily_loss_limit_pct: float = 0.5    # halt for the day after losing this fraction of capital
    max_consecutive_losses: int = 5

    # exits / trade management — early exits are OFF: hold every trade to resolution
    enable_early_exits: bool = False
    trailing_stop_pct: float = 0.15      # exit when price drops this far from peak (0 disables)
    time_exit_seconds: float = 30.0      # exit when this many seconds remain (0 disables)
    confidence_exit: bool = True         # exit if trend/indicators flip against the position
    exit_confidence_min: int = 2         # min opposing confidence to trigger a confidence exit
    volatility_exit: bool = True         # exit if volatility spikes against a losing position
    volatility_exit_mult: float = 3.0    # vol >= entry_vol * this triggers the volatility exit

    # mode: Normal (default) trades on any valid signal; Strict adds the
    # confidence gate + tighter quality filters.
    strict_mode: bool = False
    min_confidence_pct: float = 80.0     # confidence bar (only enforced in strict mode)
    # intelligence: entry-quality filters and adaptive sizing
    late_window_seconds: float = 30.0    # (strict) avoid the final N seconds…
    late_confidence_pct: float = 95.0    # …unless confidence is at least this high
    max_data_staleness_seconds: float = 30.0   # skip if candle/price feed is older than this
    max_spread: float = 0.20             # skip only EXTREMELY wide books (0 disables)
    min_liquidity: float = 10.0          # skip only near-empty books (0 disables)
    confidence_sizing: bool = True       # scale size by signal confidence
    confidence_sizing_min_mult: float = 0.5
    confidence_sizing_max_mult: float = 1.5
    avoid_rsi_extremes: bool = True      # don't buy into overbought / oversold exhaustion
    rsi_overbought: float = 80.0
    rsi_oversold: float = 20.0

    # self-learning model (learns which signals predict correct trades)
    learning_enabled: bool = True
    learning_min_samples: int = 25       # trades needed before the model influences anything
    learning_gate: bool = False          # if True, BLOCK low-prob entries (off = size-only)
    learning_min_prob: float = 0.45      # gate threshold (only used when learning_gate is True)
    learning_explore_rate: float = 0.2   # when gating, still take this fraction to keep learning
    learning_lr: float = 0.05            # SGD learning rate
    learning_size_weight: bool = True    # scale size by the model's P(correct)

    # loop
    poll_interval_seconds: float = 3.0
    market_refresh_seconds: float = 30.0
    log_level: str = "INFO"
    state_dir: str = "state"

    # secrets (from env, never YAML)
    private_key: Optional[str] = None
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    api_passphrase: Optional[str] = None

    @property
    def has_api_creds(self) -> bool:
        return bool(self.api_key and self.api_secret and self.api_passphrase)


def _known_fields() -> set:
    return {f for f in Config.__dataclass_fields__ if f not in {
        "private_key", "api_key", "api_secret", "api_passphrase"
    }}


def load_config(path: str = "config.yaml") -> Config:
    """Load YAML config (falling back to config.example.yaml) plus .env secrets."""
    load_dotenv()  # load .env into os.environ if present

    data: dict = {}
    chosen = None
    for candidate in (path, "config.example.yaml"):
        if candidate and os.path.exists(candidate):
            chosen = candidate
            break
    if chosen:
        with open(chosen, "r") as fh:
            data = yaml.safe_load(fh) or {}

    allowed = _known_fields()
    unknown = set(data) - allowed
    if unknown:
        # Ignore (don't crash) so removed/renamed keys in an existing
        # config.yaml stay loadable.
        logging.getLogger("polybot.config").warning(
            "Ignoring unknown config keys in %s: %s", chosen, sorted(unknown))

    cfg = Config(**{k: v for k, v in data.items() if k in allowed})

    # secrets strictly from environment
    cfg.private_key = os.environ.get("POLYMARKET_PRIVATE_KEY") or None
    cfg.api_key = os.environ.get("POLYMARKET_API_KEY") or None
    cfg.api_secret = os.environ.get("POLYMARKET_API_SECRET") or None
    cfg.api_passphrase = os.environ.get("POLYMARKET_API_PASSPHRASE") or None

    return cfg
