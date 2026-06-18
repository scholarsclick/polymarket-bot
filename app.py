"""Browser dashboard for polybot.

    streamlit run app.py

Paper mode runs a self-contained simulator (no network, no private key).
Live mode drives the real engine against Polymarket; reading real markets with
simulated fills needs only network access, while placing real orders requires a
funded private key in `.env`.
"""

from __future__ import annotations

import time
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
        "Mode",
        ["Paper (simulated)", "Live (real markets)"],
        help="Paper runs an offline simulator and needs no private key. "
             "Live connects to Polymarket.",
    )
    is_live = mode_label.startswith("Live")

    execute_orders = False
    if is_live:
        execute_orders = st.checkbox(
            "Execute REAL orders (requires funded key)", value=False,
            help="Off = read real markets, simulate fills (network only). "
                 "On = place real orders with real funds.",
        )
        has_key = bool(cfg.private_key)
        st.caption(f"🔑 Private key in .env: {'✅ yes' if has_key else '❌ no'}")
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


# --------------------------------------------------------------------------
# Start / stop handling
# --------------------------------------------------------------------------
if start:
    if st.session_state.runner is not None:
        st.session_state.runner.stop()
    cfg.bankroll_usd = bankroll
    cfg.min_edge = min_edge
    cfg.max_position_usd = max_pos
    cfg.kelly_fraction = kelly
    runner = BotRunner(
        cfg,
        mode="live" if is_live else "paper",
        execute_orders=execute_orders,
    )
    runner.start()
    st.session_state.runner = runner

if stop and st.session_state.runner is not None:
    st.session_state.runner.stop()


# --------------------------------------------------------------------------
# Main view (auto-refreshing fragment)
# --------------------------------------------------------------------------
def _fmt_trades(rows):
    if not rows:
        return pd.DataFrame(columns=["time", "market", "side", "size", "price", "status", "pnl"])
    df = pd.DataFrame(rows)
    df["time"] = df["time"].apply(lambda t: datetime.fromtimestamp(t).strftime("%H:%M:%S"))
    df["market"] = df["symbol"] + " " + df["duration_min"].astype(str) + "m"
    df["price"] = df["price"].map(lambda p: f"{p:.3f}")
    df["pnl"] = df["pnl"].map(lambda p: f"{p:+.2f}")
    return df[["time", "market", "side", "size", "price", "status", "pnl"]]


def _fmt_scanned(rows):
    cols = ["market", "t_left", "spot", "fair_up", "up_ask", "down_ask", "decision"]
    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(rows)
    df["market"] = df["symbol"] + " " + df["duration_min"].astype(str) + "m"
    df["t_left"] = df["seconds_left"].map(lambda s: f"{s:.0f}s")
    df["spot"] = df["spot"].map(lambda s: f"${s:,.2f}")
    df["fair_up"] = df["fair_up"].map(lambda p: f"{p:.0%}")
    df["up_ask"] = df["up_ask"].map(lambda p: f"{p:.3f}" if p is not None else "—")
    df["down_ask"] = df["down_ask"].map(lambda p: f"{p:.3f}" if p is not None else "—")
    return df[cols]


@st.fragment(run_every=1.0)
def render_dashboard():
    runner = st.session_state.runner
    st.title("Polymarket Crypto Bot — Live Dashboard")

    if runner is None:
        st.info("Configure the mode and parameters in the sidebar, then press **Start**.")
        return

    s = runner.snapshot()

    # status line
    if s.error:
        st.error(s.error)
    badge = "🟢 RUNNING" if s.running else "🔴 STOPPED"
    sim_note = " · simulated feed" if s.mode == "paper" else " · real markets"
    st.markdown(f"**{badge}**  ·  mode: `{s.mode}`{sim_note}")

    # metrics
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Balance", f"${s.equity:,.2f}", f"{s.equity - s.bankroll:+,.2f}")
    m2.metric("Total entries", s.entries)
    m3.metric("Win rate", f"{s.win_rate:.0%}", f"{s.wins}W / {s.losses}L")
    m4.metric("Realized PnL", f"${s.realized:+,.2f}")
    m5.metric("Open exposure", f"${s.exposure:,.2f}")

    # equity curve
    st.subheader("📈 Equity curve")
    if len(s.equity_curve) > 1:
        st.line_chart(pd.DataFrame({"equity ($)": s.equity_curve}), height=260)
    else:
        st.caption("Waiting for the first settled market…")

    left, right = st.columns(2)
    with left:
        st.subheader("🔎 Scanned markets")
        st.dataframe(_fmt_scanned(s.scanned), hide_index=True, width="stretch", height=300)
    with right:
        st.subheader("🧾 Recent trades")
        st.dataframe(_fmt_trades(s.recent_trades), hide_index=True, width="stretch", height=300)

    st.caption(f"Updated {datetime.now().strftime('%H:%M:%S')} · "
               "Paper/sim PnL is illustrative, not a forward-return estimate.")


render_dashboard()
