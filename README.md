# polybot — Polymarket short-term crypto bot

A paper-first trading bot for Polymarket's **short-term crypto up/down markets**
(BTC / ETH / SOL …, 5-minute and 15-minute candles). It pulls a live spot price
from major exchanges, computes a model fair value for "will the candle close
up?", and trades against the CLOB order book only when the book price differs
from fair by more than a configurable edge — with Kelly-based sizing and hard
risk limits.

> ⚠️ **Risk warning.** This software places real financial bets when run in live
> mode. Short-term crypto markets are fast, noisy, and can move against you
> instantly. The included model is a *starting point*, not a proven edge. Run in
> the default **paper mode** until you understand its behaviour, start with tiny
> size, and never risk money you can't afford to lose. You are solely
> responsible for compliance with Polymarket's terms and the laws in your
> jurisdiction.

## How it works

```
 Gamma API ──► discover BTC/ETH "up or down" 5m & 15m markets
 Exchange  ──► live spot + candle-open + realized-vol estimate
                         │
                         ▼
   Strategy (momentum):  fair_up = Φ( ln(spot/open) / (σ·√t_remaining) )
                         │   compare to CLOB best ask, require edge ≥ min_edge
                         ▼
   RiskManager: fractional-Kelly sizing + exposure / loss / count limits
                         │
                         ▼
   Executor:  PaperExecutor (simulated, default)  │  LiveExecutor (real CLOB orders)
```

The fair-value model treats the remaining price path as a zero-drift lognormal
walk. With log-return so far `r = ln(spot/open)` and remaining volatility
`σ_rem = σ_persec · √(seconds_left)`, the probability the candle closes up is
`Φ(r / σ_rem)`. As the candle nears close, `σ_rem → 0` and the estimate
collapses toward 0 or 1 — so the bot's edge comes from spotting markets the book
hasn't repriced to match the realized move yet.

## Project layout

```
run.py                 CLI: run | discover | setup
config.example.yaml    all tunables (copy to config.yaml)
.env.example           secrets (copy to .env)
polybot/
  config.py            YAML + .env loading
  pricefeed.py         Binance/Coinbase/Kraken spot, candle-open, vol estimate
  gamma.py             market discovery + parsing
  strategy.py          Strategy interface + MomentumStrategy (fair-value model)
  simulate.py          offline simulation / mini-backtester (no network/keys)
  dashboard.py         live streaming dashboard (real-time feed + equity curve)
  marketdata.py        REAL spot + OHLC candles (Binance/Coinbase) + health
  indicators.py        EMA, RSI, MACD, ATR, volume change, body %, structure
  patterns.py          engulfing, pin bar, inside bar, breakout, doji, momentum
  analysis.py          trend read + opportunity decision (trend+indicators+edge)
  risk.py              Kelly sizing + risk limits
  clob.py              py-clob-client wrapper (auth, books, orders)
  executor.py          PaperExecutor + LiveExecutor
  engine.py            main loop (with an optional observer hook for the UI)
  runner.py            thread-safe BotRunner + state that powers the web UI
app.py                 Streamlit browser dashboard (streamlit run app.py)
tests/                 unit tests for strategy, risk, market parsing
```

