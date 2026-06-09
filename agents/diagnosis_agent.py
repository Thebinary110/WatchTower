"""Agent 2 — Diagnosis Agent: LLM-backed anomaly diagnosis, regime-aware."""

import logging
import queue
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def _read_regime_from_sqlite(db_path: str) -> Optional[str]:
    """Read the most recent regime from RegimeRadar's SQLite database."""
    path = Path(db_path)
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # Try common table names used by RegimeRadar
        for table in ("regime_snapshots", "regime_log", "regimes", "regime_agent"):
            try:
                row = conn.execute(
                    f"SELECT regime FROM {table} ORDER BY timestamp DESC LIMIT 1"
                ).fetchone()
                if row:
                    conn.close()
                    return row["regime"]
            except Exception:
                continue
        conn.close()
    except Exception as e:
        logger.debug(f"RegimeRadar SQLite read failed: {e}")
    return None


class DiagnosisAgent:
    def __init__(self, config: Dict, redis_client, db_log, llm_client, activity_queue: queue.Queue):
        self.config = config
        self.redis = redis_client
        self.db = db_log
        self.llm = llm_client
        self.activity_q = activity_queue
        self._stop = False
        self._regime_stream = config["redis"]["streams"]["regime"]
        self._anomaly_stream = config["redis"]["streams"]["anomalies"]
        self._diagnosis_stream = config["redis"]["streams"]["diagnoses"]

    def run(self):
        """Subscribe to anomalies stream and diagnose each event. Blocking."""
        self._log_activity("Diagnosis Agent started", "Waiting for anomaly events")
        logger.info("Diagnosis Agent: subscribing to anomalies stream")

        if self.redis.connected:
            self._run_redis()
        else:
            self._run_offline()

    def _run_redis(self):
        for event in self.redis.subscribe_stream(self._anomaly_stream, last_id="0"):
            if self._stop:
                break
            payload = event.get("payload", {})
            if not payload:
                continue
            self._process_anomaly(payload)

    def _run_offline(self):
        """Poll an in-memory queue if Redis is unavailable."""
        logger.warning("Diagnosis Agent running in offline mode — no Redis subscription")
        while not self._stop:
            time.sleep(5)

    def _process_anomaly(self, payload: Dict):
        metric = payload.get("metric", "unknown")
        z_score = payload.get("z_score", 0.0)
        duration_min = payload.get("duration_minutes", 0.0)
        pnl_narrative = payload.get("pnl_narrative", "")
        total_trades = payload.get("total_trades", 0)
        ts = payload.get("timestamp", time.time())

        self._log_activity(
            "Diagnosing anomaly",
            f"{metric} at {z_score:+.2f}σ — fetching regime context...",
        )

        regime = self._get_regime()
        breach_history = self.db.get_metric_breach_history(metric, limit=10)

        bot_config = {
            "symbol": self.config["binance"]["symbol"],
            "timeframe": self.config["binance"]["timeframe"],
            "stop_loss_pct": self.config["bot"]["stop_loss_pct"],
            "risk_per_trade_pct": self.config["bot"]["risk_per_trade_pct"],
        }

        self._log_activity(
            "LLM diagnosis in progress",
            f"Regime={regime or 'UNKNOWN'} | Backend chain: Ollama → Groq",
        )

        diagnosis = self.llm.diagnose(
            metric=metric,
            z_score=z_score,
            duration_minutes=duration_min,
            pnl_narrative=pnl_narrative,
            regime=regime,
            bot_config=bot_config,
            breach_history=breach_history,
        )

        alert_id = self.db.log_alert(ts, metric, z_score, diagnosis)

        diagnosis_payload = {
            "alert_id": alert_id,
            "metric": metric,
            "z_score": z_score,
            "regime": regime,
            "total_trades": total_trades,
            **diagnosis,
        }
        self.redis.publish_stream(
            self._diagnosis_stream, "diagnosis_agent", "diagnosis_result", diagnosis_payload
        )

        self._log_activity(
            "Diagnosis complete",
            f"[{diagnosis.get('anomaly_type', '?').upper()}] "
            f"Severity={diagnosis.get('severity', '?')} "
            f"Confidence={diagnosis.get('confidence', 0):.0%} "
            f"Backend={diagnosis.get('backend_used', '?')} — "
            f"{diagnosis.get('reasoning', '')[:120]}",
            is_alert=True,
        )
        logger.info(
            f"Diagnosis: {metric} → {diagnosis.get('anomaly_type')} "
            f"sev={diagnosis.get('severity')} conf={diagnosis.get('confidence'):.2f} "
            f"via {diagnosis.get('backend_used')}"
        )

    def _get_regime(self) -> Optional[str]:
        # Try Redis stream first
        if self.redis.connected:
            msgs = self.redis.read_stream_latest(self._regime_stream, count=1)
            if msgs:
                p = msgs[0].get("payload", {})
                regime = p.get("regime") or p.get("current_regime")
                if regime:
                    return regime

        # Fallback to RegimeRadar SQLite
        sqlite_path = self.config["regimeradar"]["sqlite_path"]
        regime = _read_regime_from_sqlite(sqlite_path)
        if regime:
            return regime

        return None

    def _log_activity(self, action: str, detail: str, is_alert: bool = False):
        try:
            self.activity_q.put_nowait(
                {
                    "source": "DiagnosisAgent",
                    "action": action,
                    "detail": detail,
                    "timestamp": time.time(),
                    "is_alert": is_alert,
                }
            )
        except queue.Full:
            pass

    def stop(self):
        self._stop = True
