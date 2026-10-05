import logging

import redis
from fastapi import Request, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from ..core.config import settings
from ..core.redis_client import redis_client

logger = logging.getLogger(__name__)

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
        
        client_ip = request.client.host if request.client else "unknown"
        key = f"ratelimit:{client_ip}"
        
        try:
            current = redis_client.get(key)
            
            if current is None:
                redis_client.setex(key, 60, 1)
            else:
                current_count = int(current)
                if current_count >= settings.RATE_LIMIT_PER_MINUTE:
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail="Rate limit exceeded. Please try again later."
                    )
                redis_client.incr(key)
        except redis.RedisError as e:
            # If Redis is down, allow the request to proceed
            logger.warning(f"[RateLimit] Redis check failed, allowing request: {e}")
        
        response = await call_next(request)
        return response
