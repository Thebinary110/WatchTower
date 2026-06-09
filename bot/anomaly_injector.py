"""Injects synthetic anomalies into the trade stream at configurable trade indices."""

import logging
import random
from typing import Dict

logger = logging.getLogger(__name__)


class AnomalyInjector:
    """Wraps a trade dict and modifies fields to simulate anomalies."""

    def __init__(self, config: Dict):
        self.config = config
        self.enabled = config["anomaly_injection"]["enabled"]
        self._slippage_cfg = config["anomaly_injection"]["slippage_spike"]
        self._win_decay_cfg = config["anomaly_injection"]["win_rate_decay"]
        self._overtrade_cfg = config["anomaly_injection"]["overtrading"]
        self._drawdown_cfg = config["anomaly_injection"]["drawdown_breach"]
        self._manual_anomaly: Dict = {}  # type → active flag for dashboard-triggered anomalies
        self._manual_duration: Dict = {}  # type → remaining trade count
        self._injected_log = []

    def inject(self, trade: Dict) -> Dict:
        """Modify trade fields based on injection schedule. Returns modified trade."""
        if not self.enabled:
            return trade

        idx = trade["trade_idx"]
        trade = dict(trade)  # don't mutate original

        # Slippage spike
        ss = self._slippage_cfg
        if ss["trigger_trade"] <= idx < ss["trigger_trade"] + ss["duration_trades"]:
            original = trade["slippage"]
            trade["slippage"] = original * ss["multiplier"]
            # Adjust exit price to reflect worsened slippage
            slippage_delta = trade["slippage"] - original
            trade["exit_price"] = round(trade["exit_price"] * (1 - slippage_delta), 2)
            pnl_delta = -slippage_delta * trade["entry_price"] * trade["position_size"]
            trade["pnl"] = round(trade["pnl"] + pnl_delta, 4)
            trade["capital_after"] = round(trade["capital_after"] + pnl_delta, 2)
            trade["won"] = trade["pnl"] > 0
            if idx == ss["trigger_trade"]:
                logger.info(f"[INJECTOR] Slippage spike started at trade {idx}")
                self._injected_log.append({"trade_idx": idx, "type": "slippage_spike"})

        # Win rate decay: flip some winners to losers
        wd = self._win_decay_cfg
        if wd["trigger_trade"] <= idx < wd["trigger_trade"] + wd["duration_trades"]:
            if trade["won"] and random.random() < wd["flip_probability"]:
                loss = abs(trade["pnl"]) * 1.5
                trade["pnl"] = round(-loss, 4)
                trade["won"] = False
                trade["exit_price"] = round(trade["entry_price"] * (1 - self.config["bot"]["stop_loss_pct"]), 2)
                trade["capital_after"] = round(trade["capital_after"] - abs(trade["pnl"]) * 2, 2)
            if idx == wd["trigger_trade"]:
                logger.info(f"[INJECTOR] Win rate decay started at trade {idx}")
                self._injected_log.append({"trade_idx": idx, "type": "win_rate_decay"})

        # Overtrading: reduce fill rate and increase frequency signal
        ot = self._overtrade_cfg
        if ot["trigger_trade"] <= idx < ot["trigger_trade"] + ot["duration_trades"]:
            # Simulate partial fills degrading fill rate
            trade["orders_submitted"] = int(trade["orders_submitted"] * ot["frequency_multiplier"])
            filled = max(1, int(trade["orders_filled"] * 0.6))
            trade["orders_filled"] = filled
            trade["fill_rate"] = filled / trade["orders_submitted"]
            if idx == ot["trigger_trade"]:
                logger.info(f"[INJECTOR] Overtrading started at trade {idx}")
                self._injected_log.append({"trade_idx": idx, "type": "overtrading"})

        # Drawdown breach: force consecutive losses
        db = self._drawdown_cfg
        consec = db["consecutive_losses"]
        if db["trigger_trade"] <= idx < db["trigger_trade"] + consec:
            if trade["won"]:
                loss = abs(trade["pnl"]) * 2.0
                trade["pnl"] = round(-loss, 4)
                trade["won"] = False
                trade["exit_price"] = round(trade["entry_price"] * (1 - self.config["bot"]["stop_loss_pct"]), 2)
                trade["capital_after"] = round(trade["capital_after"] - loss * 2, 2)
            if idx == db["trigger_trade"]:
                logger.info(f"[INJECTOR] Drawdown breach started at trade {idx}")
                self._injected_log.append({"trade_idx": idx, "type": "drawdown_breach"})

        # Manual dashboard-triggered anomalies
        trade = self._apply_manual(trade)
        return trade

    def trigger_manual(self, anomaly_type: str, duration_trades: int = 20):
        """Called from dashboard sidebar to inject an anomaly immediately."""
        self._manual_anomaly[anomaly_type] = True
        self._manual_duration[anomaly_type] = duration_trades
        logger.info(f"[INJECTOR] Manual anomaly triggered: {anomaly_type} for {duration_trades} trades")

    def _apply_manual(self, trade: Dict) -> Dict:
        for atype in list(self._manual_anomaly.keys()):
            if not self._manual_anomaly.get(atype):
                continue
            remaining = self._manual_duration.get(atype, 0)
            if remaining <= 0:
                self._manual_anomaly[atype] = False
                continue

            if atype == "slippage_spike":
                trade["slippage"] = trade["slippage"] * self._slippage_cfg["multiplier"]
                pnl_delta = -trade["slippage"] * trade["entry_price"] * trade["position_size"]
                trade["pnl"] = round(trade["pnl"] + pnl_delta, 4)
                trade["won"] = trade["pnl"] > 0
            elif atype == "win_rate_decay":
                if trade["won"] and random.random() < 0.5:
                    loss = abs(trade["pnl"]) * 1.5
                    trade["pnl"] = round(-loss, 4)
                    trade["won"] = False
            elif atype == "drawdown_breach":
                if trade["won"]:
                    loss = abs(trade["pnl"]) * 2.0
                    trade["pnl"] = round(-loss, 4)
                    trade["won"] = False

            self._manual_duration[atype] = remaining - 1
        return trade
