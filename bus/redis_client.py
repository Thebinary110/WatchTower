"""Redis Cloud connection and stream publish/subscribe wrappers."""

import json
import logging
import time
from typing import Any, Dict, List, Optional

import redis

logger = logging.getLogger(__name__)


class RedisClient:
    def __init__(self, config: Dict):
        self.config = config
        self.url = config["redis"]["url"]
        self.streams = config["redis"]["streams"]
        self._client: Optional[redis.Redis] = None
        self._connected = False
        self._connect()

    def _connect(self):
        if not self.url:
            logger.warning("Redis URL not configured — running in offline mode")
            return
        try:
            self._client = redis.from_url(self.url, decode_responses=True, socket_connect_timeout=5)
            self._client.ping()
            self._connected = True
            logger.info("Redis connected")
        except Exception as e:
            logger.warning(f"Redis connection failed: {e} — running in offline mode")
            self._client = None
            self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def publish_stream(self, stream_key: str, source_agent: str, message_type: str, payload: Dict) -> bool:
        if not self._connected or self._client is None:
            return False
        try:
            message = {
                "timestamp": str(time.time()),
                "source_agent": source_agent,
                "message_type": message_type,
                "payload_json": json.dumps(payload),
            }
            self._client.xadd(stream_key, message, maxlen=1000, approximate=True)
            return True
        except Exception as e:
            logger.error(f"Stream publish failed ({stream_key}): {e}")
            self._connected = False
            return False

    def read_stream_latest(self, stream_key: str, count: int = 10) -> List[Dict]:
        """Read the latest N messages from a stream."""
        if not self._connected or self._client is None:
            return []
        try:
            results = self._client.xrevrange(stream_key, count=count)
            messages = []
            for msg_id, fields in results:
                entry = dict(fields)
                entry["_id"] = msg_id
                if "payload_json" in entry:
                    try:
                        entry["payload"] = json.loads(entry["payload_json"])
                    except Exception:
                        entry["payload"] = {}
                messages.append(entry)
            return messages
        except Exception as e:
            logger.error(f"Stream read failed ({stream_key}): {e}")
            return []

    def subscribe_stream(self, stream_key: str, last_id: str = "$"):
        """Generator: yields (id, fields) for each new message."""
        if not self._connected or self._client is None:
            return
        current_id = last_id
        while True:
            try:
                results = self._client.xread({stream_key: current_id}, block=5000, count=10)
                if results:
                    for _stream, messages in results:
                        for msg_id, fields in messages:
                            current_id = msg_id
                            entry = dict(fields)
                            entry["_id"] = msg_id
                            if "payload_json" in entry:
                                try:
                                    entry["payload"] = json.loads(entry["payload_json"])
                                except Exception:
                                    entry["payload"] = {}
                            yield entry
            except redis.exceptions.ConnectionError as e:
                logger.error(f"Redis connection lost during subscribe: {e}")
                self._connected = False
                break
            except Exception as e:
                logger.debug(f"Stream subscribe error ({stream_key}): {e}")
                time.sleep(1)

    def get_stream_length(self, stream_key: str) -> int:
        if not self._connected or self._client is None:
            return 0
        try:
            return self._client.xlen(stream_key)
        except Exception:
            return 0

    def ping(self) -> bool:
        if self._client is None:
            return False
        try:
            self._client.ping()
            self._connected = True
            return True
        except Exception:
            self._connected = False
            return False


if __name__ == "__main__":
    import yaml
    from pathlib import Path

    logging.basicConfig(level=logging.INFO)
    cfg_path = Path(__file__).parent.parent / "config.yaml"
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    client = RedisClient(cfg)
    if client.connected:
        print("Redis connection: OK")
        client.publish_stream(cfg["redis"]["streams"]["bot_metrics"], "test", "ping", {"hello": "world"})
        msgs = client.read_stream_latest(cfg["redis"]["streams"]["bot_metrics"], 1)
        print(f"Read back: {msgs}")
    else:
        print("Redis offline — check config.yaml redis.url")
