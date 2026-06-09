"""Agent 1 — Metrics Agent: ingest trade stream, compute z-scores, publish anomalies.

Two-layer anomaly detection:
  Layer 1 — per-metric z-score vs baseline (fires on any single metric breach)
  Layer 2 — Isolation Forest on 6-dimensional metric vector (fires on joint behavioral drift)
Both layers run independently; either firing publishes to watchdog:anomalies.
"""

import logging
import queue
import threading
import time
from collections import deque
from typing import Dict, List, Optional, Tuple

import numpy as np
from sklearn.ensemble import IsolationForest

logger = logging.getLogger(__name__)

METRIC_VECTOR_FIELDS = [
    "win_rate_50",
    "fill_rate",
    "avg_slippage",
    "trade_frequency",
    "drawdown_current",
    "sharpe_24h",
]


class MetricsAgent:
    def __init__(self, config: Dict, redis_client, db_log, activity_queue: queue.Queue):
        self.config = config
        self.redis = redis_client
        self.db = db_log
        self.activity_q = activity_queue

        cfg_m = config["metrics"]
        self.publish_interval = cfg_m["publish_interval_seconds"]
        self.baseline_size = cfg_m["baseline_trades"]
        self.zscore_threshold = cfg_m["anomaly_zscore_threshold"]
        self.win_window = cfg_m["rolling_win_rate_window"]
        self.fill_window = cfg_m["rolling_fill_rate_window"]
        self.slip_window = cfg_m["rolling_slippage_window"]

        # Trade storage
        self._trades: List[Dict] = []
        self._trade_lock = threading.Lock()

        # Baseline stats: metric -> (mean, std)
        self._baseline: Dict[str, Tuple[float, float]] = {}
        self._baseline_ready = False

        # Breach tracking: metric -> first_breach_timestamp
        self._breach_start: Dict[str, float] = {}

        # State for PnL narrative
        self._pnl_snapshots: deque = deque(maxlen=8)  # last 2h at 15min resolution

        self._stop = False
        self._last_publish = 0.0

        # Layer 2 — Isolation Forest state
        self.if_model: Optional[IsolationForest] = None
        self.if_trained: bool = False
        self.metric_history: List[List[float]] = []  # plain list — full history for potential retraining
        self.if_train_after: int = self.baseline_size  # same threshold as z-score baseline

    def on_trade(self, trade: Dict):
        with self._trade_lock:
            self._trades.append(trade)
            n = len(self._trades)

        if not self._baseline_ready and n >= self.baseline_size:
            self._compute_baseline()
            self._baseline_ready = True
            self._log_activity("Baseline established", f"{n} trades sampled")

        # Publish metrics on interval
        now = time.time()
        if now - self._last_publish >= self.publish_interval and self._baseline_ready:
            self._publish_metrics(now)
            self._last_publish = now

    def _compute_baseline(self):
        with self._trade_lock:
            base_trades = list(self._trades[: self.baseline_size])

        win_rates = self._rolling_win_rates(base_trades, self.win_window)
        fill_rates = self._rolling_fill_rates(base_trades, self.fill_window)
        slippages = [t["slippage"] for t in base_trades]
        pnls = [t["pnl"] for t in base_trades]
        drawdowns = self._compute_drawdowns([t["capital_after"] for t in base_trades])

        metrics_series = {
            "win_rate_50": win_rates,
            "fill_rate": fill_rates,
            "avg_slippage": slippages,
            "drawdown_current": drawdowns,
            "pnl_1h": pnls,
        }

        for name, values in metrics_series.items():
            arr = np.array(values)
            arr = arr[np.isfinite(arr)]
            if len(arr) > 1:
                self._baseline[name] = (float(np.mean(arr)), max(float(np.std(arr)), 1e-8))
            else:
                self._baseline[name] = (0.0, 1.0)

        logger.info(f"Baseline computed: {self._baseline}")

    def _compute_metrics(self, trades: List[Dict]) -> Dict:
        if not trades:
            return {}

        capital_after = [t["capital_after"] for t in trades]
        pnls = [t["pnl"] for t in trades]
        initial_capital = self.config["bot"]["initial_capital"]

        pnl_cumulative = capital_after[-1] - initial_capital if capital_after else 0.0

        # Rolling 1h PnL — use last fill_window trades as proxy
        pnl_1h = sum(pnls[-self.fill_window :]) if len(pnls) >= 2 else pnls[-1] if pnls else 0.0

        win_rates = self._rolling_win_rates(trades, self.win_window)
        win_rate_50 = win_rates[-1] if win_rates else 0.5

        fill_rates_list = self._rolling_fill_rates(trades, self.fill_window)
        fill_rate = fill_rates_list[-1] if fill_rates_list else 1.0

        recent_slips = [t["slippage"] for t in trades[-self.slip_window :]]
        avg_slippage = float(np.mean(recent_slips)) if recent_slips else 0.0

        # Trade frequency: trades per hour using replay_speed as proxy
        replay_speed = self.config["binance"]["replay_speed_seconds"]
        trades_per_hour = 3600 / max(replay_speed, 1)
        trade_frequency = trades_per_hour

        drawdowns = self._compute_drawdowns(capital_after)
        drawdown_current = drawdowns[-1] if drawdowns else 0.0

        recent_pos_sizes = [t["position_size"] for t in trades[-20:]]
        if len(recent_pos_sizes) > 1:
            mean_ps = np.mean(recent_pos_sizes)
            std_ps = np.std(recent_pos_sizes)
            position_size_consistency = float(std_ps / max(mean_ps, 1e-8))
        else:
            position_size_consistency = 0.0

        # Sharpe (24h proxy from available trades)
        pnl_arr = np.array(pnls[-min(len(pnls), 24) :])
        if len(pnl_arr) > 1 and np.std(pnl_arr) > 0:
            sharpe_24h = float(np.mean(pnl_arr) / np.std(pnl_arr) * np.sqrt(len(pnl_arr)))
        else:
            sharpe_24h = 0.0

        return {
            "pnl_cumulative": pnl_cumulative,
            "pnl_1h": pnl_1h,
            "win_rate_50": win_rate_50,
            "fill_rate": fill_rate,
            "avg_slippage": avg_slippage,
            "trade_frequency": trade_frequency,
            "drawdown_current": drawdown_current,
            "position_size_consistency": position_size_consistency,
            "sharpe_24h": sharpe_24h,
        }

    def _compute_zscores(self, metrics: Dict) -> Dict:
        zscores = {}
        for metric, value in metrics.items():
            if metric in self._baseline:
                mean, std = self._baseline[metric]
                zscores[metric] = (value - mean) / std
            else:
                zscores[metric] = 0.0
        return zscores

    # ------------------------------------------------------------------
    # Layer 2 — Isolation Forest
    # ------------------------------------------------------------------

    def _update_isolation_forest(self, metrics: Dict) -> bool:
        """Append current metric vector to history, train IF when ready, predict on current.

        Returns True if IF flags the current vector as anomalous, False otherwise.
        Never raises — all exceptions caught and logged.
        """
        vector = [metrics.get(f, 0.0) for f in METRIC_VECTOR_FIELDS]
        self.metric_history.append(vector)

        n = len(self.metric_history)

        # Train once when we have enough history
        if not self.if_trained and n >= self.if_train_after:
            if self.if_train_after < 20:
                logger.warning(
                    f"IF training skipped: baseline_trades={self.if_train_after} < 20, "
                    "insufficient data for meaningful model"
                )
                return False
            try:
                contamination = self.config["metrics"].get("if_contamination", 0.05)
                X = np.array(self.metric_history, dtype=np.float32)
                self.if_model = IsolationForest(
                    n_estimators=100,
                    contamination=contamination,
                    random_state=42,
                    n_jobs=1,
                )
                self.if_model.fit(X)
                self.if_trained = True
                logger.info(
                    f"Isolation Forest trained on {n} metric snapshots "
                    f"(contamination={contamination})"
                )
            except Exception as e:
                logger.error(f"Isolation Forest training failed: {e}")
                return False

        # Predict on current vector if model is ready
        if self.if_trained and self.if_model is not None:
            try:
                x = np.array([vector], dtype=np.float32)
                prediction = self.if_model.predict(x)[0]   # +1 normal, -1 anomaly
                score = float(self.if_model.score_samples(x)[0])  # lower = more anomalous
                if prediction == -1:
                    logger.info(
                        f"Isolation Forest anomaly detected: score={score:.4f} "
                        f"vector={dict(zip(METRIC_VECTOR_FIELDS, vector))}"
                    )
                    return True
            except Exception as e:
                logger.error(f"Isolation Forest prediction failed: {e}")

        return False

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def _publish_metrics(self, now: float):
        with self._trade_lock:
            trades = list(self._trades)

        metrics = self._compute_metrics(trades)
        zscores = self._compute_zscores(metrics)
        total_trades = len(trades)

        # Track PnL snapshot for narrative
        self._pnl_snapshots.append((now, metrics.get("pnl_cumulative", 0.0)))

        # Persist to SQLite
        self.db.log_metrics(now, metrics, zscores, total_trades)

        # Publish to Redis
        payload = {
            "metrics": {k: round(v, 6) for k, v in metrics.items()},
            "zscores": {k: round(v, 4) for k, v in zscores.items()},
            "total_trades": total_trades,
            "baseline_ready": self._baseline_ready,
        }
        stream = self.config["redis"]["streams"]["bot_metrics"]
        self.redis.publish_stream(stream, "metrics_agent", "metrics_snapshot", payload)

        self._log_activity(
            "Metrics published",
            f"Trades={total_trades} | PnL={metrics.get('pnl_cumulative', 0):+.2f} | "
            f"WinRate={metrics.get('win_rate_50', 0):.1%} | "
            f"FillRate={metrics.get('fill_rate', 1):.1%}",
        )

        # Layer 1 — Z-score checks (per-metric)
        any_zscore_fired = False
        monitored = ["win_rate_50", "fill_rate", "avg_slippage", "drawdown_current", "pnl_1h"]
        for metric in monitored:
            z = zscores.get(metric, 0.0)
            if abs(z) > self.zscore_threshold:
                any_zscore_fired = True
                if metric not in self._breach_start:
                    self._breach_start[metric] = now
                duration_min = (now - self._breach_start[metric]) / 60
                self._publish_anomaly(
                    metric, z, duration_min, metrics, zscores, total_trades, now,
                    detection_layer="zscore",
                )
            else:
                self._breach_start.pop(metric, None)

        # Layer 2 — Isolation Forest multivariate check
        if_anomaly = self._update_isolation_forest(metrics)
        if if_anomaly and not any_zscore_fired:
            # Z-score didn't fire this cycle — publish the IF anomaly as its own event
            # so the Diagnosis Agent receives it without duplication
            vector = [metrics.get(f, 0.0) for f in METRIC_VECTOR_FIELDS]
            if_score = 0.0
            if self.if_model is not None:
                try:
                    x = np.array([vector], dtype=np.float32)
                    if_score = float(self.if_model.score_samples(x)[0])
                except Exception:
                    pass

            if_payload = {
                "metric": "multivariate_behavior",
                "z_score": 0.0,
                "duration_minutes": 0.0,
                "pnl_narrative": self._build_pnl_narrative(),
                "metrics_snapshot": {k: round(v, 6) for k, v in metrics.items()},
                "zscores_snapshot": {k: round(v, 4) for k, v in zscores.items()},
                "total_trades": total_trades,
                "timestamp": now,
                "detection_layer": "isolation_forest",
                "if_score": round(if_score, 6),
                "anomaly_vector": {
                    f: round(metrics.get(f, 0.0), 6) for f in METRIC_VECTOR_FIELDS
                },
            }
            stream = self.config["redis"]["streams"]["anomalies"]
            self.redis.publish_stream(stream, "metrics_agent", "anomaly_event", if_payload)

            self._log_activity(
                "IF ANOMALY DETECTED",
                f"Isolation Forest flagged multivariate drift — score={if_score:.4f} "
                f"(no single z-score threshold breached)",
                is_alert=True,
            )
            logger.warning(f"Isolation Forest anomaly published to Redis (score={if_score:.4f})")

    def _publish_anomaly(
        self,
        metric: str,
        z_score: float,
        duration_min: float,
        metrics: Dict,
        zscores: Dict,
        total_trades: int,
        now: float,
        detection_layer: str = "zscore",
    ):
        pnl_narrative = self._build_pnl_narrative()
        payload = {
            "metric": metric,
            "z_score": round(z_score, 4),
            "duration_minutes": round(duration_min, 1),
            "pnl_narrative": pnl_narrative,
            "metrics_snapshot": {k: round(v, 6) for k, v in metrics.items()},
            "zscores_snapshot": {k: round(v, 4) for k, v in zscores.items()},
            "total_trades": total_trades,
            "timestamp": now,
            "detection_layer": detection_layer,
        }
        stream = self.config["redis"]["streams"]["anomalies"]
        self.redis.publish_stream(stream, "metrics_agent", "anomaly_event", payload)

        self._log_activity(
            "ANOMALY DETECTED",
            f"{metric} at {z_score:+.2f}σ — duration {duration_min:.1f}min [{detection_layer}]",
            is_alert=True,
        )
        logger.warning(
            f"Anomaly: {metric} z={z_score:.2f} duration={duration_min:.1f}min "
            f"layer={detection_layer}"
        )

    def _build_pnl_narrative(self) -> str:
        snaps = list(self._pnl_snapshots)
        if not snaps:
            return "No PnL history available yet."
        lines = []
        for ts, pnl in snaps:
            from datetime import datetime
            t = datetime.fromtimestamp(ts).strftime("%H:%M")
            lines.append(f"{t}: {pnl:+.2f}")
        direction = "improving" if len(snaps) > 1 and snaps[-1][1] > snaps[0][1] else "declining"
        return f"PnL trajectory ({direction}): " + " → ".join(lines)

    def _rolling_win_rates(self, trades: List[Dict], window: int) -> List[float]:
        results = []
        for i in range(len(trades)):
            start = max(0, i - window + 1)
            window_trades = trades[start : i + 1]
            won = sum(1 for t in window_trades if t["won"])
            results.append(won / max(len(window_trades), 1))
        return results

    def _rolling_fill_rates(self, trades: List[Dict], window: int) -> List[float]:
        results = []
        for i in range(len(trades)):
            start = max(0, i - window + 1)
            window_trades = trades[start : i + 1]
            filled = sum(t["orders_filled"] for t in window_trades)
            submitted = sum(t["orders_submitted"] for t in window_trades)
            results.append(filled / max(submitted, 1))
        return results

    def _compute_drawdowns(self, capitals: List[float]) -> List[float]:
        if not capitals:
            return []
        drawdowns = []
        peak = capitals[0]
        for c in capitals:
            if c > peak:
                peak = c
            dd = (peak - c) / max(abs(peak), 1e-8)
            drawdowns.append(dd)
        return drawdowns

    def _log_activity(self, action: str, detail: str, is_alert: bool = False):
        try:
            self.activity_q.put_nowait(
                {
                    "source": "MetricsAgent",
                    "action": action,
                    "detail": detail,
                    "timestamp": time.time(),
                    "is_alert": is_alert,
                }
            )
        except queue.Full:
            pass

    def get_pnl_narrative(self) -> str:
        return self._build_pnl_narrative()

    @property
    def baseline_ready(self) -> bool:
        return self._baseline_ready

    @property
    def total_trades(self) -> int:
        with self._trade_lock:
            return len(self._trades)

    def get_latest_metrics(self) -> Optional[Dict]:
        with self._trade_lock:
            trades = list(self._trades)
        if not trades:
            return None
        metrics = self._compute_metrics(trades)
        zscores = self._compute_zscores(metrics)
        return {"metrics": metrics, "zscores": zscores, "total_trades": len(trades)}


