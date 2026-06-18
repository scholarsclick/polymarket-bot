"""Configuration loading: YAML file + environment secrets."""

from __future__ import annotations

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
    symbols: List[str] = field(default_factory=lambda: ["BTC", "ETH"])
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
    max_total_exposure_usd: float = 100.0
    max_open_positions: int = 6
    max_trades_per_market: int = 1
    daily_loss_limit_usd: float = 50.0
    max_consecutive_losses: int = 5

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
        raise ValueError(f"Unknown config keys in {chosen}: {sorted(unknown)}")

    cfg = Config(**{k: v for k, v in data.items() if k in allowed})

    # secrets strictly from environment
    cfg.private_key = os.environ.get("POLYMARKET_PRIVATE_KEY") or None
    cfg.api_key = os.environ.get("POLYMARKET_API_KEY") or None
    cfg.api_secret = os.environ.get("POLYMARKET_API_SECRET") or None
    cfg.api_passphrase = os.environ.get("POLYMARKET_API_PASSPHRASE") or None

    return cfg