## Setup

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml      # tune to taste
cp .env.example .env                    # only needed for live trading
```

For **paper trading** you don't need any keys. For **live trading**, put the
private key of the wallet that controls your Polymarket account in `.env`, and
set `signature_type` + `funder` in `config.yaml`:

- `signature_type: 0` — plain EOA wallet (leave `funder` blank).
- `signature_type: 1` — email / Magic login. `funder` = your Polymarket proxy
  address (the one holding USDC on Polygon).
- `signature_type: 2` — browser wallet (MetaMask etc.) proxy.

Then make sure that account holds USDC.e on Polygon and has approved the CLOB
exchange (Polymarket's UI does this the first time you trade).

## Browser dashboard (Streamlit)

A point-and-click dashboard with a live equity curve, balance, entries, win
rate, recent trades, and the markets currently being scanned.

```bash
pip install -r requirements.txt     # includes streamlit + pandas
streamlit run app.py                 # opens http://localhost:8501
```

In the sidebar:

- **Mode** — *Paper (simulated)* runs an offline simulator and needs **no
  private key**; *Live (real markets)* connects to Polymarket.
  - In live mode, leave **"Execute REAL orders"** unchecked to read real
    markets and books while simulating fills (needs only network access).
    Check it to place real orders — this requires a funded
    `POLYMARKET_PRIVATE_KEY` in `.env`.
- **Parameters** — bankroll, min-edge, max position, and Kelly fraction.
- **Start / Stop** — the bot runs in a background thread; the page
  auto-refreshes about once per second.

### Live mode uses REAL market data

Selecting **Live (real markets)** switches the whole pipeline to real data:

- **Real prices** — live BTC & ETH spot from Binance/Coinbase, refreshed every
  poll (1–5s), with the last-updated time shown.
- **Real candles & trend** — 1m / 5m / 15m OHLC (pick the timeframe in the
  sidebar), with **EMA9, EMA21, RSI14, MACD, ATR, volume change, candle body %**
  and **higher-high / lower-low** structure, combined into a
  bullish / bearish / neutral trend.
- **Candlestick patterns** — engulfing, pin bar / wick rejection, inside bar,
  breakout, doji, momentum.
- **Opportunity scanner** — for each BTC/ETH 5m & 15m Polymarket market it
  compares the live trend + indicators against the YES/NO book price and shows
  **🟢 OPPORTUNITY** (trend + indicators + market agree and edge ≥ min-edge),
  **🟡 possible** (thin edge) or **⚪ no trade** (weak edge).
- **Trade lifecycle** — separate **Open trades** and **Closed trades** tables;
  every closed trade logs entry/close time, market, side, entry & exit price,
  PnL, win/loss, **reason for entry** (price, trend, indicators, pattern, edge,
  market) and **reason for close**.
- **API health** — green/red status for the spot and candle feeds.

**Safety (enforced):**

- Paper mode is the default and needs no key.
- Real orders require **both** Live mode **and** the "Execute REAL orders"
  checkbox **and** a funded `POLYMARKET_PRIVATE_KEY`.
- If the live feed fails, live mode **stops trading and shows a warning** — it
  never falls back to a simulated price. Simulated prices only ever appear in
  paper mode (and are labelled as such).

Dashboard panels:

| Panel | What it is |
|---|---|
| BTC / ETH (live) | real spot price + last-updated time (live mode) |
| Spot / Candle API | feed health (✅/❌ + source) |
| Balance | equity (bankroll + realized PnL) |
| Total entries / Win rate | trades taken / settled win rate |
| Equity curve | equity over time |
| Indicators | EMA/RSI/MACD/ATR/volume/body/structure per symbol |
| Patterns & structure | detected candlestick patterns + market structure |
| Opportunity scanner & log | per-market decision with full reason |
| Open / Closed trades | live lifecycle with entry & close reasons |

> Paper/simulated PnL is illustrative (the model matches the synthetic
> generator by construction) — not a forward-return estimate. See the risk
> warning at the top.

Run it headless / on a server with
`streamlit run app.py --server.headless true --server.port 8501`.

## Usage

```bash
# List the short-term crypto markets the bot would consider right now
python run.py discover

# Live dashboard — watch the engine trade in real time with a streaming feed,
# stats panel, and ASCII equity curve. Offline (simulated feed); no keys needed.
python run.py dashboard --seconds 60

# Offline simulation / mini-backtest — drives the REAL strategy+risk+executor
# over synthetic candles. No network or keys needed; great first run.
python run.py simulate --n 500 --seed 7

# Run in paper mode (default — no real orders, journals to state/)
python run.py run

# Check live auth + USDC balance/allowance
python run.py setup

# Run LIVE with real funds (requires .env private key). Be careful.
python run.py run --live
```

Paper trades and settlements are written to `state/paper_journal.jsonl` so you
can review fills and PnL.

## Deployment

To run it 24/7 on your own machine, Docker, or a VPS (with systemd), see
**[DEPLOY.md](DEPLOY.md)** — it covers local setup, `docker compose`, a hardened
systemd unit, USDC funding / signature-type setup, and safe go-live steps.

## Key configuration knobs

| Setting | Meaning |
|---|---|
| `symbols`, `durations_minutes` | which assets/candle lengths to trade |
| `min_seconds_to_resolution` / `max_…` | only enter inside this time-to-close window |
| `min_edge` | required fair-vs-ask edge before trading |
| `max_price` / `min_price` | price band you'll buy in |
| `vol_floor_annual` / `vol_ceiling_annual` | clamps on the vol estimate |
| `bankroll_usd`, `kelly_fraction` | sizing base + fraction of full Kelly |
| `max_position_usd`, `max_total_exposure_usd` | notional caps |
| `daily_loss_limit_usd`, `max_consecutive_losses` | kill-switches that halt new entries |

## Writing your own strategy

Implement the `Strategy` interface in `polybot/strategy.py`:

```python
class MyStrategy(Strategy):
    name = "mine"
    def evaluate(self, market, candle_open, spot, vol_per_sec, up_quote, down_quote, now):
        ...  # return a Signal or None
```

then register it in `build_strategy()` and set `strategy: mine` in config.

## Testing

```bash
python -m pytest -q
```

The suite covers the fair-value math, Kelly sizing, risk limits, and Gamma
market parsing — no network or keys required.

## Limitations & ideas

- REST polling (not websockets) — fine for 5/15m horizons; add WS for tighter loops.
- Live settlement/PnL relies on Polymarket's on-chain resolution; the bot only
  records entries. A position-reconciliation step is a good next addition.
- The momentum model assumes zero drift and lognormal returns; consider adding
  funding/skew, a maker (resting-order) mode, and a historical backtester.
