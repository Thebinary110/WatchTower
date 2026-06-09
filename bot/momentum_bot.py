"""Momentum bot: replays BTC/USDT 1H candles with EMA crossover + RSI strategy."""

import logging
import queue
import time
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

import ccxt
import numpy as np
import pandas as pd
import pandas_ta as ta

logger = logging.getLogger(__name__)


def _fetch_ohlcv(cfg: Dict) -> pd.DataFrame:
    """Fetch historical OHLCV from Binance public endpoints (no auth required)."""
    symbol = cfg["binance"]["symbol"]
    timeframe = cfg["binance"]["timeframe"]
    days = cfg["binance"]["history_days"]

    exchange = ccxt.binance({
        "enableRateLimit": True,
        "fetchCurrencies": False,
        "options": {
            "fetchCurrencies": False,
            "defaultType": "spot",
            "adjustForTimeDifference": True,
        },
    })

    since_ms = int((datetime.now().timestamp() - days * 86400) * 1000)
    all_candles = []
    max_retries = 3

    for attempt in range(max_retries):
        try:
            since = since_ms
            while True:
                candles = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=500)
                if not candles:
                    break
                all_candles.extend(candles)
                if len(candles) < 500:
                    break
                since = candles[-1][0] + 1
                time.sleep(0.2)
            break
        except Exception as e:
            logger.warning(f"Binance fetch attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.info(f"Retrying in {wait}s...")
                time.sleep(wait)
            else:
                logger.error("All Binance fetch attempts failed")
                if all_candles:
                    logger.info("Using partial cached data")
                else:
                    raise

    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    logger.info(f"Fetched {len(df)} candles for {symbol} {timeframe}")
    return df


def _compute_signals(df: pd.DataFrame, cfg: Dict) -> pd.DataFrame:
    """Add EMA, RSI, and entry/exit signals to dataframe."""
    ema_fast = cfg["bot"]["ema_fast"]
    ema_slow = cfg["bot"]["ema_slow"]
    rsi_period = cfg["bot"]["rsi_period"]
    rsi_threshold = cfg["bot"]["rsi_threshold"]

    df = df.copy()
    df[f"ema{ema_fast}"] = ta.ema(df["close"], length=ema_fast)
    df[f"ema{ema_slow}"] = ta.ema(df["close"], length=ema_slow)
    df["rsi"] = ta.rsi(df["close"], length=rsi_period)

    prev_fast = df[f"ema{ema_fast}"].shift(1)
    prev_slow = df[f"ema{ema_slow}"].shift(1)

    df["bullish_cross"] = (df[f"ema{ema_fast}"] > df[f"ema{ema_slow}"]) & (prev_fast <= prev_slow)
    df["bearish_cross"] = (df[f"ema{ema_fast}"] < df[f"ema{ema_slow}"]) & (prev_fast >= prev_slow)
    df["rsi_ok"] = df["rsi"] > rsi_threshold

    df["entry_signal"] = df["bullish_cross"] & df["rsi_ok"]
    df["exit_signal"] = df["bearish_cross"]
    return df.dropna().reset_index(drop=True)


def _simulate_trades(df: pd.DataFrame, cfg: Dict) -> List[Dict]:
    """Run strategy simulation, return list of completed trades."""
    stop_loss_pct = cfg["bot"]["stop_loss_pct"]
    risk_pct = cfg["bot"]["risk_per_trade_pct"]
    capital = cfg["bot"]["initial_capital"]
    slippage_mean = 0.0005
    slippage_std = 0.0002

    trades = []
    in_trade = False
    entry_price = 0.0
    entry_idx = 0
    position_size = 0.0
    entry_slippage = 0.0
    current_capital = capital

    for i, row in df.iterrows():
        if not in_trade:
            if row["entry_signal"]:
                slippage = max(0.0, np.random.normal(slippage_mean, slippage_std))
                fill_price = row["close"] * (1 + slippage)
                risk_amount = current_capital * risk_pct
                position_size = risk_amount / (fill_price * stop_loss_pct)
                entry_price = fill_price
                entry_idx = i
                entry_slippage = slippage
                in_trade = True
        else:
            stop_hit = row["low"] <= entry_price * (1 - stop_loss_pct)
            exit_sig = row["exit_signal"]

            if stop_hit or exit_sig:
                slippage = max(0.0, np.random.normal(slippage_mean, slippage_std))

                if stop_hit:
                    exit_price = entry_price * (1 - stop_loss_pct) * (1 - slippage)
                else:
                    exit_price = row["close"] * (1 - slippage)

                pnl = (exit_price - entry_price) * position_size
                avg_slippage = (entry_slippage + slippage) / 2
                current_capital += pnl

                trades.append({
                    "trade_idx": len(trades),
                    "entry_time": df.loc[entry_idx, "timestamp"],
                    "exit_time": row["timestamp"],
                    "direction": "LONG",
                    "entry_price": round(entry_price, 2),
                    "exit_price": round(exit_price, 2),
                    "position_size": round(position_size, 6),
                    "pnl": round(pnl, 4),
                    "slippage": round(avg_slippage, 6),
                    "orders_submitted": 2,
                    "orders_filled": 2,
                    "fill_rate": 1.0,
                    "won": pnl > 0,
                    "capital_after": round(current_capital, 2),
                    "exit_reason": "stop_loss" if stop_hit else "signal",
                })
                in_trade = False

    return trades


class MomentumBot:
    """Replays historical trades at configurable speed, calling trade_callback per trade."""

    def __init__(self, config: Dict, trade_callback: Callable[[Dict], None]):
        self.config = config
        self.trade_callback = trade_callback
        self.replay_speed = config["binance"]["replay_speed_seconds"]
        self._stop = False
        self._trades: List[Dict] = []
        self._trade_idx = 0
        self.status = "stopped"

    def load(self) -> None:
        """Fetch data and generate trades. Call before start()."""
        logger.info("Fetching historical data...")
        df = _fetch_ohlcv(self.config)
        df = _compute_signals(df, self.config)
        self._trades = _simulate_trades(df, self.config)
        logger.info(f"Generated {len(self._trades)} trades from historical data")

    def start(self) -> None:
        """Replay trades in real-time. Blocking — run in a thread."""
        self._stop = False
        self.status = "running"
        logger.info(f"Bot replay started: {len(self._trades)} trades at {self.replay_speed}s/trade")
        for trade in self._trades:
            if self._stop:
                break
            self.trade_callback(trade)
            time.sleep(self.replay_speed)
        self.status = "stopped"
        logger.info("Bot replay completed")

    def stop(self) -> None:
        self._stop = True
        self.status = "stopped"

    @property
    def total_trades(self) -> int:
        return len(self._trades)


if __name__ == "__main__":
    import yaml
    from pathlib import Path

    logging.basicConfig(level=logging.INFO)
    cfg_path = Path(__file__).parent.parent / "config.yaml"
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    trades_seen = []

    def on_trade(t: Dict) -> None:
        trades_seen.append(t)
        print(f"Trade {t['trade_idx']:4d}: PnL={t['pnl']:+.2f}  capital={t['capital_after']:.2f}  slippage={t['slippage']:.5f}")
        if len(trades_seen) >= 5:
            raise SystemExit("First 5 trades printed — momentum_bot OK")

    bot = MomentumBot(cfg, on_trade)
    bot.load()
    cfg["binance"]["replay_speed_seconds"] = 0.05
    bot.replay_speed = 0.05
    bot.start()