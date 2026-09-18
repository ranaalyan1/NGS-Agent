import hashlib
import json
import logging
import os
from typing import Any

import boto3
import redis

logger = logging.getLogger(__name__)

# Keys that differ on every submission but don't change the computation.
# Hashing them would make identical re-runs always miss the cache.
VOLATILE_HASH_KEYS = frozenset({"run_id", "sample_id"})


def _strip_volatile(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_volatile(v) for k, v in obj.items() if k not in VOLATILE_HASH_KEYS}
    if isinstance(obj, list):
        return [_strip_volatile(v) for v in obj]
    return obj


class CacheManager:
    def __init__(self) -> None:
        self.redis = redis.from_url(
            os.environ.get("REDIS_URL", "redis://localhost:6379"), decode_responses=True
        )
        self.s3 = boto3.client(
            "s3",
            endpoint_url=os.environ.get("S3_ENDPOINT", "http://localhost:9000"),
            aws_access_key_id=os.environ.get("S3_ACCESS_KEY", "minioadmin"),
            aws_secret_access_key=os.environ.get("S3_SECRET_KEY", "minioadmin"),
        )
        self.bucket = os.environ.get("CACHE_BUCKET", "ngs-cache")

    def compute_hash(self, agent_name: str, inputs: dict[str, Any]) -> str:
        stable = _strip_volatile({"agent": agent_name, "inputs": inputs})
        content = json.dumps(stable, sort_keys=True)
        return hashlib.blake2b(content.encode("utf-8")).hexdigest()[:16]

    async def get(self, cache_key: str) -> dict[str, Any] | None:
        redis_key = f"cache:{cache_key}"
        try:
            data = self.redis.get(redis_key)
        except Exception as exc:
            # A stopped Redis must degrade to "cache miss", never fail the run.
            logger.warning("Redis cache unavailable, treating as miss: %s", exc)
            data = None
        if data:
            try:
                return json.loads(data)
            except Exception:
                return None

        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=f"{cache_key}.json")
            return json.loads(obj["Body"].read().decode("utf-8"))
        except Exception:
            return None

    async def set(self, cache_key: str, data: dict[str, Any], ttl_days: int = 30) -> None:
        redis_key = f"cache:{cache_key}"
        try:
            self.redis.setex(redis_key, ttl_days * 24 * 3600, json.dumps(data))
        except Exception as exc:
            logger.warning("Redis cache write failed, continuing uncached: %s", exc)
        try:
            self.s3.put_object(Bucket=self.bucket, Key=f"{cache_key}.json", Body=json.dumps(data))
        except Exception as exc:
            logger.warning("S3 cache write failed, continuing uncached: %s", exc)
