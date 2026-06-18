"""Browser dashboard for polybot.

    streamlit run app.py

Default mode is **Live Data Paper Trading**: real BTC/ETH prices (Binance →
Coinbase), real Polymarket BTC/ETH 5m & 15m markets/order books, and PAPER
fills — no wallet, no funds, no real orders. The simulator is a *separate* test
mode and never feeds prices into the live view. If live data is unavailable the
bot pauses and shows a warning (never a fake price).
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

MODE_LIVE_PAPER = "🟢 Live Data Paper Trading (default)"
MODE_SIM = "🧪 Simulator Test Mode"
MODE_REAL = "🔴 Live Real Orders (real funds)"


# --------------------------------------------------------------------------
# Sidebar
# --------------------------------------------------------------------------
with st.sidebar:
    st.title("⚡ polybot")
    st.caption("Polymarket short-term crypto bot (BTC/ETH 5m & 15m)")

    mode_label = st.radio("Mode", [MODE_LIVE_PAPER, MODE_SIM, MODE_REAL], index=0,
                          help="Live Data Paper Trading uses REAL prices/markets with "
                               "paper fills and needs no key. Simulator is synthetic. "
                               "Live Real Orders places real trades (needs a funded key).")
    timeframe = st.selectbox("Candle timeframe", ["1m", "5m", "15m"], index=1)

    enable_real = False
    if mode_label == MODE_REAL:
        st.caption(f"🔑 Private key in .env: {'✅ yes' if cfg.private_key else '❌ no'}")
        enable_real = st.checkbox("I understand — enable REAL orders with REAL funds", value=False)
        if not enable_real:
            st.info("Real orders are disabled until you tick the box above.")
    elif mode_label == MODE_LIVE_PAPER:
        st.caption("🔓 No private key needed. Real data, paper fills.")
    else:
        st.caption("🧪 Synthetic prices for development/testing only.")

    st.divider()
    from polybot.marketdata import SUPPORTED_SYMBOLS
    symbols = st.multiselect(
        "Symbols", SUPPORTED_SYMBOLS, default=SUPPORTED_SYMBOLS,
        help="More symbols = more markets = more trades. All on by default.")

    st.subheader("Signal quality (no trade-count target)")
    high_conf = st.checkbox("High-confidence only", value=cfg.high_confidence_only,
                            help="Only enter when trend + indicators + pattern + edge all "
                                 "agree above the confidence bar. Unlimited trades when "
                                 "confident; weak setups skipped.")
    min_conf_pct = st.slider("Min confidence %", 50, 99, int(cfg.min_confidence_pct), 1)
    min_edge = st.slider("Edge threshold", 0.0, 0.20, float(cfg.min_edge), 0.005)
    max_spread = st.slider("Max spread", 0.0, 0.50, float(cfg.max_spread), 0.01)
    min_liq = st.number_input("Min liquidity", min_value=0.0, value=float(cfg.min_liquidity), step=10.0)

    st.subheader("Risk (always enforced)")
    bankroll = st.number_input("Bankroll ($)", min_value=10.0, value=float(cfg.bankroll_usd), step=10.0)
    max_pos = st.number_input("Max position ($)", min_value=1.0, value=float(cfg.max_position_usd), step=5.0)
    max_exp = st.number_input("Max total exposure ($)", min_value=1.0,
                              value=float(cfg.max_total_exposure_usd), step=25.0)
    kelly = st.slider("Kelly fraction", 0.05, 1.0, float(cfg.kelly_fraction), 0.05)
    daily_stop_pct = st.slider("Daily stop-loss (% of capital)", 0, 100,
                               int(cfg.daily_loss_limit_pct * 100), 5,
                               help="Halt trading for the rest of the day after losing this "
                                    "fraction of capital. 0 = no daily stop.")

    st.divider()
    c1, c2 = st.columns(2)
    start = c1.button("▶ Start", width="stretch", type="primary")
    stop = c2.button("⏹ Stop", width="stretch")


if start:
    if mode_label == MODE_REAL and not enable_real:
        st.sidebar.error("Tick the confirmation box to start real-order mode.")
    elif mode_label == MODE_REAL and not cfg.private_key:
        st.sidebar.error("Real orders need POLYMARKET_PRIVATE_KEY in .env.")
    else:
        if st.session_state.runner is not None:
            st.session_state.runner.stop()
        cfg.bankroll_usd = bankroll
        cfg.min_edge = min_edge
        cfg.max_spread = max_spread
        cfg.min_liquidity = min_liq
        cfg.high_confidence_only = high_conf
        cfg.min_confidence_pct = float(min_conf_pct)
        cfg.max_position_usd = max_pos
        cfg.max_total_exposure_usd = max_exp
        cfg.kelly_fraction = kelly
        cfg.daily_loss_limit_pct = daily_stop_pct / 100.0
        cfg.symbols = symbols or cfg.symbols
        runner = BotRunner(
            cfg,
            mode="paper" if mode_label == MODE_SIM else "live",
            execute_orders=(mode_label == MODE_REAL and enable_real),
            timeframe=timeframe,
            min_confidence=2,
        )
        runner.start()
        st.session_state.runner = runner

if stop and st.session_state.runner is not None:
    st.session_state.runner.stop()


# --------------------------------------------------------------------------
# formatting helpers
# --------------------------------------------------------------------------
def _hhmmss(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "—"


def _f(x, fmt="{:.3f}", dash="—"):
    return fmt.format(x) if x is not None else dash


def _indicator_rows(analyses):
    return pd.DataFrame([{
        "symbol": sym, "trend": a["trend"].upper(),
        "EMA9": _f(a["ema9"], "{:,.2f}"), "EMA21": _f(a["ema21"], "{:,.2f}"),
        "RSI14": _f(a["rsi"], "{:.1f}"), "MACD hist": _f(a["macd_hist"], "{:+.2f}"),
        "ATR": _f(a["atr"], "{:,.2f}"), "vol Δ%": _f(a["volume_change"], "{:+.1f}"),
        "body %": _f(a["body_pct"], "{:.0f}"), "score": f"{a['bull_score']}↑/{a['bear_score']}↓",
    } for sym, a in analyses.items()])


def _pattern_rows(analyses):
    return pd.DataFrame([{
        "symbol": sym,
        "patterns": ", ".join(a["patterns"]) or "—",
        "structure": ", ".join(k for k, v in a["structure"].items() if v) or "—",
        "signals": ", ".join(a["signals"]) or "—",
    } for sym, a in analyses.items()])


def _markets_rows(scanned):
    cols = ["market", "expiry", "YES", "NO", "spread", "liquidity"]
    if not scanned:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(scanned)
    df["expiry"] = df["expiry"].map(_hhmmss)
    df["YES"] = df["yes_price"].map(lambda p: _f(p))
    df["NO"] = df["no_price"].map(lambda p: _f(p))
    df["spread"] = df["spread"].map(lambda p: _f(p))
    df["liquidity"] = df["liquidity"].map(lambda v: _f(v, "{:,.0f}"))
    return df[cols]


def _opp_rows(scanned):
    cols = ["market", "t_left", "action", "conf%", "model", "side", "edge", "blockers", "reason"]
    if not scanned:
        return pd.DataFrame(columns=cols)
    rows = []
    amap = {"enter": "🟢 TRADE", "possible": "🟡 possible", "no_trade": "⚪ no trade",
            "blocked": "🚫 blocked", "ENTER": "🟢 ENTER"}
    for o in scanned:
        rows.append({
            "market": o.get("market"), "t_left": f"{o.get('seconds_left', 0):.0f}s",
            "action": amap.get(o.get("action"), o.get("action")),
            "conf%": f"{o.get('confidence_pct', 0):.0f}%",
            "model": (f"{o['model']:.0%}" if o.get("model") is not None else "—"),
            "side": o.get("side"), "edge": f"{o.get('edge', 0):+.3f}",
            "blockers": ", ".join(o.get("blockers", []) or []) or "—",
            "reason": o.get("reason", ""),
        })
    return pd.DataFrame(rows)[cols]


def _open_rows(trades):
    cols = ["id", "entry", "market", "side", "size", "entry_px", "entry_spot",
            "mark", "uPnL", "reason"]
    if not trades:
        return pd.DataFrame(columns=cols)
    rows = []
    for t in trades:
        rows.append({
            "id": t.get("id"), "entry": _hhmmss(t.get("entry_time")),
            "market": t.get("market"), "side": t.get("side"), "size": t.get("size"),
            "entry_px": _f(t.get("entry_price")),      # token price paid
            "entry_spot": _spot(t.get("candle_open")), # underlying at entry
            "mark": _f(t.get("mark")), "uPnL": f"{t.get('upnl', 0.0):+.2f}",
            "reason": t.get("reason_entry", ""),
        })
    return pd.DataFrame(rows)[cols]


def _spot(x):
    return f"${x:,.2f}" if x is not None else "—"


def _closed_rows(trades):
    cols = ["entry", "close", "market", "side", "entry_px", "exit_px",
            "entry_spot", "exit_spot", "pnl", "result"]
    if not trades:
        return pd.DataFrame(columns=cols)
    rows = []
    for t in trades:
        rows.append({
            "entry": _hhmmss(t.get("entry_time")), "close": _hhmmss(t.get("close_time")),
            "market": t.get("market"), "side": t.get("side"),
            "entry_px": _f(t.get("entry_price")),     # token price paid
            "exit_px": _f(t.get("exit_price")),       # token settlement value (1/0)
            "entry_spot": _spot(t.get("entry_spot")), # underlying at entry
            "exit_spot": _spot(t.get("exit_spot")),   # underlying at resolution
            "pnl": f"{t.get('pnl', 0.0):+.2f}", "result": t.get("result"),
        })
    return pd.DataFrame(rows)[cols]


def _exit_perf_rows(perf):
    cols = ["exit_type", "count", "win_rate", "avg_pnl", "pnl"]
    if not perf:
        return pd.DataFrame(columns=cols)
    rows = [{"exit_type": p["exit_type"], "count": p["count"],
             "win_rate": f"{p['win_rate']:.0%}", "avg_pnl": f"{p['avg_pnl']:+.2f}",
             "pnl": f"{p['pnl']:+.2f}"} for p in perf]
    return pd.DataFrame(rows)[cols]


def _discovery_panel(disc):
    if not disc:
        st.caption("No discovery report yet — waiting for the first market scan…")
        return
    if disc.get("error"):
        st.error(f"Discovery error: {disc['error']}")
    d1, d2, d3, d4 = st.columns(4)
    d1.metric("Markets returned", disc.get("markets_returned", 0))
    d2.metric("Accepted (BTC/ETH 5m/15m)", disc.get("accepted", 0))
    d3.metric("Filtered out", disc.get("filtered_out", 0))
    d4.metric("HTTP status", disc.get("http_status") or "—")
    st.write(f"**Endpoint:** `{disc.get('endpoint','?')}` · "
             f"**strategy used:** `{disc.get('strategy_used')}`")

    st.write("**Query attempts:**")
    st.dataframe(pd.DataFrame(disc.get("attempts", [])), hide_index=True, width="stretch")

    reasons = disc.get("reasons", {})
    if reasons:
        st.write("**Why markets were filtered (count by reason):**")
        st.dataframe(pd.DataFrame([{"reason": k, "count": v} for k, v in
                                  sorted(reasons.items(), key=lambda x: -x[1])],),
                     hide_index=True, width="stretch")

    cw = disc.get("crypto_titles", [])
    st.write(f"**BTC/ETH-related markets found ({len(cw)}):**")
    if cw:
        cdf = pd.DataFrame(cw)
        if "expiry" in cdf:
            cdf["expiry"] = cdf["expiry"].map(lambda t: _hhmmss(t) if t else "—")
        st.dataframe(cdf, hide_index=True, width="stretch", height=220)
    else:
        st.caption("No BTC/ETH markets matched even loosely — the endpoint may be "
                   "returning unrelated markets, or field names changed (see raw sample).")

    dm = disc.get("discovered_markets", [])
    if dm:
        st.write("**Discovered markets — expiry / YES / NO / liquidity:**")
        ddf = pd.DataFrame(dm)
        ddf["expiry"] = ddf["expiry"].map(lambda t: _hhmmss(t) if t else "—")
        for col in ("yes", "no"):
            ddf[col] = ddf[col].map(lambda p: _f(p))
        ddf["liquidity"] = ddf["liquidity"].map(lambda v: _f(v, "{:,.0f}"))
        st.dataframe(ddf, hide_index=True, width="stretch", height=200)

    with st.expander("First 20 titles returned"):
        st.write(disc.get("first_titles", []))
    with st.expander("Available fields + raw market sample (verify field names)"):
        st.write("**Fields present on returned markets:**", disc.get("available_fields", []))
        st.json(disc.get("raw_sample", {}))


def _sim_scanned_rows(scanned):
    cols = ["market", "t_left", "spot", "fair_up", "up_ask", "down_ask", "decision"]
    if not scanned:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(scanned)
    df["market"] = df["symbol"] + " " + df["duration_min"].astype(str) + "m"
    df["t_left"] = df["seconds_left"].map(lambda s: f"{s:.0f}s")
    df["spot"] = df["spot"].map(lambda s: f"${s:,.2f}")
    df["fair_up"] = df["fair_up"].map(lambda p: f"{p:.0%}")
    df["up_ask"] = df["up_ask"].map(lambda p: _f(p))
    df["down_ask"] = df["down_ask"].map(lambda p: _f(p))
    return df[cols]


# --------------------------------------------------------------------------
# main view
# --------------------------------------------------------------------------
@st.fragment(run_every=1.0)
def render_dashboard():
    runner = st.session_state.runner
    st.title("Polymarket Crypto Bot — Dashboard")

    if runner is None:
        st.info("Pick a mode in the sidebar and press **Start**. "
                "Default is Live Data Paper Trading (real data, paper fills, no key).")
        return

    s = runner.snapshot()
    live = s.mode == "live"

    if s.error:
        st.error(s.error)
    if s.warning:
        st.warning(s.warning, icon="⚠️")
    badge = "🟢 RUNNING" if s.running else "🔴 STOPPED"
    mode_name = ("LIVE DATA PAPER TRADING" if live and not runner.execute_orders
                 else "LIVE REAL ORDERS" if live else "SIMULATOR TEST MODE")
    sim_flag = "TRUE" if s.simulator_active else "FALSE"
    st.markdown(f"**{badge}** · **{mode_name}** · timeframe `{s.timeframe}` · "
                f"simulator active: **{sim_flag}**")

    # ---- live header: prices + API health ----
    if live:
        h1, h2, h3, h4, h5 = st.columns(5)
        btc, eth = s.prices.get("BTC"), s.prices.get("ETH")
        h1.metric("BTC (live)", f"${btc['price']:,.2f}" if btc else "—",
                  f"{btc['source']} · {_hhmmss(btc['ts'])}" if btc else "no data")
        h2.metric("ETH (live)", f"${eth['price']:,.2f}" if eth else "—",
                  f"{eth['source']} · {_hhmmss(eth['ts'])}" if eth else "no data")
        h3.metric("Spot API", "✅" if s.spot_health.get("ok") else "❌",
                  s.spot_health.get("source") or "down")
        h4.metric("Candle API", "✅" if s.candle_health.get("ok") else "❌",
                  s.candle_health.get("source") or "down")
        ph = s.polymarket_health
        h5.metric("Polymarket API", "✅" if ph.get("discover_ok") else "❌",
                  f"{ph.get('markets', 0)} mkts")
        if not s.data_available:
            st.error("Live data unavailable — trading is paused. "
                     "No simulated prices are shown (simulator FALSE).")
        d = s.entry_diagnostics
        if d:
            dp, dl = d.get("daily_pnl", 0.0), d.get("daily_limit", 0.0)
            if d.get("halted"):
                st.error(f"🛑 STOPPED FOR THE DAY — daily loss limit reached "
                         f"(day PnL ${dp:+.2f}, limit -${dl:.2f}). "
                         f"New entries are paused; resumes at UTC midnight.", icon="🛑")
            elif dl > 0:
                st.caption(f"Daily stop-loss: day PnL **${dp:+.2f}** of allowed **-${dl:.2f}** "
                           f"({(-dp / dl * 100) if dl else 0:.0f}% used)")
            open_n, max_open = d.get("open", 0), d.get("max_open", "∞")
            exp, max_exp = d.get("exposure", 0), d.get("max_exposure", 0)
            blockers = d.get("blockers", {})
            mode_txt = (f"HIGH-CONFIDENCE-ONLY ≥{d.get('min_confidence_pct', 0):.0f}%"
                        if d.get("high_confidence_only") else "all directional setups")
            msg = (f"Mode: **{mode_txt}** · scanned **{d.get('scanned', 0)}** · "
                   f"strong signals **{d.get('enter_signals', 0)}** · "
                   f"skipped opportunities **{d.get('skipped_opportunities', 0)}** · "
                   f"open **{open_n}/{max_open}** · exposure **${exp:.0f}/${max_exp:.0f}** · "
                   f"~{d.get('trades_per_hour', 0)}/hr")
            st.info(msg, icon="🔎")
            if blockers:
                st.caption("🚧 Active blockers: " +
                           " · ".join(f"**{k}** ×{v}" for k, v in
                                      sorted(blockers.items(), key=lambda x: -x[1])))
            if max_exp and exp >= max_exp * 0.99:
                st.caption("⚠️ At max exposure — raise `max_total_exposure_usd` / `bankroll_usd`.")
    else:
        st.caption("🧪 SIMULATOR TEST MODE — prices below are **synthetic** (not live).")

    # ---- account metrics ----
    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("Balance", f"${s.equity:,.2f}", f"{s.equity - s.bankroll:+,.2f}")
    m2.metric("Total entries", s.entries)
    m3.metric("Win rate", f"{s.win_rate:.0%}", f"{s.wins}W / {s.losses}L")
    m4.metric("Realized PnL", f"${s.realized:+,.2f}")
    m5.metric("Unrealized PnL", f"${s.unrealized:+,.2f}")
    m6.metric("Open exposure", f"${s.exposure:,.2f}")

    st.subheader("📈 Equity curve")
    if len(s.equity_curve) > 1:
        st.line_chart(pd.DataFrame({"equity ($)": s.equity_curve}), height=220)
    else:
        st.caption("Waiting for the first settled trade…")

    if live:
        i1, i2 = st.columns(2)
        with i1:
            st.subheader("📊 Indicators")
            st.dataframe(_indicator_rows(s.analyses), hide_index=True, width="stretch")
        with i2:
            st.subheader("🕯️ Patterns & structure")
            st.dataframe(_pattern_rows(s.analyses), hide_index=True, width="stretch")

        mc1, mc2 = st.columns([3, 1])
        mc1.subheader("🎯 Polymarket markets (real)")
        if mc2.button("🔄 Refresh Markets", width="stretch"):
            runner.request_market_refresh()
        st.dataframe(_markets_rows(s.scanned), hide_index=True, width="stretch", height=200)

        disc = s.polymarket_health.get("discovery", {})
        auto_open = bool(s.data_available and not s.scanned)   # exactly the "0 markets" case
        with st.expander("🔍 Market Discovery Debug", expanded=auto_open):
            _discovery_panel(disc)

        st.subheader("🔭 Opportunity scanner")
        st.dataframe(_opp_rows(s.scanned), hide_index=True, width="stretch", height=200)

        o1, o2 = st.columns(2)
        with o1:
            st.subheader(f"📂 Open trades ({len(s.open_trades)})")
            st.caption("entry_px = token price paid · entry_spot = asset price at entry · "
                       "mark = current value · held to resolution")
            st.dataframe(_open_rows(s.open_trades), hide_index=True, width="stretch", height=220)
        with o2:
            st.subheader(f"✅ Closed trades ({len(s.closed_trades)})")
            st.caption("entry_px/exit_px = token paid → settlement (1/0) · "
                       "entry_spot/exit_spot = asset price at entry → resolution · "
                       "result = prediction correct")
            st.dataframe(_closed_rows(s.closed_trades), hide_index=True, width="stretch", height=220)

        st.subheader("🎯 Performance by confidence")
        cb1, cb2 = st.columns([2, 1])
        with cb1:
            st.caption("Win rate and PnL grouped by the entry confidence score")
            buckets = s.confidence_buckets
            if buckets:
                bdf = pd.DataFrame([{
                    "confidence": b["bucket"], "trades": b["count"],
                    "win_rate": f"{b['win_rate']:.0%}", "pnl": f"{b['pnl']:+.2f}",
                } for b in buckets])
                st.dataframe(bdf, hide_index=True, width="stretch")
            else:
                st.caption("Fills in as trades close.")
        with cb2:
            # high-confidence-only (≥80) aggregate
            hi = [b for b in s.confidence_buckets if b["bucket"] in ("80-89", "90-100")]
            n = sum(b["count"] for b in hi)
            wins = sum(round(b["win_rate"] * b["count"]) for b in hi)
            pnl = sum(b["pnl"] for b in hi)
            st.metric("High-conf trades (≥80%)", n, f"{(wins / n * 100) if n else 0:.0f}% win")
            st.metric("High-conf PnL", f"${pnl:+,.2f}")
            st.metric("Skipped opportunities",
                      s.entry_diagnostics.get("skipped_opportunities", 0))

        st.subheader("🧠 Self-learning model")
        ms = s.model_stats
        if ms:
            g1, g2, g3 = st.columns([1, 1, 2])
            ready = ms.get("ready")
            g1.metric("Training samples", ms.get("samples", 0),
                      "active" if ready else f"need {ms.get('min_samples', 0)}")
            acc = ms.get("recent_accuracy")
            g2.metric("Model accuracy", f"{acc:.0%}" if acc is not None else "—",
                      "P(correct) calls")
            with g3:
                st.caption("Signal weights the model has learned "
                           "(|weight| = how much that signal matters; +favours the bet)")
                st.dataframe(pd.DataFrame(ms.get("weights", [])), hide_index=True, width="stretch")
            if not ready:
                st.caption(f"Cold start — running on rules until "
                           f"{ms.get('min_samples', 0)} trades are logged. The model column "
                           "in the scanner activates once trained.")
        else:
            st.caption("Model warms up after the first closed trades.")

        st.subheader("📜 Opportunity log")
        st.dataframe(_opp_rows(list(s.opportunities)), hide_index=True, width="stretch", height=180)

        with st.expander("🛠️ Debug panel (raw sources)"):
            st.write(f"**Simulator active:** `{sim_flag}` (must be FALSE in live mode)")
            st.write("**Spot sources (raw):**", s.debug.get("spot_raw", {}))
            st.write("**Candle sources:**", s.debug.get("candles", {}))
            st.write("**Polymarket API:**", s.polymarket_health)
    else:
        left, right = st.columns(2)
        with left:
            st.subheader("🔎 Scanned markets (synthetic)")
            st.dataframe(_sim_scanned_rows(s.scanned), hide_index=True, width="stretch", height=300)
        with right:
            st.subheader("🧾 Recent trades (synthetic)")
            df = pd.DataFrame(s.recent_trades)
            if not df.empty:
                df["time"] = df["time"].map(_hhmmss)
                df["market"] = df["symbol"] + " " + df["duration_min"].astype(str) + "m"
                df["price"] = df["price"].map(lambda p: f"{p:.3f}")
                df["pnl"] = df["pnl"].map(lambda p: f"{p:+.2f}")
                df = df[["time", "market", "side", "size", "price", "status", "pnl"]]
            st.dataframe(df, hide_index=True, width="stretch", height=300)
        with st.expander("🛠️ Debug panel"):
            st.write(f"**Simulator active:** `{sim_flag}`")
            st.write(s.debug)

    st.caption(f"Updated {datetime.now().strftime('%H:%M:%S')} · "
               "Paper/sim PnL is illustrative, not a forward-return estimate.")


render_dashboard()
