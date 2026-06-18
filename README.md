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

```bash
pip install -r requirements.txt     # includes streamlit + pandas
streamlit run app.py                 # opens http://localhost:8501
```

### Exact commands

**Windows (PowerShell or Command Prompt):**

```bat
cd path\to\polymarket-bot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

(If `streamlit` isn't recognised, use `python -m streamlit run app.py`.)
Then open the URL it prints (default http://localhost:8501).

**macOS / Linux:**

```bash
cd path/to/polymarket-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

### Modes (sidebar)

| Mode | Data | Orders | Key needed |
|---|---|---|---|
| **🟢 Live Data Paper Trading** (default) | **REAL** BTC/ETH prices + **REAL** Polymarket books | paper (simulated fills) | **No** |
| 🧪 Simulator Test Mode | synthetic (dev/testing only) | paper | No |
| 🔴 Live Real Orders | REAL | **real money** | Yes (+ confirm checkbox) |

**Live Data Paper Trading is the default and uses real market data** — it never
shows simulator prices. The simulator lives only in its own test mode and is
clearly flagged (`simulator active: TRUE`). In live mode the debug panel always
shows `simulator active: FALSE`.

**Trade frequency.** The sidebar has a **Symbols** picker (BTC, ETH, SOL, XRP,
DOGE, BNB, **HYPE** — all on by default; more symbols ⇒ more markets ⇒ more
trades) and a **frequency preset**
(Balanced / Aggressive / **Max frequency ≈500/day**) that sets edge, confidence
and spread filters, plus **max concurrent trades** and **max exposure** inputs.
A live **pace meter** shows trades/hour and projected/day vs a 500 target, and a
diagnostics banner shows exactly which limit is throttling entries. Looser
filters ⇒ more trades but lower win rate — tune to taste.

What live mode does:

- **Real prices** — BTC & ETH spot from **Binance, falling back to Coinbase**,
  refreshed every 1–5s, showing the **exact source** and **last-updated time**.
  Prices are sanity-checked (rejected if outside a plausible range or if the two
  exchanges disagree by >2%) — it never silently substitutes a fake price.
- **Real candles** — 1m / 5m / 15m OHLC (timeframe selector), with the latest
  candle **close time** shown in the debug panel.
- **Indicators** — EMA9, EMA21, RSI14, MACD, ATR, volume change, candle body %,
  higher-high / lower-low structure → bullish / bearish / neutral trend.
- **Patterns** — engulfing, pin bar / wick rejection, inside bar, breakout, doji,
  momentum.
- **Real Polymarket markets** — BTC/ETH 5m & 15m: question, expiry, YES price,
  NO price, spread, liquidity, plus Polymarket API status.
- **Opportunity scanner** — combines trend + indicators + market price into
  **🟢 TRADE / 🟡 possible / ⚪ NO TRADE** with a **confidence score** and full
  reason. Tune **Edge threshold** and **Min confidence** in the sidebar.
- **Trade lifecycle** — **Open trades** show live mark price + unrealized PnL;
  **Closed trades** show entry & close time, entry price, close price, settle
  spot, PnL, win/loss, and the entry & close reasons.
- **Debug panel** — raw per-source spot responses, candle source + close time,
  Polymarket API status, and the `simulator active` flag.
- **Market Discovery Debug** — number of markets returned, accepted and filtered,
  the **exact reason each market was filtered**, the first 20 titles, all
  BTC/ETH-related markets found, discovered markets' expiry/YES/NO/liquidity, the
  available field names and a **raw market sample** (to verify the API schema),
  plus a **🔄 Refresh Markets** button. It auto-expands when live data is healthy
  but zero markets are found, so you can see immediately why.

### Trade management & early exits

> **Default: early exits are OFF — every trade is held to resolution.** Set
> `enable_early_exits: true` to re-enable the exits below.

By default positions are no longer just held to resolution — they can be closed
early by **selling the held token back into the book** to lock gains or cut
losses. Each open position is marked every loop to its current sellable price
(best bid) and checked against, in priority order:

