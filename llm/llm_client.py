"""LLM client: Ollama primary → Groq fallback → rule-based fallback."""

import json
import logging
import re
import time
from typing import Dict, Optional, Tuple

import requests
from openai import OpenAI

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are WatchdogAI, a trading bot health monitoring expert. You analyze metric anomalies in algorithmic trading systems and provide structured diagnoses.

You classify anomalies into exactly one of these failure modes:
1. market_driven - regime changed, liquidity event, flash crash; strategy behaving correctly given market conditions
2. signal_decay - alpha degrading, market has adapted, win rate eroding over time
3. execution_infrastructure - exchange latency, API rate limits, partial fills, connectivity issues
4. parameter_sensitivity - bot parameters poorly suited to current volatility regime
5. noise - one-off statistical outlier, no structural issue

Severity scale:
1 = informational, no action needed
2 = minor, monitor closely
3 = notable, flag for human review
4 = significant, recommend parameter adjustment
5 = critical, recommend kill-switch

Regime context MUST influence severity:
- In HIGH_VOLATILITY regime: fill_rate drops and slippage spikes are partially expected (reduce severity by 1-2)
- In TRENDING regime: win_rate drops and signal anomalies are more severe (maintain or increase severity)
- RANGING regime: overtrading signals are more concerning

Always output valid JSON only. No prose before or after the JSON block."""

_OUTPUT_SCHEMA = """{
  "anomaly_type": "<market_driven|signal_decay|execution_infrastructure|parameter_sensitivity|noise>",
  "confidence": <0.0-1.0>,
  "severity": <1-5>,
  "reasoning": "<2-4 sentences explaining the diagnosis>",
  "primary_indicators": ["<metric at Xσ>", ...],
  "regime_context": "<one sentence on how regime affects this diagnosis>",
  "recommended_actions": ["<action 1>", "<action 2>"]
}"""


def _rule_based_diagnosis(metric: str, z_score: float, regime: Optional[str]) -> Dict:
    """Fallback when all LLM backends fail."""
    abs_z = abs(z_score)
    if abs_z >= 4.0:
        severity = 5
    elif abs_z >= 3.0:
        severity = 4
    elif abs_z >= 2.5:
        severity = 3
    else:
        severity = 2

    regime = regime or "UNKNOWN"
    if regime == "HIGH_VOLATILITY" and metric in ("fill_rate", "avg_slippage"):
        severity = max(1, severity - 2)

    anomaly_map = {
        "fill_rate": "execution_infrastructure",
        "avg_slippage": "execution_infrastructure",
        "win_rate_50": "signal_decay",
        "pnl_1h": "signal_decay",
        "drawdown_current": "market_driven",
        "trade_frequency": "parameter_sensitivity",
    }
    anomaly_type = anomaly_map.get(metric, "noise")

    return {
        "anomaly_type": anomaly_type,
        "confidence": 0.5,
        "severity": severity,
        "reasoning": f"Rule-based diagnosis: {metric} at {z_score:.1f}σ. LLM backends unavailable.",
        "primary_indicators": [f"{metric} at {z_score:.1f}σ"],
        "regime_context": f"Regime: {regime}. Regime context unavailable (rule-based mode).",
        "recommended_actions": ["Monitor metric closely", "Check LLM backend availability"],
        "backend_used": "rule_based",
    }


def _parse_llm_json(text: str) -> Optional[Dict]:
    """Extract and parse JSON from LLM response text."""
    # Try direct parse first
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass

    # Extract JSON block from markdown fences or surrounding text
    patterns = [
        r"```json\s*([\s\S]+?)\s*```",
        r"```\s*([\s\S]+?)\s*```",
        r"(\{[\s\S]+\})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            try:
                return json.loads(match.group(1))
            except Exception:
                continue
    return None


class LLMClient:
    def __init__(self, config: Dict):
        self.ollama_cfg = config["ollama"]
        self.groq_cfg = config["groq"]
        self._groq_client: Optional[OpenAI] = None
        if self.groq_cfg.get("api_key"):
            self._groq_client = OpenAI(
                api_key=self.groq_cfg["api_key"],
                base_url=self.groq_cfg["base_url"],
                timeout=self.groq_cfg["timeout_seconds"],
            )

    def _call_ollama(self, prompt: str) -> Tuple[Optional[str], str]:
        """Returns (response_text, backend_name) or (None, backend_name) on failure."""
        url = f"{self.ollama_cfg['base_url']}/api/chat"
        payload = {
            "model": self.ollama_cfg["model"],
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": {
                "temperature": self.ollama_cfg["temperature"],
                "num_predict": self.ollama_cfg["max_tokens"],
            },
        }
        try:
            resp = requests.post(
                url, json=payload, timeout=self.ollama_cfg["timeout_seconds"]
            )
            resp.raise_for_status()
            data = resp.json()
            text = data.get("message", {}).get("content", "")
            return text, "ollama"
        except Exception as e:
            logger.warning(f"Ollama failed: {e}")
            return None, "ollama"

    def _call_groq(self, prompt: str, model: str) -> Tuple[Optional[str], str]:
        if self._groq_client is None:
            return None, f"groq/{model}"
        try:
            completion = self._groq_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=self.groq_cfg["temperature"],
                max_tokens=self.groq_cfg["max_tokens"],
            )
            text = completion.choices[0].message.content or ""
            return text, f"groq/{model}"
        except Exception as e:
            logger.warning(f"Groq ({model}) failed: {e}")
            return None, f"groq/{model}"

    def diagnose(
        self,
        metric: str,
        z_score: float,
        duration_minutes: float,
        pnl_narrative: str,
        regime: Optional[str],
        bot_config: Dict,
        breach_history: list,
    ) -> Dict:
        breach_summary = (
            f"This metric has breached {len(breach_history)} time(s) before."
            if breach_history
            else "First breach for this metric."
        )

        prompt = f"""Diagnose this trading bot anomaly and respond with JSON only.

