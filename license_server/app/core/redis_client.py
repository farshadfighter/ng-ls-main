"""The process's single Redis connection.

Extracted from app/middleware/rate_limit.py, which used to own the only
client, so app/core/portal_auth.py can reuse the same connection pool for
`jti` replay detection instead of opening a second one.

Both callers share the pool but NOT the failure policy, and the difference is
deliberate:

* the rate limiter fails **open** — if Redis is down, requests are allowed
  through unthrottled, because refusing every license validation to protect a
  rate limit would take the product down;
* portal token replay detection fails **closed** — if Redis is down,
  /internal/portal/* returns 503, because the endpoint behind it mints
  licenses, and an un-replay-checked write is worse than an unavailable one.

Each caller therefore handles redis.RedisError itself; nothing here does.
"""
import redis

from .config import settings

redis_client = redis.Redis(
    host=settings.REDIS_HOST,
    port=settings.REDIS_PORT,
    db=settings.REDIS_DB,
    password=settings.REDIS_PASSWORD,
    decode_responses=True,
)
