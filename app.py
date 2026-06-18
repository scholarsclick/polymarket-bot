"""Browser dashboard for polybot.

    streamlit run app.py

Paper mode runs a self-contained simulator (no network, no private key) — its
prices are clearly labelled simulated. Live mode uses REAL Binance/Coinbase
spot + candles and real Polymarket books: it shows live BTC/ETH prices,
indicators, patterns, an opportunity scanner and full trade lifecycle. If the
live data feed fails, live mode stops trading and shows a warning (never a fake
price).
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

from polybot.config import load_config
from polybot.runner import BotRunner

st.set_page_config(page_title="polybot — Polymarket crypto bot", layout="wide")

cfg = load_config()

if "runner" not in st.session_state:
    st.session_state.runner = None


# --------------------------------------------------------------------------
# Sidebar — controls
# --------------------------------------------------------------------------
with st.sidebar:
    st.title("⚡ polybot")
    st.caption("Polymarket short-term crypto bot (BTC/ETH 5m & 15m)")

    mode_label = st.radio(
        "Mode", ["Paper (simulated)", "Live (real markets)"],
        help="Paper runs an offline simulator (no key). Live uses real "
             "Binance/Coinbase data and real Polymarket markets.")
    is_live = mode_label.startswith("Live")

    timeframe = st.selectbox("Candle timeframe", ["1m", "5m", "15m"], index=1)

    execute_orders = False
    if is_live:
        execute_orders = st.checkbox(
            "Execute REAL orders (requires funded key)", value=False,
            help="Off = real data + simulated fills (network only). "
                 "On = place real orders with real funds.")
        st.caption(f"🔑 Private key in .env: {'✅ yes' if cfg.private_key else '❌ no'}")
        if execute_orders:
            st.warning("Real funds at risk in this mode.", icon="⚠️")
    else:
        st.caption("🔓 Paper mode needs no private key.")

    st.divider()
    st.subheader("Parameters")
    bankroll = st.number_input("Bankroll ($)", min_value=10.0, value=float(cfg.bankroll_usd), step=10.0)
    min_edge = st.slider("Min edge", 0.0, 0.20, float(cfg.min_edge), 0.01)
    max_pos = st.number_input("Max position ($)", min_value=1.0, value=float(cfg.max_position_usd), step=5.0)
    kelly = st.slider("Kelly fraction", 0.05, 1.0, float(cfg.kelly_fraction), 0.05)

    st.divider()
    c1, c2 = st.columns(2)
    start = c1.button("▶ Start", width="stretch", type="primary")
    stop = c2.button("⏹ Stop", width="stretch")


if start:
    if st.session_state.runner is not None:
        st.session_state.runner.stop()
    cfg.bankroll_usd = bankroll
    cfg.min_edge = min_edge
    cfg.max_position_usd = max_pos
    cfg.kelly_fraction = kelly
    runner = BotRunner(cfg, mode="live" if is_live else "paper",
                       execute_orders=execute_orders, timeframe=timeframe)
    runner.start()
    st.session_state.runner = runner

if stop and st.session_state.runner is not None:
    st.session_state.runner.stop()


# --------------------------------------------------------------------------
# formatting helpers
# --------------------------------------------------------------------------
def _hhmmss(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "—"


def _indicator_rows(analyses: dict) -> pd.DataFrame:
    rows = []
    for sym, a in analyses.items():
        rows.append({
            "symbol": sym, "trend": a["trend"].upper(),
            "EMA9": f"{a['ema9']:,.2f}" if a["ema9"] else "—",
            "EMA21": f"{a['ema21']:,.2f}" if a["ema21"] else "—",
            "RSI14": f"{a['rsi']:.1f}" if a["rsi"] is not None else "—",
            "MACD hist": f"{a['macd_hist']:+.2f}" if a["macd_hist"] is not None else "—",
            "ATR": f"{a['atr']:,.2f}" if a["atr"] else "—",
            "vol Δ%": f"{a['volume_change']:+.1f}" if a["volume_change"] is not None else "—",
            "body %": f"{a['body_pct']:.0f}" if a["body_pct"] is not None else "—",
            "score": f"{a['bull_score']}↑/{a['bear_score']}↓",
        })
    return pd.DataFrame(rows)


def _pattern_rows(analyses: dict) -> pd.DataFrame:
    rows = []
    for sym, a in analyses.items():
        rows.append({
            "symbol": sym,
            "patterns": ", ".join(a["patterns"]) or "—",
            "structure": ", ".join(k for k, v in a["structure"].items() if v) or "—",
            "signals": ", ".join(a["signals"]) or "—",
        })
    return pd.DataFrame(rows)


def _opp_rows(scanned: list) -> pd.DataFrame:
    cols = ["market", "t_left", "action", "side", "edge", "fair", "reason"]
    if not scanned:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(scanned)
    df["t_left"] = df["seconds_left"].map(lambda s: f"{s:.0f}s")
    df["edge"] = df["edge"].map(lambda e: f"{e:+.3f}")
    df["fair"] = df["fair"].map(lambda f: f"{f:.3f}")
    df["action"] = df["action"].map({"enter": "🟢 OPPORTUNITY", "possible": "🟡 possible",
                                     "no_trade": "⚪ no trade"}).fillna(df["action"])
    return df[cols]


def _open_rows(trades: list) -> pd.DataFrame:
    cols = ["id", "entry", "market", "side", "size", "entry_price", "reason"]
    if not trades:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(trades)
    df["entry"] = df["entry_time"].map(_hhmmss)
    df["entry_price"] = df["entry_price"].map(lambda p: f"{p:.3f}")
    df["reason"] = df["reason_entry"]
    return df[cols]


def _closed_rows(trades: list) -> pd.DataFrame:
    cols = ["entry", "close", "market", "side", "entry_price", "exit_price",
            "pnl", "result", "reason_entry", "reason_close"]
    if not trades:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(trades)
    df["entry"] = df["entry_time"].map(_hhmmss)
    df["close"] = df["close_time"].map(_hhmmss)
    df["entry_price"] = df["entry_price"].map(lambda p: f"{p:.3f}")
    df["exit_price"] = df["exit_price"].map(lambda p: f"${p:,.2f}")
    df["pnl"] = df["pnl"].map(lambda p: f"{p:+.2f}")
    return df[cols]


def _sim_scanned_rows(scanned: list) -> pd.DataFrame:
    cols = ["market", "t_left", "spot", "fair_up", "up_ask", "down_ask", "decision"]
    if not scanned:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(scanned)
    df["market"] = df["symbol"] + " " + df["duration_min"].astype(str) + "m"
    df["t_left"] = df["seconds_left"].map(lambda s: f"{s:.0f}s")
    df["spot"] = df["spot"].map(lambda s: f"${s:,.2f}")
    df["fair_up"] = df["fair_up"].map(lambda p: f"{p:.0%}")
    df["up_ask"] = df["up_ask"].map(lambda p: f"{p:.3f}" if p is not None else "—")
    df["down_ask"] = df["down_ask"].map(lambda p: f"{p:.3f}" if p is not None else "—")
    return df[cols]


# --------------------------------------------------------------------------
# main view (auto-refreshing fragment)
# --------------------------------------------------------------------------
@st.fragment(run_every=1.0)
def render_dashboard():
    runner = st.session_state.runner
    st.title("Polymarket Crypto Bot — Live Dashboard")

    if runner is None:
        st.info("Configure the mode and parameters in the sidebar, then press **Start**.")
        return

    s = runner.snapshot()

    if s.error:
        st.error(s.error)
    if s.warning:
        st.warning(s.warning, icon="⚠️")
    badge = "🟢 RUNNING" if s.running else "🔴 STOPPED"
    st.markdown(f"**{badge}** · mode: `{s.mode}` · timeframe: `{s.timeframe}`")

    live = s.mode == "live"

    # ---- live data header: prices + API health ----
    if live:
        h1, h2, h3, h4 = st.columns(4)
        btc = s.prices.get("BTC"); eth = s.prices.get("ETH")
        h1.metric("BTC (live)", f"${btc['price']:,.2f}" if btc else "—",
                  f"upd {_hhmmss(btc['ts'])}" if btc else "no data")
        h2.metric("ETH (live)", f"${eth['price']:,.2f}" if eth else "—",
                  f"upd {_hhmmss(eth['ts'])}" if eth else "no data")
        sok = s.spot_health.get("ok"); cok = s.candle_health.get("ok")
        h3.metric("Spot API", "✅ ok" if sok else "❌ down",
                  s.spot_health.get("source") or (s.spot_health.get("last_error") or "")[:24])
        h4.metric("Candle API", "✅ ok" if cok else "❌ down",
                  s.candle_health.get("source") or (s.candle_health.get("last_error") or "")[:24])
        if not s.data_available:
            st.error("Live data unavailable — trading is paused. No simulated prices are shown.")
    else:
        st.caption("🧪 Paper mode — prices below are **simulated**, not live market data.")

    # ---- common account metrics ----
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Balance", f"${s.equity:,.2f}", f"{s.equity - s.bankroll:+,.2f}")
    m2.metric("Total entries", s.entries)
    m3.metric("Win rate", f"{s.win_rate:.0%}", f"{s.wins}W / {s.losses}L")
    m4.metric("Realized PnL", f"${s.realized:+,.2f}")
    m5.metric("Open exposure", f"${s.exposure:,.2f}")

    st.subheader("📈 Equity curve")
    if len(s.equity_curve) > 1:
        st.line_chart(pd.DataFrame({"equity ($)": s.equity_curve}), height=240)
    else:
        st.caption("Waiting for the first settled trade…")

    if live:
        st.subheader("📊 Indicators")
        st.dataframe(_indicator_rows(s.analyses), hide_index=True, width="stretch")
        st.subheader("🕯️ Patterns & structure")
        st.dataframe(_pattern_rows(s.analyses), hide_index=True, width="stretch")

        st.subheader("🔭 Opportunity scanner")
        st.dataframe(_opp_rows(s.scanned), hide_index=True, width="stretch", height=240)

        oc1, oc2 = st.columns(2)
        with oc1:
            st.subheader(f"📂 Open trades ({len(s.open_trades)})")
            st.dataframe(_open_rows(s.open_trades), hide_index=True, width="stretch", height=240)
        with oc2:
            st.subheader(f"✅ Closed trades ({len(s.closed_trades)})")
            st.dataframe(_closed_rows(s.closed_trades), hide_index=True, width="stretch", height=240)

        st.subheader("📜 Opportunity log")
        st.dataframe(_opp_rows(list(s.opportunities)), hide_index=True, width="stretch", height=200)
    else:
        left, right = st.columns(2)
        with left:
            st.subheader("🔎 Scanned markets (simulated)")
            st.dataframe(_sim_scanned_rows(s.scanned), hide_index=True, width="stretch", height=300)
        with right:
            st.subheader("🧾 Recent trades (simulated)")
            df = pd.DataFrame(s.recent_trades)
            if not df.empty:
                df["time"] = df["time"].map(_hhmmss)
                df["market"] = df["symbol"] + " " + df["duration_min"].astype(str) + "m"
                df["price"] = df["price"].map(lambda p: f"{p:.3f}")
                df["pnl"] = df["pnl"].map(lambda p: f"{p:+.2f}")
                df = df[["time", "market", "side", "size", "price", "status", "pnl"]]
            st.dataframe(df, hide_index=True, width="stretch", height=300)

    st.caption(f"Updated {datetime.now().strftime('%H:%M:%S')} · "
               "Paper/sim PnL is illustrative, not a forward-return estimate.")


render_dashboard()
