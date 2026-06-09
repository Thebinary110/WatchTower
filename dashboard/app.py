"""WatchdogAI — Bloomberg Terminal Style Dashboard."""

import csv
import io
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components
import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from bus.redis_client import RedisClient
from storage.watchdog_log import WatchdogLog

st.set_page_config(
    page_title="WATCHDOG·AI",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Color + font constants ────────────────────────────────────────────────────
C = {
    "bg_base":      "#0A0A0F",
    "bg_panel":     "#0D1117",
    "bg_panel2":    "#111827",
    "bg_ticker":    "#050508",
    "green":        "#00FF88",
    "red":          "#FF3366",
    "amber":        "#FFB800",
    "blue":         "#4D9FFF",
    "cyan":         "#00D4FF",
    "border":       "#1E2433",
    "border_b":     "#2A3548",
    "text":         "#E8EDF5",
    "text2":        "#8892A4",
    "text3":        "#4A5568",
}

SEV_COLORS  = {1: C["blue"], 2: C["green"], 3: C["amber"], 4: C["red"], 5: "#880000"}
SEV_LABELS  = {1: "INFO", 2: "MINOR", 3: "REVIEW", 4: "ACTION", 5: "CRITICAL"}
REGIME_COLORS = {
    "TRENDING":        C["green"],
    "HIGH_VOLATILITY": C["red"],
    "MEAN_REVERTING":  C["blue"],
    "TRANSITIONAL":    C["amber"],
    "UNKNOWN":         C["text3"],
}
AGENT_COLORS = {
    "MetricsAgent":   C["blue"],
    "DiagnosisAgent": C["amber"],
    "ActionAgent":    C["red"],
}


# ── CSS injection ─────────────────────────────────────────────────────────────
GLOBAL_CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700&family=Inter:wght@300;400;500;600&display=swap');

:root {{
  --bg-base:      {C['bg_base']};
  --bg-panel:     {C['bg_panel']};
  --bg-panel2:    {C['bg_panel2']};
  --green:        {C['green']};
  --red:          {C['red']};
  --amber:        {C['amber']};
  --blue:         {C['blue']};
  --cyan:         {C['cyan']};
  --border:       {C['border']};
  --border-b:     {C['border_b']};
  --text:         {C['text']};
  --text2:        {C['text2']};
  --text3:        {C['text3']};
  --mono: 'JetBrains Mono', 'Fira Code', monospace;
  --ui:   'Inter', system-ui, sans-serif;
}}

html, body, [data-testid="stAppViewContainer"] {{
  background: var(--bg-base) !important;
  color: var(--text) !important;
}}

[data-testid="stHeader"], footer, #MainMenu {{ display: none !important; }}

[data-testid="stSidebar"] {{
  background: {C['bg_ticker']} !important;
  border-right: 1px solid var(--border-b);
}}

[data-testid="stSidebar"] * {{ color: var(--text) !important; }}

section.main > div {{ padding-top: 0 !important; }}

::-webkit-scrollbar {{ width: 4px; height: 4px; }}
::-webkit-scrollbar-track {{ background: var(--bg-base); }}
::-webkit-scrollbar-thumb {{ background: var(--border-b); border-radius: 2px; }}

.terminal-panel {{
  background: var(--bg-panel);
  border: 1px solid var(--border);
  padding: 14px 16px;
  font-family: var(--mono);
}}

.terminal-label {{
  font-family: var(--ui);
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 1.5px;
  color: var(--text2);
  margin-bottom: 4px;
}}

.terminal-value {{
  font-family: var(--mono);
  font-size: 26px;
  font-weight: 700;
  line-height: 1.1;
}}

.terminal-empty {{
  font-family: var(--mono);
  font-size: 12px;
  color: var(--text3);
  padding: 20px;
  text-align: center;
  border: 1px dashed var(--border);
  background: var(--bg-panel);
}}

.z-badge {{
  font-family: var(--mono);
  font-size: 11px;
  padding: 2px 7px;
  border-radius: 3px;
  font-weight: 600;
}}

.status-strip {{
  height: 3px;
  border-radius: 0 0 2px 2px;
  margin-top: 8px;
}}

.bbg-divider {{
  font-family: var(--mono);
  font-size: 10px;
  color: var(--text3);
  letter-spacing: 1px;
  margin: 12px 0 8px 0;
  display: flex;
  align-items: center;
  gap: 8px;
}}
.bbg-divider::before, .bbg-divider::after {{
  content: '';
  flex: 1;
  height: 1px;
  background: var(--border);
}}

.alert-card {{
  font-family: var(--mono);
  font-size: 12px;
  padding: 10px 12px;
  margin-bottom: 8px;
  border-radius: 2px;
  border-left: 3px solid;
  background: var(--bg-panel2);
  line-height: 1.6;
}}

.fkey {{
  font-family: var(--mono);
  font-size: 10px;
  font-weight: 700;
  padding: 2px 6px;
  border: 1px solid;
  border-radius: 2px;
  letter-spacing: 0.5px;
}}

.console-wrap {{
  background: {C['bg_ticker']};
  border: 1px solid var(--border-b);
  height: 220px;
  overflow-y: auto;
  padding: 8px 10px;
  font-family: var(--mono);
  font-size: 11px;
  line-height: 1.7;
}}

.console-line {{
  padding: 1px 4px;
  border-radius: 2px;
}}

.console-line.alert {{
  border-left: 3px solid {C['red']};
  background: rgba(255,51,102,0.06);
  padding-left: 8px;
}}

.blink {{
  animation: blink 1s step-end infinite;
}}
@keyframes blink {{
  0%, 100% {{ opacity: 1; }}
  50% {{ opacity: 0; }}
}}

.regime-big {{
  font-family: var(--mono);
  font-size: 30px;
  font-weight: 700;
  letter-spacing: 2px;
}}

.detect-row {{
  display: flex;
  gap: 12px;
  margin: 8px 0;
}}

.detect-card {{
  flex: 1;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  padding: 8px 10px;
  font-family: var(--mono);
  font-size: 11px;
}}

.detect-label {{
  color: var(--text3);
  font-size: 9px;
  text-transform: uppercase;
  letter-spacing: 1px;
  margin-bottom: 3px;
}}

.stButton > button {{
  background: var(--bg-panel2) !important;
  color: var(--amber) !important;
  border: 1px solid var(--amber) !important;
  font-family: var(--mono) !important;
  font-size: 11px !important;
  font-weight: 600 !important;
  border-radius: 2px !important;
  padding: 4px 10px !important;
  letter-spacing: 0.5px;
  text-transform: uppercase;
}}
.stButton > button:hover {{
  background: var(--amber) !important;
  color: var(--bg-base) !important;
}}

.stDownloadButton > button {{
  background: var(--bg-panel) !important;
  color: var(--cyan) !important;
  border: 1px solid var(--cyan) !important;
  font-family: var(--mono) !important;
  font-size: 11px !important;
  border-radius: 2px !important;
}}
</style>
"""


# ── Cached loaders ─────────────────────────────────────────────────────────────
@st.cache_resource
def _load_config():
    with open(ROOT / "config.yaml") as f:
        return yaml.safe_load(f)

@st.cache_resource
def _get_redis(_cfg_hash):
    cfg = _load_config()
    return RedisClient(cfg)

@st.cache_resource
def _get_db(_cfg_hash):
    cfg = _load_config()
    return WatchdogLog(cfg["storage"]["sqlite_path"])

@st.cache_data(ttl=300)
def _fetch_btc_candles():
    """Fetch BTC/USDT 1H candles from Binance public endpoint."""
    try:
        import ccxt as _ccxt
        exchange = _ccxt.binance({
            "enableRateLimit": True,
            "fetchCurrencies": False,
            "options": {"fetchCurrencies": False, "defaultType": "spot"},
        })
        raw = exchange.fetch_ohlcv("BTC/USDT", "1h", limit=200)
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["time"] = pd.to_datetime(df["ts"], unit="ms")
        df["ema9"]  = df["close"].ewm(span=9,  adjust=False).mean()
        df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
        return df
    except Exception:
        return pd.DataFrame()


# ── Helpers ────────────────────────────────────────────────────────────────────
def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d  %H:%M:%S UTC")

def _ts_fmt(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%H:%M:%S")
    except Exception:
        return "——:——:——"

def _z_color(z: Optional[float]) -> str:
    if z is None: return C["text3"]
    az = abs(z)
    if az < 1.5: return C["green"]
    if az < 2.0: return C["amber"]
    return C["red"]

def _get_regime(cfg, redis_client) -> Dict:
    """Try Redis then RegimeRadar SQLite."""
    try:
        msgs = redis_client.read_stream_latest(cfg["redis"]["streams"]["regime"], 1)
        if msgs:
            p = msgs[0].get("payload", {})
            r = p.get("regime") or p.get("current_regime")
            if r:
                return {"regime": r, "confidence": p.get("confidence", 0),
                        "reasoning": p.get("reasoning", "")}
    except Exception:
        pass
    try:
        rr = Path(cfg["regimeradar"]["sqlite_path"])
        if rr.exists():
            conn = sqlite3.connect(str(rr))
            conn.row_factory = sqlite3.Row
            for tbl in ("regime_log", "regime_snapshots", "regimes"):
                try:
                    row = conn.execute(
                        f"SELECT regime, confidence, reasoning FROM {tbl} "
                        f"ORDER BY timestamp DESC LIMIT 1"
                    ).fetchone()
                    if row:
                        conn.close()
                        return {"regime": row["regime"],
                                "confidence": row["confidence"] or 0,
                                "reasoning": row["reasoning"] or ""}
                except Exception:
                    pass
            conn.close()
    except Exception:
        pass
    return {"regime": "UNKNOWN", "confidence": 0, "reasoning": ""}


# ── Ticker strip ───────────────────────────────────────────────────────────────
def _render_ticker(metrics_row, regime_info, backend):
    m = metrics_row or {}
    pnl   = m.get("pnl_cumulative", 0) or 0
    wr    = m.get("win_rate_50", 0) or 0
    fr    = m.get("fill_rate", 1) or 1
    dd    = m.get("drawdown_current", 0) or 0
    n     = m.get("total_trades", 0) or 0
    regime = regime_info.get("regime", "UNKNOWN")

    pnl_color  = C["green"] if pnl >= 0 else C["red"]
    wr_color   = C["green"] if wr > 0.5 else C["amber"]
    dd_color   = C["red"]   if dd > 0.05 else C["amber"] if dd > 0.02 else C["green"]
    reg_color  = REGIME_COLORS.get(regime, C["text3"])

    sep = f'<span style="color:{C["text3"]};margin:0 10px;">·</span>'
    items = [
        f'<span style="color:{C["cyan"]};">BTC/USDT</span>',
        f'<span style="color:{C["text2"]};">WIN RATE</span> <span style="color:{wr_color};">{wr:.1%}</span>',
        f'<span style="color:{C["text2"]};">FILL RATE</span> <span style="color:{C["green"]};">{fr:.1%}</span>',
        f'<span style="color:{C["text2"]};">DRAWDOWN</span> <span style="color:{dd_color};">{dd:.2%}</span>',
        f'<span style="color:{C["text2"]};">CUM PNL</span> <span style="color:{pnl_color};">${pnl:+,.0f}</span>',
        f'<span style="color:{C["text2"]};">TRADES</span> <span style="color:{C["text"]};">{n}</span>',
        f'<span style="color:{C["text2"]};">REGIME</span> <span style="color:{reg_color};">{regime}</span>',
        f'<span style="color:{C["text2"]};">BACKEND</span> <span style="color:{C["cyan"]};">{backend or "—"}</span>',
        f'<span style="color:{C["amber"]};">WATCHDOG·AI  ⚡  LIVE</span>',
    ]
    ticker_content = sep.join(items)

    components.html(f"""
    <style>
      @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500&display=swap');
      body {{ margin:0; padding:0; background:{C['bg_ticker']}; overflow:hidden; }}
      .ticker-wrap {{
        width:100%; height:32px; background:{C['bg_ticker']};
        border-top:1px solid {C['border_b']}; border-bottom:1px solid {C['border_b']};
        overflow:hidden; display:flex; align-items:center;
      }}
      .ticker-content {{
        display:inline-block;
        white-space:nowrap;
        font-family:'JetBrains Mono',monospace;
        font-size:11px;
        animation: ticker 35s linear infinite;
        padding-left: 100%;
      }}
      @keyframes ticker {{
        0%   {{ transform: translateX(0); }}
        100% {{ transform: translateX(-100%); }}
      }}
    </style>
    <div class="ticker-wrap">
      <div class="ticker-content">{ticker_content}&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;{ticker_content}</div>
    </div>
    """, height=36)


# ── Header ─────────────────────────────────────────────────────────────────────
def _render_header(redis_ok, ollama_ok, groq_ok):
    col_l, col_r = st.columns([2, 3])
    with col_l:
        st.markdown(
            f'<div style="font-family:var(--mono);font-size:22px;font-weight:700;'
            f'color:{C["green"]};letter-spacing:3px;padding:8px 0 4px 0;">'
            f'WATCHDOG·AI<span class="blink" style="color:{C["green"]};">█</span></div>',
            unsafe_allow_html=True,
        )
    with col_r:
        r_dot = f'<span style="color:{C["green"]};">●</span>' if redis_ok  else f'<span style="color:{C["red"]};">●</span>'
        o_dot = f'<span style="color:{C["green"]};">●</span>' if ollama_ok else f'<span style="color:{C["red"]};">●</span>'
        g_dot = f'<span style="color:{C["green"]};">●</span>' if groq_ok   else f'<span style="color:{C["text3"]};">○</span>'
        st.markdown(
            f'<div style="font-family:var(--mono);font-size:11px;color:{C["text2"]};'
            f'text-align:right;padding-top:10px;line-height:1.9;">'
            f'{r_dot} REDIS &nbsp;&nbsp; {o_dot} OLLAMA &nbsp;&nbsp; {g_dot} GROQ'
            f'<br><span style="color:{C["text3"]};">{_utcnow()}</span></div>',
            unsafe_allow_html=True,
        )
    st.markdown(f'<hr style="border:none;border-top:1px solid {C["border_b"]};margin:4px 0 12px 0;">', unsafe_allow_html=True)


# ── Metric panels ──────────────────────────────────────────────────────────────
def _sparkline(values: List[float], color: str) -> go.Figure:
    fig = go.Figure(go.Scatter(
        y=values, mode="lines",
        line=dict(color=color, width=1.5),
        fill="tozeroy",
        fillcolor=color.replace(")", ",0.08)").replace("rgb", "rgba") if "rgb" in color
                  else color + "14",
    ))
    fig.update_layout(
        height=50, margin=dict(l=0, r=0, t=0, b=0),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor=C["bg_panel"],
        xaxis=dict(visible=False), yaxis=dict(visible=False),
        showlegend=False,
    )
    return fig

def _render_metric_panels(all_metrics: List[Dict]):
    cols = st.columns(4)
    defs = [
        ("CUMULATIVE PnL",  "pnl_cumulative",    "zscore_pnl",      "${:+,.0f}",  None),
        ("WIN RATE · 50T",  "win_rate_50",        "zscore_win_rate", "{:.1%}",     None),
        ("FILL RATE",       "fill_rate",          "zscore_fill_rate","{:.1%}",     None),
        ("DRAWDOWN",        "drawdown_current",   "zscore_drawdown", "{:.2%}",     None),
    ]
    latest = all_metrics[0] if all_metrics else {}
    history = all_metrics[::-1]  # chronological

    for col, (label, field, z_field, fmt, _) in zip(cols, defs):
        val = latest.get(field)
        z   = latest.get(z_field)
        color = _z_color(z)
        z_text = f"{z:+.2f}σ" if z is not None else "—"
        val_str = fmt.format(val) if val is not None else "——"

        spark_vals = [r.get(field, 0) or 0 for r in history if r.get(field) is not None][-20:]

        with col:
            st.markdown(
                f'<div class="terminal-panel" style="padding-bottom:0;">'
                f'<div style="display:flex;justify-content:space-between;align-items:center;">'
                f'<span class="terminal-label">{label}</span>'
                f'<span class="z-badge" style="background:{color}22;color:{color};border:1px solid {color}44;">'
                f'{z_text}</span></div>'
                f'<div class="terminal-value" style="color:{color};">{val_str}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
            if spark_vals:
                st.plotly_chart(_sparkline(spark_vals, color),
                                use_container_width=True, config={"displayModeBar": False})
            st.markdown(
                f'<div class="status-strip" style="background:{color};margin-top:0;"></div>',
                unsafe_allow_html=True,
            )


# ── Candlestick chart ──────────────────────────────────────────────────────────
def _render_price_chart(alert_timestamps: List[float]):
    df = _fetch_btc_candles()
    if df.empty:
        st.markdown('<div class="terminal-empty">⚡ AWAITING PRICE DATA STREAM...</div>', unsafe_allow_html=True)
        return

    fig = go.Figure()

    # Volume (bottom 20%)
    vol_colors = [C["green"] if c >= o else C["red"]
                  for c, o in zip(df["close"], df["open"])]
    fig.add_trace(go.Bar(
        x=df["time"], y=df["volume"],
        marker_color=vol_colors, opacity=0.3,
        name="Volume", yaxis="y2", showlegend=False,
    ))

    # Candles
    fig.add_trace(go.Candlestick(
        x=df["time"],
        open=df["open"], high=df["high"],
        low=df["low"],   close=df["close"],
        increasing_line_color=C["green"],
        decreasing_line_color=C["red"],
        increasing_fillcolor=C["green"],
        decreasing_fillcolor=C["red"],
        name="BTC/USDT",
    ))

    # EMA overlays
    fig.add_trace(go.Scatter(
        x=df["time"], y=df["ema9"],
        mode="lines", name="EMA 9",
        line=dict(color=C["cyan"], width=1),
        opacity=0.8,
    ))
    fig.add_trace(go.Scatter(
        x=df["time"], y=df["ema21"],
        mode="lines", name="EMA 21",
        line=dict(color=C["amber"], width=1),
        opacity=0.8,
    ))

    # Anomaly vertical lines
    for ats in alert_timestamps[-5:]:
        try:
            t = datetime.fromtimestamp(float(ats), tz=timezone.utc)
            fig.add_vline(
                x=t.timestamp() * 1000,
                line_dash="dash", line_color=C["red"],
                line_width=1, opacity=0.7,
                annotation_text="⚠",
                annotation_font_color=C["red"],
                annotation_font_size=10,
            )
        except Exception:
            pass

    fig.update_layout(
        height=400,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor=C["bg_panel"],
        font=dict(family="JetBrains Mono", color=C["text2"], size=10),
        xaxis=dict(
            gridcolor=C["border"], showgrid=True,
            rangeslider_visible=False,
            linecolor=C["border"],
        ),
        yaxis=dict(
            gridcolor=C["border"], showgrid=True,
            side="right", linecolor=C["border"],
        ),
        yaxis2=dict(
            overlaying="y", side="left",
            showgrid=False, showticklabels=False,
            domain=[0, 0.2],
        ),
        legend=dict(
            orientation="h", x=0, y=1.02,
            bgcolor="rgba(13,17,23,0.8)",
            bordercolor=C["border"], borderwidth=1,
            font=dict(size=10),
        ),
        margin=dict(l=8, r=8, t=8, b=8),
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


# ── Metrics timeline ───────────────────────────────────────────────────────────
def _render_metrics_timeline(all_metrics: List[Dict]):
    if not all_metrics:
        st.markdown('<div class="terminal-empty">⚡ AWAITING METRICS STREAM...</div>', unsafe_allow_html=True)
        return
    df = pd.DataFrame(all_metrics[::-1])
    df["time"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)

    fig = go.Figure()
    for field, label, color in [
        ("win_rate_50", "WIN RATE", C["green"]),
        ("fill_rate",   "FILL RATE", C["blue"]),
        ("avg_slippage","SLIPPAGE",  C["amber"]),
    ]:
        if field in df.columns:
            s = df[field].fillna(0)
            rng = s.max() - s.min()
            norm = (s - s.min()) / rng if rng > 0 else s * 0
            fig.add_trace(go.Scatter(
                x=df["time"], y=norm, mode="lines",
                name=label, line=dict(color=color, width=1.2),
            ))
    fig.update_layout(
        height=140,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor=C["bg_panel"],
        font=dict(family="JetBrains Mono", color=C["text2"], size=9),
        xaxis=dict(gridcolor=C["border"], showgrid=True, linecolor=C["border"]),
        yaxis=dict(gridcolor=C["border"], showgrid=False, showticklabels=False),
        legend=dict(orientation="h", x=0, y=1.1, font=dict(size=9),
                    bgcolor="rgba(0,0,0,0)"),
        margin=dict(l=4, r=4, t=4, b=4),
        title=dict(text="NORMALISED METRICS TIMELINE", font=dict(size=9, color=C["text3"]),
                   x=0.5),
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


# ── Alerts panel ───────────────────────────────────────────────────────────────
def _render_alerts(active_alerts: List[Dict], db: WatchdogLog):
    count = len(active_alerts)
    st.markdown(
        f'<div style="font-family:var(--mono);font-size:12px;color:{C["text2"]};'
        f'letter-spacing:1px;margin-bottom:8px;">'
        f'ACTIVE ALERTS &nbsp;<span style="background:{C["red"] if count else C["green"]}22;'
        f'color:{C["red"] if count else C["green"]};border:1px solid;'
        f'padding:1px 7px;border-radius:2px;font-size:11px;">{count}</span></div>',
        unsafe_allow_html=True,
    )
    if not active_alerts:
        st.markdown(
            f'<div class="terminal-empty" style="color:{C["green"]};">✓ ALL SYSTEMS NOMINAL</div>',
            unsafe_allow_html=True,
        )
        return

    for alert in active_alerts[:6]:
        sev    = alert.get("severity", 1)
        sc     = SEV_COLORS.get(sev, C["text3"])
        sl     = SEV_LABELS.get(sev, "?")
        ts     = _ts_fmt(alert.get("timestamp", 0))
        metric = alert.get("metric_breached", "?")
        z      = alert.get("z_score", 0) or 0
        atype  = (alert.get("anomaly_type") or "unknown").replace("_", " ").upper()
        reason = alert.get("reasoning") or ""
        backend= alert.get("backend_used", "?")
        regime = alert.get("regime_context") or ""
        layer  = alert.get("detection_layer", "zscore")

        actions = alert.get("recommended_actions") or []
        if isinstance(actions, str):
            try: actions = json.loads(actions)
            except Exception: actions = [actions]

        layer_tag = (
            f'<div style="color:{C["amber"]};font-size:10px;margin-bottom:3px;">'
            f'◈ MULTIVARIATE ANOMALY · ISOLATION FOREST</div>'
            if layer == "isolation_forest" else
            f'<div style="color:{C["blue"]};font-size:10px;margin-bottom:3px;">'
            f'◉ THRESHOLD BREACH · Z-SCORE</div>'
        )
        actions_html = "".join(
            f'<div style="color:{C["cyan"]};margin-top:2px;">→ {a}</div>'
            for a in actions
        )
        regime_html = (
            f'<div style="color:{C["text3"]};font-size:10px;margin-top:4px;'
            f'font-style:italic;">{regime[:100]}</div>'
            if regime else ""
        )

        st.markdown(
            f'<div class="alert-card" style="border-color:{sc};">'
            f'{layer_tag}'
            f'<div style="display:flex;justify-content:space-between;">'
            f'<span style="color:{sc};font-weight:700;">'
            f'<span class="fkey" style="border-color:{sc};color:{sc};">SEV-{sev}</span> '
            f'<span class="fkey" style="border-color:{sc};color:{sc};">{sl}</span>'
            f' &nbsp;{atype}</span>'
            f'<span style="color:{C["text3"]};font-size:10px;">{ts} UTC</span></div>'
            f'<div style="color:{C["text2"]};font-size:11px;margin-top:3px;">'
            f'{metric} · z={z:+.2f}σ · via {backend}</div>'
            f'<div style="color:{C["text"]};margin-top:5px;font-size:11px;">{reason}</div>'
            f'{actions_html}'
            f'{regime_html}'
            f'</div>',
            unsafe_allow_html=True,
        )
        if st.button(f"✓ RESOLVE  #{alert['id']}", key=f"res_{alert['id']}"):
            db.resolve_alert(alert["id"])
            st.rerun()


# ── Agent activity log ─────────────────────────────────────────────────────────
def _render_agent_log(activity_msgs: List[Dict]):
    st.markdown(
        f'<div style="font-family:var(--mono);font-size:12px;color:{C["text2"]};'
        f'letter-spacing:1px;margin-bottom:6px;">'
        f'AGENT ACTIVITY LOG &nbsp;<span style="color:{C["text3"]};font-size:10px;">'
        f'last {min(len(activity_msgs),40)} events</span></div>',
        unsafe_allow_html=True,
    )
    if not activity_msgs:
        st.markdown('<div class="terminal-empty">⚡ AWAITING AGENT STREAM...</div>', unsafe_allow_html=True)
        return

    lines_html = ""
    for msg in activity_msgs[-40:]:
        src    = msg.get("source", "?")
        action = msg.get("action", "")
        detail = msg.get("detail", "")[:140]
        ts     = _ts_fmt(msg.get("timestamp", 0))
        is_al  = msg.get("is_alert", False)
        sc     = AGENT_COLORS.get(src, C["text2"])
        cls    = "console-line alert" if is_al else "console-line"
        lines_html += (
            f'<div class="{cls}">'
            f'<span style="color:{C["text3"]};">[{ts}]</span> '
            f'<span style="color:{sc};font-weight:600;">[{src}]</span> '
            f'<span style="color:{C["text"] if is_al else C["text2"]};">{action}</span>'
            f'<span style="color:{C["text3"]};"> — {detail}</span>'
            f'</div>'
        )

    components.html(
        f"""
        <style>
          @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400&display=swap');
          body {{ margin:0; padding:0; background:{C['bg_ticker']}; }}
          .console-wrap {{
            background:{C['bg_ticker']}; border:1px solid {C['border_b']};
            height:210px; overflow-y:auto; padding:8px 10px;
            font-family:'JetBrains Mono',monospace; font-size:11px; line-height:1.7;
          }}
          .console-line {{ padding:1px 4px; border-radius:2px; }}
          .console-line.alert {{
            border-left:3px solid {C['red']};
            background:rgba(255,51,102,0.06); padding-left:8px;
          }}
        </style>
        <div class="console-wrap" id="log">{lines_html}</div>
        <script>
          var el = document.getElementById('log');
          if(el) el.scrollTop = el.scrollHeight;
        </script>
        """,
        height=230,
    )


# ── Metrics snapshot table ─────────────────────────────────────────────────────
def _render_snapshot_table(all_metrics: List[Dict], if_alert_ts: set):
    if not all_metrics:
        st.markdown('<div class="terminal-empty">⚡ AWAITING SNAPSHOT DATA...</div>', unsafe_allow_html=True)
        return
    rows = []
    for m in all_metrics[:20]:
        ts = m.get("timestamp", 0)
        rows.append({
            "TIME":     _ts_fmt(ts),
            "TRADES":   int(m.get("total_trades", 0) or 0),
            "CUM PNL":  f'${m.get("pnl_cumulative",0) or 0:+,.0f}',
            "WIN RATE": f'{(m.get("win_rate_50",0) or 0):.1%}',
            "FILL RATE":f'{(m.get("fill_rate",1) or 1):.1%}',
            "SLIPPAGE": f'{(m.get("avg_slippage",0) or 0):.5f}',
            "DRAWDOWN": f'{(m.get("drawdown_current",0) or 0):.2%}',
            "SHARPE":   f'{(m.get("sharpe_24h",0) or 0):.2f}',
            "IF":       "⚠ ANOMALY" if ts in if_alert_ts else "NORMAL",
        })
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True, height=320)


# ── Regime + detection panel ───────────────────────────────────────────────────
def _render_regime_panel(regime_info: Dict, all_alerts: List[Dict],
                         if_count: int, total_metrics: int, baseline: int):
    regime     = regime_info.get("regime", "UNKNOWN")
    conf       = regime_info.get("confidence", 0) or 0
    reasoning  = regime_info.get("reasoning", "") or ""
    rc         = REGIME_COLORS.get(regime, C["text3"])

    conf_pct   = int(conf * 100)
    conf_bar   = (
        f'<div style="background:{C["border"]};height:4px;border-radius:2px;margin:6px 0 4px;">'
        f'<div style="background:{rc};width:{conf_pct}%;height:100%;border-radius:2px;"></div></div>'
    )

    st.markdown(
        f'<div class="terminal-panel">'
        f'<div class="terminal-label">REGIME CONTEXT · REGIMERADAR</div>'
        f'<div class="regime-big" style="color:{rc};">{regime}</div>'
        f'{conf_bar}'
        f'<div style="font-family:var(--mono);font-size:10px;color:{C["text3"]};">'
        f'CONFIDENCE: {conf_pct}%</div>'
        f'<div style="font-family:var(--mono);font-size:10px;color:{C["text2"]};'
        f'font-style:italic;margin-top:6px;">{reasoning[-120:]}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    st.markdown("<br>", unsafe_allow_html=True)

    if_status = (
        f'<span style="color:{C["green"]};">TRAINED ({total_metrics} obs)</span>'
        if if_count > 0 or total_metrics >= baseline else
        f'<span style="color:{C["amber"]};">CALIBRATING ({total_metrics}/{baseline})</span>'
    )
    z_today = sum(1 for a in all_alerts if a.get("detection_layer") == "zscore")

    st.markdown(
        f'<div class="detect-row">'
        f'<div class="detect-card">'
        f'<div class="detect-label">Z-SCORE LAYER</div>'
        f'<span style="color:{C["green"]};">ACTIVE</span>'
        f'<div style="color:{C["text3"]};font-size:10px;margin-top:3px;">'
        f'threshold 2.0σ · {z_today} triggers</div>'
        f'</div>'
        f'<div class="detect-card">'
        f'<div class="detect-label">ISOLATION FOREST</div>'
        f'{if_status}'
        f'<div style="color:{C["text3"]};font-size:10px;margin-top:3px;">'
        f'{if_count} multivariate detections</div>'
        f'</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # Anomaly type distribution
    type_counts: Dict[str, int] = {}
    for a in all_alerts:
        t = a.get("anomaly_type") or "unknown"
        type_counts[t] = type_counts.get(t, 0) + 1

    if type_counts:
        labels = list(type_counts.keys())
        values = list(type_counts.values())
        bar_colors = [C["red"], C["amber"], C["blue"], C["text3"], C["cyan"]][:len(labels)]
        fig = go.Figure(go.Bar(
            y=[l.replace("_", " ").upper() for l in labels],
            x=values, orientation="h",
            marker_color=bar_colors,
            text=values, textposition="outside",
            textfont=dict(size=9, color=C["text2"], family="JetBrains Mono"),
        ))
        fig.update_layout(
            height=120,
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor=C["bg_panel"],
            font=dict(family="JetBrains Mono", color=C["text2"], size=9),
            xaxis=dict(gridcolor=C["border"], showgrid=True),
            yaxis=dict(gridcolor=C["border"], showgrid=False),
            margin=dict(l=4, r=30, t=4, b=4),
        )
        st.markdown(
            f'<div style="font-family:var(--mono);font-size:9px;color:{C["text3"]};'
            f'letter-spacing:1px;margin:8px 0 2px;">ANOMALY TYPE DISTRIBUTION</div>',
            unsafe_allow_html=True,
        )
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


# ── Sidebar ────────────────────────────────────────────────────────────────────
def _render_sidebar(cfg, redis_client, regime_info, redis_ok, ollama_ok, groq_ok, db):
    st.sidebar.markdown(
        f'<div style="font-family:var(--mono);font-size:14px;font-weight:700;'
        f'color:{C["green"]};letter-spacing:2px;padding:8px 0;">WATCHDOG·AI</div>',
        unsafe_allow_html=True,
    )
    _sdiv("STATUS")
    rr_ok = Path(cfg["regimeradar"]["sqlite_path"]).exists()
    rows = [
        ("REDIS",       redis_ok,  "CONNECTED",    "OFFLINE"),
        ("OLLAMA",      ollama_ok, "RUNNING",      "OFFLINE"),
        ("GROQ",        groq_ok,   "CONFIGURED",   "NOT SET"),
        ("REGIMERADAR", rr_ok,     "LINKED",       "NOT FOUND"),
    ]
    html = ""
    for label, ok, yes, no in rows:
        dot = C["green"] if ok else (C["amber"] if label == "GROQ" else C["red"])
        val = yes if ok else no
        html += (
            f'<div style="font-family:var(--mono);font-size:11px;'
            f'display:flex;justify-content:space-between;padding:2px 0;">'
            f'<span style="color:{C["text3"]};">● {label}</span>'
            f'<span style="color:{dot};">{val}</span></div>'
        )
    st.sidebar.markdown(html, unsafe_allow_html=True)

    _sdiv("REGIME")
    regime = regime_info.get("regime", "UNKNOWN")
    rc = REGIME_COLORS.get(regime, C["text3"])
    conf = int((regime_info.get("confidence") or 0) * 100)
    st.sidebar.markdown(
        f'<div style="font-family:var(--mono);font-size:13px;font-weight:700;'
        f'color:{rc};letter-spacing:1px;">{regime}</div>'
        f'<div style="font-family:var(--mono);font-size:10px;color:{C["text3"]};">'
        f'CONFIDENCE: {conf}%</div>',
        unsafe_allow_html=True,
    )

    _sdiv("INJECT ANOMALY")
    st.sidebar.markdown(
        f'<div style="font-family:var(--mono);font-size:9px;color:{C["text3"]};">'
        f'TRIGGER SYNTHETIC ANOMALY FOR DEMO</div>',
        unsafe_allow_html=True,
    )
    bc1, bc2, bc3 = st.sidebar.columns(3)
    with bc1:
        if st.button("SLIP\nSPIKE", key="inj_slip"):
            _write_injection("slippage_spike")
            st.sidebar.success("INJECTED")
    with bc2:
        if st.button("WIN\nDECAY", key="inj_win"):
            _write_injection("win_rate_decay")
            st.sidebar.success("INJECTED")
    with bc3:
        if st.button("DWN\nBRCH", key="inj_dd"):
            _write_injection("drawdown_breach")
            st.sidebar.success("INJECTED")

    _sdiv("EXPORT")
    all_alerts = db.get_all_alerts(500) if hasattr(db, "get_all_alerts") else []
    if all_alerts:
        csv_data = _export_alerts_csv(all_alerts)
        if csv_data:
            st.sidebar.download_button(
                "⬇ EXPORT ALERTS CSV",
                data=csv_data,
                file_name=f"watchdog_{int(time.time())}.csv",
                mime="text/csv",
            )


def _sdiv(label: str):
    st.sidebar.markdown(
        f'<div class="bbg-divider">{label}</div>',
        unsafe_allow_html=True,
    )


def _write_injection(injection_type: str):
    """Write injection request to a temp file polled by main.py."""
    try:
        (ROOT / ".injection_request").write_text(injection_type)
    except Exception:
        pass


def _export_alerts_csv(alerts: List[Dict]) -> str:
    output = io.StringIO()
    if not alerts:
        return ""
    fields = ["id","timestamp","metric_breached","z_score","anomaly_type",
              "severity","confidence","reasoning","regime_context","backend_used",
              "detection_layer","resolved"]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for a in alerts:
        row = {k: a.get(k, "") for k in fields}
        row["timestamp"] = _ts_fmt(a.get("timestamp", 0))
        writer.writerow(row)
    return output.getvalue()


def _format_payload_detail(payload: Dict, msg_type: str) -> str:
    if msg_type == "metrics_snapshot":
        m = payload.get("metrics", {})
        return (f"trades={payload.get('total_trades','?')} "
                f"pnl={m.get('pnl_cumulative',0):+.2f} "
                f"wr={m.get('win_rate_50',0):.1%}")
    if msg_type == "anomaly_event":
        layer = payload.get("detection_layer", "zscore")
        return f"{payload.get('metric','?')} z={payload.get('z_score',0):+.2f}σ [{layer}]"
    if msg_type == "diagnosis_result":
        return (f"{payload.get('anomaly_type','?')} sev={payload.get('severity','?')} "
                f"conf={payload.get('confidence',0):.0%} via {payload.get('backend_used','?')}")
    if msg_type == "action_recommendation":
        return f"{payload.get('routing','?')} — {str(payload.get('user_message',''))[:80]}"
    return str(payload)[:100]


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    st.markdown(GLOBAL_CSS, unsafe_allow_html=True)

    cfg        = _load_config()
    cfg_hash   = id(cfg)
    redis_c    = _get_redis(cfg_hash)
    db         = _get_db(cfg_hash)

    # System checks
    redis_ok  = redis_c.ping() if hasattr(redis_c, "ping") else redis_c.connected
    ollama_ok = False
    try:
        import requests as _req
        ollama_ok = _req.get(f"{cfg['ollama']['base_url']}/api/tags", timeout=2).status_code == 200
    except Exception:
        pass
    groq_ok = bool(cfg.get("groq", {}).get("api_key", ""))

    # Data
    regime_info  = _get_regime(cfg, redis_c)
    all_metrics  = db.get_recent_metrics(100)
    active_alerts= db.get_active_alerts(10)
    try:
        all_alerts = db.get_all_alerts(500)
    except Exception:
        all_alerts = active_alerts

    if_alerts    = [a for a in all_alerts if a.get("detection_layer") == "isolation_forest"]
    if_count     = len(if_alerts)
    if_alert_ts  = {a.get("timestamp") for a in if_alerts}
    baseline     = cfg.get("metrics", {}).get("baseline_trades", 50)
    total_metrics= len(all_metrics)

    latest_m     = all_metrics[0] if all_metrics else {}
    backend      = (all_alerts[0].get("backend_used") if all_alerts else
                    latest_m.get("backend_used", "—"))

    # Activity log from Redis
    activity_msgs = []
    stream_source = {
        cfg["redis"]["streams"]["bot_metrics"]:  "MetricsAgent",
        cfg["redis"]["streams"]["anomalies"]:     "MetricsAgent",
        cfg["redis"]["streams"]["diagnoses"]:     "DiagnosisAgent",
        cfg["redis"]["streams"]["actions"]:       "ActionAgent",
    }
    for stream, source in stream_source.items():
        try:
            for msg in redis_c.read_stream_latest(stream, 20):
                ts = float(msg.get("timestamp", 0))
                p  = msg.get("payload", {})
                mt = msg.get("message_type", "")
                activity_msgs.append({
                    "source":    source,
                    "action":    mt.replace("_", " ").title(),
                    "detail":    _format_payload_detail(p, mt),
                    "timestamp": ts,
                    "is_alert":  "anomaly" in stream or "diagnos" in stream,
                })
        except Exception:
            pass
    activity_msgs.sort(key=lambda x: x["timestamp"])

    alert_timestamps = [a.get("timestamp", 0) for a in all_alerts]

    # ── Render ──
    _render_ticker(latest_m, regime_info, backend)
    _render_header(redis_ok, ollama_ok, groq_ok)
    _render_sidebar(cfg, redis_c, regime_info, redis_ok, ollama_ok, groq_ok, db)

    # Zone 2 — metric panels
    if all_metrics:
        _render_metric_panels(all_metrics)
    else:
        st.markdown(
            f'<div class="terminal-empty">⚡ CALIBRATING... WAITING FOR BASELINE '
            f'({total_metrics}/{baseline} TRADES)</div>',
            unsafe_allow_html=True,
        )
    st.markdown(f'<hr style="border:none;border-top:1px solid {C["border"]};margin:10px 0;">', unsafe_allow_html=True)

    # Zone 3 — main grid
    col_l, col_r = st.columns([6, 4])
    with col_l:
        st.markdown(
            f'<div class="terminal-label" style="margin-bottom:6px;">BTC/USDT · 1H · EMA 9/21</div>',
            unsafe_allow_html=True,
        )
        _render_price_chart(alert_timestamps)
        _render_metrics_timeline(all_metrics)
    with col_r:
        _render_alerts(active_alerts, db)

    st.markdown(f'<hr style="border:none;border-top:1px solid {C["border"]};margin:10px 0;">', unsafe_allow_html=True)

    # Zone 4 — agent log
    _render_agent_log(activity_msgs)
    st.markdown(f'<hr style="border:none;border-top:1px solid {C["border"]};margin:10px 0;">', unsafe_allow_html=True)

    # Zone 5 — bottom
    col_b1, col_b2 = st.columns(2)
    with col_b1:
        st.markdown(
            f'<div class="terminal-label" style="margin-bottom:6px;">METRICS SNAPSHOTS · LAST 20</div>',
            unsafe_allow_html=True,
        )
        _render_snapshot_table(all_metrics, if_alert_ts)
    with col_b2:
        _render_regime_panel(regime_info, all_alerts, if_count, total_metrics, baseline)

    # Auto-refresh
    time.sleep(10)
    st.rerun()


if __name__ == "__main__":
    main()