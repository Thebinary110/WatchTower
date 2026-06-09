"""Agent 3 — Action Agent: severity routing, recommendation generation, human-in-the-loop."""

import logging
import queue
import time
from typing import Dict, List

logger = logging.getLogger(__name__)

_SEVERITY_LABELS = {
    1: "INFO",
    2: "MINOR",
    3: "REVIEW",
    4: "ACTION",
    5: "CRITICAL",
}

_SEVERITY_COLORS = {
    1: "blue",
    2: "yellow",
    3: "orange",
    4: "red",
    5: "darkred",
}


def _build_recommendation(diagnosis: Dict) -> Dict:
    severity = diagnosis.get("severity", 1)
    anomaly_type = diagnosis.get("anomaly_type", "noise")
    actions = diagnosis.get("recommended_actions", [])
    metric = diagnosis.get("metric", "unknown")

    if severity <= 2:
        routing = "log_only"
        user_message = f"Metric {metric} is anomalous. Logged for monitoring. No action required."
        dashboard_level = "notification"

    elif severity == 3:
        routing = "human_review"
        user_message = (
            f"WARNING: {metric} anomaly detected ({anomaly_type.replace('_', ' ').title()}). "
            f"Flagged for human review."
        )
        dashboard_level = "warning"

    elif severity == 4:
        routing = "recommend_action"
        actions_text = "; ".join(actions) if actions else "Review bot parameters"
        user_message = (
            f"ACTION RECOMMENDED: {metric} shows {anomaly_type.replace('_', ' ')} pattern. "
            f"Suggested actions: {actions_text}. Human confirmation required."
        )
        dashboard_level = "action_required"

    else:  # severity 5
        routing = "kill_switch"
        user_message = (
            f"CRITICAL: {metric} indicates severe anomaly ({anomaly_type.replace('_', ' ')}). "
            f"Kill-switch recommended. ONE-CLICK CONFIRMATION REQUIRED."
        )
        dashboard_level = "kill_switch"

    return {
        "routing": routing,
        "severity": severity,
        "severity_label": _SEVERITY_LABELS.get(severity, "UNKNOWN"),
        "severity_color": _SEVERITY_COLORS.get(severity, "grey"),
        "user_message": user_message,
        "recommended_actions": actions,
        "dashboard_level": dashboard_level,
        "anomaly_type": anomaly_type,
        "metric": metric,
        "reasoning": diagnosis.get("reasoning", ""),
        "confidence": diagnosis.get("confidence", 0.0),
        "regime_context": diagnosis.get("regime_context", ""),
        "alert_id": diagnosis.get("alert_id"),
        "backend_used": diagnosis.get("backend_used", "unknown"),
        "timestamp": time.time(),
    }


class ActionAgent:
    def __init__(self, config: Dict, redis_client, db_log, activity_queue: queue.Queue):
        self.config = config
        self.redis = redis_client
        self.db = db_log
        self.activity_q = activity_queue
        self._stop = False
        self._diagnosis_stream = config["redis"]["streams"]["diagnoses"]
        self._actions_stream = config["redis"]["streams"]["actions"]
        self._severity_cfg = config["agent"]

    def run(self):
        """Subscribe to diagnoses stream and route actions. Blocking."""
        self._log_activity("Action Agent started", "Waiting for diagnosis events")
        logger.info("Action Agent: subscribing to diagnoses stream")

        if self.redis.connected:
            self._run_redis()
        else:
            self._run_offline()

    def _run_redis(self):
        for event in self.redis.subscribe_stream(self._diagnosis_stream, last_id="0"):
            if self._stop:
                break
            payload = event.get("payload", {})
            if not payload:
                continue
            self._route(payload)

    def _run_offline(self):
        logger.warning("Action Agent running in offline mode")
        while not self._stop:
            time.sleep(5)

    def _route(self, diagnosis: Dict):
        recommendation = _build_recommendation(diagnosis)
        severity = recommendation["severity"]
        routing = recommendation["routing"]

        # Always publish to Redis actions stream
        self.redis.publish_stream(
            self._actions_stream, "action_agent", "action_recommendation", recommendation
        )

        # Log critical events to events_log
        if severity >= 4:
            self.db.log_event(
                ts=recommendation["timestamp"],
                event_type=f"severity_{severity}_{routing}",
                details=recommendation["user_message"],
                severity=severity,
            )

        self._log_activity(
            f"Severity-{severity} routed [{routing.upper()}]",
            recommendation["user_message"][:160],
            is_alert=severity >= 3,
        )
        logger.info(
            f"Action routed: sev={severity} routing={routing} "
            f"metric={recommendation['metric']} type={recommendation['anomaly_type']}"
        )

    def _log_activity(self, action: str, detail: str, is_alert: bool = False):
        try:
            self.activity_q.put_nowait(
                {
                    "source": "ActionAgent",
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
