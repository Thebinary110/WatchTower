"""SQLite persistence: metrics_log, alerts_log, events_log."""

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_CREATE_METRICS_LOG = """
CREATE TABLE IF NOT EXISTS metrics_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    pnl_cumulative REAL,
    pnl_1h REAL,
    win_rate_50 REAL,
    fill_rate REAL,
    avg_slippage REAL,
    trade_frequency REAL,
    drawdown_current REAL,
    sharpe_24h REAL,
    zscore_win_rate REAL,
    zscore_fill_rate REAL,
    zscore_slippage REAL,
    zscore_drawdown REAL,
    total_trades INTEGER
)
"""

_CREATE_ALERTS_LOG = """
CREATE TABLE IF NOT EXISTS alerts_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    metric_breached TEXT,
    z_score REAL,
    anomaly_type TEXT,
    severity INTEGER,
    confidence REAL,
    reasoning TEXT,
    recommended_actions TEXT,
    regime_context TEXT,
    backend_used TEXT,
    resolved INTEGER DEFAULT 0
)
"""

_CREATE_EVENTS_LOG = """
CREATE TABLE IF NOT EXISTS events_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    event_type TEXT,
    details TEXT,
    severity INTEGER
)
"""


class WatchdogLog:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(_CREATE_METRICS_LOG)
                conn.execute(_CREATE_ALERTS_LOG)
                conn.execute(_CREATE_EVENTS_LOG)
                conn.commit()
            finally:
                conn.close()

    def log_metrics(self, ts: float, metrics: Dict, zscores: Dict, total_trades: int):
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO metrics_log
                       (timestamp, pnl_cumulative, pnl_1h, win_rate_50, fill_rate, avg_slippage,
                        trade_frequency, drawdown_current, sharpe_24h,
                        zscore_win_rate, zscore_fill_rate, zscore_slippage, zscore_drawdown,
                        total_trades)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        ts,
                        metrics.get("pnl_cumulative"),
                        metrics.get("pnl_1h"),
                        metrics.get("win_rate_50"),
                        metrics.get("fill_rate"),
                        metrics.get("avg_slippage"),
                        metrics.get("trade_frequency"),
                        metrics.get("drawdown_current"),
                        metrics.get("sharpe_24h"),
                        zscores.get("win_rate_50"),
                        zscores.get("fill_rate"),
                        zscores.get("avg_slippage"),
                        zscores.get("drawdown_current"),
                        total_trades,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def log_alert(
        self,
        ts: float,
        metric_breached: str,
        z_score: float,
        diagnosis: Dict,
    ) -> int:
        with self._lock:
            conn = self._get_conn()
            try:
                actions = diagnosis.get("recommended_actions", [])
                if isinstance(actions, list):
                    actions = json.dumps(actions)
                cursor = conn.execute(
                    """INSERT INTO alerts_log
                       (timestamp, metric_breached, z_score, anomaly_type, severity, confidence,
                        reasoning, recommended_actions, regime_context, backend_used, resolved)
                       VALUES (?,?,?,?,?,?,?,?,?,?,0)""",
                    (
                        ts,
                        metric_breached,
                        z_score,
                        diagnosis.get("anomaly_type"),
                        diagnosis.get("severity"),
                        diagnosis.get("confidence"),
                        diagnosis.get("reasoning"),
                        actions,
                        diagnosis.get("regime_context"),
                        diagnosis.get("backend_used"),
                    ),
                )
                conn.commit()
                return cursor.lastrowid
            finally:
                conn.close()

    def resolve_alert(self, alert_id: int):
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("UPDATE alerts_log SET resolved=1 WHERE id=?", (alert_id,))
                conn.commit()
            finally:
                conn.close()

    def log_event(self, ts: float, event_type: str, details: str, severity: int):
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT INTO events_log (timestamp, event_type, details, severity) VALUES (?,?,?,?)",
                    (ts, event_type, details, severity),
                )
                conn.commit()
            finally:
                conn.close()

    def get_recent_metrics(self, limit: int = 100) -> List[Dict]:
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM metrics_log ORDER BY timestamp DESC LIMIT ?", (limit,)
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def get_active_alerts(self, limit: int = 50) -> List[Dict]:
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM alerts_log WHERE resolved=0 ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                result = []
                for r in rows:
                    d = dict(r)
                    if d.get("recommended_actions"):
                        try:
                            d["recommended_actions"] = json.loads(d["recommended_actions"])
                        except Exception:
                            d["recommended_actions"] = [d["recommended_actions"]]
                    result.append(d)
                return result
            finally:
                conn.close()

    def get_all_alerts(self, limit: int = 200) -> List[Dict]:
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM alerts_log ORDER BY timestamp DESC LIMIT ?", (limit,)
                ).fetchall()
                result = []
                for r in rows:
                    d = dict(r)
                    if d.get("recommended_actions"):
                        try:
                            d["recommended_actions"] = json.loads(d["recommended_actions"])
                        except Exception:
                            d["recommended_actions"] = [d["recommended_actions"]]
                    result.append(d)
                return result
            finally:
                conn.close()

    def get_metric_breach_history(self, metric: str, limit: int = 20) -> List[Dict]:
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM alerts_log WHERE metric_breached=? ORDER BY timestamp DESC LIMIT ?",
                    (metric, limit),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def get_recent_events(self, limit: int = 50) -> List[Dict]:
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM events_log ORDER BY timestamp DESC LIMIT ?", (limit,)
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
