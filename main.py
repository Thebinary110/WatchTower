"""WatchdogAI entry point — starts all three agents as threads."""

import logging
import queue
import sys
import threading
import time
from pathlib import Path

import yaml

from agents.action_agent import ActionAgent
from agents.diagnosis_agent import DiagnosisAgent
from agents.metrics_agent import MetricsAgent
from bot.anomaly_injector import AnomalyInjector
from bot.momentum_bot import MomentumBot
from bus.redis_client import RedisClient
from llm.llm_client import LLMClient
from storage.watchdog_log import WatchdogLog


def _setup_logging(log_path: str):
    log_file = Path(log_path)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(str(log_file)),
        ],
    )


def _load_config() -> dict:
    cfg_path = Path(__file__).parent / "config.yaml"
    with open(cfg_path) as f:
        return yaml.safe_load(f)


def main():
    config = _load_config()
    _setup_logging(config["storage"]["log_path"])
    logger = logging.getLogger("main")

    logger.info("=" * 60)
    logger.info("WatchdogAI starting")
    logger.info("=" * 60)

    # Shared infrastructure
    redis_client = RedisClient(config)
    db_log = WatchdogLog(config["storage"]["sqlite_path"])
    llm_client = LLMClient(config)

    # Shared activity queue for dashboard (maxsize=500 — dashboard drains it)
    activity_q: queue.Queue = queue.Queue(maxsize=500)

    # Agents
    metrics_agent = MetricsAgent(config, redis_client, db_log, activity_q)
    diagnosis_agent = DiagnosisAgent(config, redis_client, db_log, llm_client, activity_q)
    action_agent = ActionAgent(config, redis_client, db_log, activity_q)

    # Anomaly injector wraps the trade callback
    injector = AnomalyInjector(config)

    def trade_callback(trade: dict):
        injected_trade = injector.inject(trade)
        metrics_agent.on_trade(injected_trade)

    # Bot (loads data synchronously before thread start)
    bot = MomentumBot(config, trade_callback)
    logger.info("Loading historical data from Binance...")
    try:
        bot.load()
    except Exception as e:
        logger.error(f"Failed to load bot data: {e}")
        sys.exit(1)

    # Launch all threads
    threads = []

    def run_agent(agent, name):
        try:
            agent.run()
        except Exception as e:
            logger.error(f"{name} crashed: {e}", exc_info=True)

    t_diagnosis = threading.Thread(
        target=run_agent,
        args=(diagnosis_agent, "DiagnosisAgent"),
        name="DiagnosisAgent",
        daemon=True,
    )
    t_action = threading.Thread(
        target=run_agent,
        args=(action_agent, "ActionAgent"),
        name="ActionAgent",
        daemon=True,
    )
    t_bot = threading.Thread(
        target=bot.start,
        name="MomentumBot",
        daemon=True,
    )

    threads = [t_diagnosis, t_action, t_bot]

    for t in threads:
        t.start()
        logger.info(f"Thread started: {t.name}")

    logger.info("All threads running. Press Ctrl+C to stop.")
    logger.info(f"Dashboard: streamlit run dashboard/app.py")

    try:
        while True:
            alive = [t.name for t in threads if t.is_alive()]
            if not alive:
                logger.info("All threads finished")
                break
            time.sleep(10)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        diagnosis_agent.stop()
        action_agent.stop()
        bot.stop()

    logger.info("WatchdogAI stopped")


if __name__ == "__main__":
    main()
