import logging

import redis
from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from ..core.config import settings
from ..core.redis_client import redis_client
from ..core.security import request_ip

logger = logging.getLogger(__name__)

# The client address is X-Real-IP, which nginx overwrites with the peer it saw
# (the real client - with the Let's Encrypt setup, restored from Traefik's PROXY
# header); never the left-most X-Forwarded-For entry, which a client can write
# itself and so pick a fresh bucket for every request.
#
# Paths that bypass the per-IP limiter entirely.
#
# /internal/portal/* is exempt because it is not a public endpoint: it is
# reachable only from PORTAL_IP_WHITELIST (enforced by nginx `allow`/`deny`
# and again in app/core/portal_auth.py) and every call carries a signed,
# single-use, 5-minute portal token. Those are the real gate. Leaving it under
# the 60-req/min per-IP bucket would instead mean the whole portal — one
# source IP for every order it places — shares a single admin user's budget,
# so a batch of orders would start getting 429s for no security benefit.
RATE_LIMIT_EXEMPT_PREFIXES = ("/internal/portal",)


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Skip rate limiting for health check
        if request.url.path == "/health":
            return await call_next(request)

        if request.url.path.startswith(RATE_LIMIT_EXEMPT_PREFIXES):
            return await call_next(request)

        client_ip = request_ip(request) or "unknown"
        key = f"ratelimit:{client_ip}"

        try:
            # One atomic step: count this request and (on the first one in the
            # window) start the window. A separate GET then SETEX/INCR let two
            # concurrent requests both see "under the limit".
            pipe = redis_client.pipeline()
            pipe.set(key, 0, ex=60, nx=True)
            pipe.incr(key)
            _, count = pipe.execute()
        except redis.RedisError as e:
            # If Redis is down, allow the request to proceed
            logger.warning(f"[RateLimit] Redis check failed, allowing request: {e}")
            count = 0

        if count > settings.RATE_LIMIT_PER_MINUTE:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "Rate limit exceeded. Please try again later."},
                headers={"Retry-After": "60"},
            )

        response = await call_next(request)
        return response