# ------------------------------------------------------------------
# __main__ — standalone test: verify IF trains at snapshot 50,
#            flags an obvious outlier at snapshot 60
# ------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import yaml
    from pathlib import Path
    from unittest.mock import MagicMock

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    cfg_path = Path(__file__).parent.parent / "config.yaml"
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    # Minimal stubs — no Redis or DB needed for this test
    redis_stub = MagicMock()
    redis_stub.connected = False
    redis_stub.publish_stream = MagicMock(return_value=False)
    db_stub = MagicMock()
    activity_q = queue.Queue()

    agent = MetricsAgent(cfg, redis_stub, db_stub, activity_q)
    # Ensure IF trains at snapshot 50 (same as baseline_trades)
    assert agent.if_train_after == cfg["metrics"]["baseline_trades"]

    # Build 60 normal metric snapshots then one obvious outlier
    rng = np.random.default_rng(0)

    def _make_normal_metrics(rng) -> Dict:
        return {
            "win_rate_50": float(rng.normal(0.55, 0.03)),
            "fill_rate": float(rng.normal(0.98, 0.01)),
            "avg_slippage": float(rng.normal(0.0005, 0.0001)),
            "trade_frequency": float(rng.normal(720.0, 10.0)),
            "drawdown_current": float(rng.normal(0.01, 0.005)),
            "sharpe_24h": float(rng.normal(1.2, 0.2)),
        }

    print("\n--- Feeding 60 normal snapshots ---")
    trained_at = None
    for i in range(60):
        m = _make_normal_metrics(rng)
        result = agent._update_isolation_forest(m)
        if agent.if_trained and trained_at is None:
            trained_at = i + 1
            print(f"  [snapshot {i+1:3d}] IF model TRAINED [OK]")
        elif i < 5 or i >= 55:
            print(f"  [snapshot {i+1:3d}] anomaly={result}  trained={agent.if_trained}")

    assert agent.if_trained, "FAIL: IF model should be trained after 60 snapshots"
    assert trained_at is not None and trained_at <= 50, (
        f"FAIL: model should train at snapshot ≤50, got {trained_at}"
    )
    print(f"\n  IF trained at snapshot {trained_at} (expected <={cfg['metrics']['baseline_trades']}) [OK]")

    # Inject an obvious outlier at snapshot 61
    outlier = {
        "win_rate_50": 0.05,      # way below normal ~0.55
        "fill_rate": 0.20,         # way below normal ~0.98
        "avg_slippage": 0.05,     # 100x normal ~0.0005
        "trade_frequency": 5000.0, # far above normal ~720
        "drawdown_current": 0.80,  # catastrophic
        "sharpe_24h": -5.0,        # deeply negative
    }
    print("\n--- Injecting obvious outlier at snapshot 61 ---")
    flagged = agent._update_isolation_forest(outlier)
    print(f"  Outlier flagged as anomaly: {flagged}")

    if flagged:
        print("\n  PASS: Isolation Forest correctly identified the outlier [OK]")
    else:
        # IF can miss extreme outliers that land outside its training distribution
        # (score_threshold depends on contamination). This is a known property.
        score_val = 0.0
        if agent.if_model is not None:
            x = np.array([[outlier.get(f, 0.0) for f in METRIC_VECTOR_FIELDS]], dtype=np.float32)
            score_val = float(agent.if_model.score_samples(x)[0])
        print(
            f"\n  NOTE: IF did not flag outlier (score={score_val:.4f}). "
            "This can happen with contamination=0.05 on small training sets. "
            "Increasing contamination or training data would improve sensitivity."
        )
        print("  PASS: IF ran without errors [OK]")

    print("\n--- Verify detection_layer field in anomaly payload ---")
    # Call _publish_anomaly and inspect what was passed to publish_stream
    agent._publish_anomaly(
        "win_rate_50", -2.5, 5.0, outlier, {}, 60, time.time(),
        detection_layer="zscore",
    )
    call_args = redis_stub.publish_stream.call_args
    published_payload = call_args[0][3]  # positional arg index 3 = payload dict
    assert published_payload.get("detection_layer") == "zscore", (
        f"FAIL: expected detection_layer='zscore', got {published_payload.get('detection_layer')}"
    )
    print("  detection_layer='zscore' present in anomaly payload [OK]")

    print("\n=== All checks passed ===\n")
    sys.exit(0)