| Exit | Config | Behaviour |
|---|---|---|
| Trailing stop | `trailing_stop_pct` | exit when price falls % from its peak (locks gains) |
| Time exit | `time_exit_seconds` | exit N seconds before resolution |
| Confidence exit | `confidence_exit` | exit if trend/indicators flip against the side |
| Volatility exit | `volatility_exit` | exit if vol spikes against a losing position |

> Fixed take-profit and stop-loss were **removed by request**. The **trailing
> stop** locks profit dynamically instead: your *buy YES 0.50 → 0.90* scenario
> rides up, sets a trailing level (e.g. 15% below the 0.90 peak ≈ 0.765), and
> exits if it falls back to that level — capturing the move without capping it
> early. All exit knobs live in `config.yaml`; set a value to `0` to disable.

**Win/loss = prediction correctness, not just PnL.** A trade counts as a *win*
only if the underlying moved in the predicted direction (UP bet → spot above the
candle open; DOWN bet → below) at the moment it closed. A trade that booked a
small profit on a bounce while the prediction was actually wrong is recorded as
a **loss**. PnL is tracked separately, so the win rate reflects how often the
bot is *right*, not just green.

### Intelligence: entry quality & adaptive sizing

- **Spread / liquidity guard** (`max_spread`, `min_liquidity`) — skips markets
  whose book is too wide or thin to fill well; the scanner shows the skip reason.
- **Confidence-weighted sizing** (`confidence_sizing`) — scales position size by
  signal strength (between `confidence_sizing_min_mult` and `…_max_mult`).
- **RSI-extreme avoidance** (`avoid_rsi_extremes`) — won't buy UP into an
  overbought reading (`rsi_overbought`) or DOWN into oversold (`rsi_oversold`),
  avoiding exhaustion entries that mean-revert.

### Self-learning model

The bot learns from its own history. Every closed trade contributes one training
example — the entry's signals (trend, RSI, MACD, EMA, volume, body, pattern,
edge, time-left, oriented toward the bet) plus the label *was the prediction
correct* — to an online logistic-regression model (pure Python, no extra deps).

- It estimates **P(correct)** for each new setup, shown in the scanner's `model`
  column. By default it only **scales size** (`learning_size_weight`); it does
  **not** block trades. Set `learning_gate: true` to also skip low-probability
  setups — even then it keeps an exploration rate (`learning_explore_rate`) so it
  can never freeze itself by gating out all its own training data.
- It **cold-starts on rules only** for the first `learning_min_samples` trades,
  then activates.
- Weights + raw training rows persist to `state/model.json` and
  `state/trade_log.jsonl`, so learning survives restarts and keeps improving.
- The dashboard's **🧠 Self-learning model** panel shows training samples, the
  model's recent accuracy, and the **learned signal weights** (which indicators
  have actually predicted correct trades).

Tune in `config.yaml`: `learning_enabled`, `learning_min_samples`,
`learning_min_prob`, `learning_lr`, `learning_size_weight`. Delete the two
`state/` files to reset learning.

The dashboard shows, per open trade, the **mark price, unrealized PnL, and live
TP / SL / trailing levels**; closed trades show the **exit type and exit
reason**; and an **Exit performance** panel ranks exit methods by average PnL and
compares **early-exit vs hold-to-resolution** (it replays what each early-exited
market would have settled at).

> Early exits sell at the current book price, so realised PnL =
> `size × (exit_price − entry_price)`. In live-real-orders mode this is a real
> FAK sell order; in paper mode it's simulated at the same price.

**Daily stop-loss.** Set **Daily stop-loss (% of capital)** in the sidebar
(`daily_loss_limit_pct`, default **50%**). Once the day's realized loss reaches
that fraction of `bankroll_usd`, the bot **stops taking new trades for the rest
of the day** (open positions still resolve) and shows a 🛑 banner; it resets and
resumes at UTC midnight. Set it to 0 to disable. The diagnostics line shows how
much of the daily allowance is used.

**Safety (enforced):**

- Default mode is Live Data Paper Trading; **no private key required**.
- **Daily stop-loss** halts new entries after a −50%-of-capital day (configurable).
- Real orders require Live Real Orders mode **and** the confirmation checkbox
  **and** a funded `POLYMARKET_PRIVATE_KEY` — three explicit steps.
- If live data is unavailable, the bot **pauses and shows a warning**; it never
  falls back to a simulated price in live mode.

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