ANOMALY:
- Metric breached: {metric}
- Z-score: {z_score:.2f}σ (threshold: ±2.0σ)
- Duration in breach: {duration_minutes:.1f} minutes

PnL CONTEXT (last 2 hours):
{pnl_narrative}

REGIME: {regime or 'UNKNOWN'}

BOT CONFIG:
- Strategy: Momentum (EMA crossover + RSI)
- Asset: {bot_config.get('symbol', 'BTC/USDT')}
- Timeframe: {bot_config.get('timeframe', '1h')}
- Stop-loss: {bot_config.get('stop_loss_pct', 0.02) * 100:.1f}%
- Risk per trade: {bot_config.get('risk_per_trade_pct', 0.01) * 100:.1f}%

BREACH HISTORY: {breach_summary}

Output JSON matching exactly this schema:
{_OUTPUT_SCHEMA}"""

        # Try backends in order: Ollama → Groq primary → Groq fallback → rule-based
        backends = [
            lambda: self._call_ollama(prompt),
            lambda: self._call_groq(prompt, self.groq_cfg["primary_model"]),
            lambda: self._call_groq(prompt, self.groq_cfg["fallback_model"]),
        ]

        for backend_fn in backends:
            text, backend_name = backend_fn()
            if text:
                parsed = _parse_llm_json(text)
                if parsed:
                    parsed["backend_used"] = backend_name
                    return parsed
                logger.warning(f"{backend_name} returned unparseable JSON: {text[:200]}")

        logger.error("All LLM backends failed — using rule-based diagnosis")
        return _rule_based_diagnosis(metric, z_score, regime)


if __name__ == "__main__":
    import yaml
    from pathlib import Path

    logging.basicConfig(level=logging.INFO)
    cfg_path = Path(__file__).parent.parent / "config.yaml"
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    client = LLMClient(cfg)
    result = client.diagnose(
        metric="win_rate_50",
        z_score=-2.8,
        duration_minutes=45.0,
        pnl_narrative="PnL declined from +150 to +80 over 2 hours. Last 4 windows showed negative PnL_1h.",
        regime="TRENDING",
        bot_config={"symbol": "BTC/USDT", "timeframe": "1h"},
        breach_history=[],
    )
    print(json.dumps(result, indent=2))
