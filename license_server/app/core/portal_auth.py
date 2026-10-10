"""Authentication for /internal/portal/* — the endpoints the internal portal
(http://172.16.120.80) calls to create and inspect licenses.

Three independent checks, applied in this order as router-level dependencies
(see app/routers/internal_portal.py):

    1. source IP is in PORTAL_IP_WHITELIST
    2. bearer token is a valid HS256 JWT signed with PORTAL_JWT_SECRET, with
       the expected iss/aud and an unexpired, short-lived exp
    3. the token's `jti` has not been seen before (Redis, single use)

Ordered cheapest-first and, more importantly, so a caller from the wrong
network is turned away before any token parsing or Redis round trip happens.

Why not reuse app/core/security.py
----------------------------------
That module validates admin UI sessions against SECRET_KEY. Portal tokens are
verified against a different secret (PORTAL_JWT_SECRET) on purpose, so that a
leak of one cannot mint the other, and they carry claims — iss, aud, jti — an
admin session does not have. `issuer=`/`audience=` and the required claims are
passed to `jwt.decode` explicitly: a claim that is neither required nor given
an expected value is not checked.
"""
import ipaddress
import logging
import time
from typing import Optional

import redis
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt
from jwt import PyJWTError

from .config import settings
from .redis_client import redis_client

logger = logging.getLogger(__name__)

# auto_error=False so a request with no Authorization header reaches our own
# check and gets the same 401 shape as a bad token, instead of HTTPBearer's
# 403.
portal_bearer = HTTPBearer(auto_error=False)

# Redis key prefix for spent token ids.
JTI_KEY_PREFIX = "portal:jti:"

# Generic on purpose. A caller that got past the IP whitelist but presented a
# bad token learns only that the token was rejected — not whether the
# signature, the issuer, the audience, the expiry or the jti was at fault.
# The specific reason goes to the log instead.
_INVALID_TOKEN = "Invalid portal token"


def _reject(reason: str, client_ip: Optional[str]) -> HTTPException:
    """Log why a token was refused, return the deliberately vague 401."""
    logger.warning("[portal_auth] rejected call from %s: %s", client_ip, reason)
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=_INVALID_TOKEN,
        headers={"WWW-Authenticate": "Bearer"},
    )


def client_ip(request: Request) -> Optional[str]:
    """The caller's address, as trustworthily as this process can know it.

    Reads X-Real-IP first, NOT request.client.host, and the distinction is the
    whole security of the IP whitelist:

    * nginx sets `X-Real-IP $remote_addr`, which OVERWRITES anything the
      client sent, so behind nginx this header is the real peer address;
    * request.client.host is derived by uvicorn's ProxyHeadersMiddleware, which
      this image runs with `--forwarded-allow-ips "*"` (see the Dockerfile CMD).
      With `*` it trusts X-Forwarded-For unconditionally and takes the
      left-most entry — and nginx *appends* to X-Forwarded-For rather than
      replacing it. A caller sending `X-Forwarded-For: 172.16.120.80` would
      therefore hand itself request.client.host == "172.16.120.80" and walk
      straight through a whitelist built on it.

    Both of those hold only while nginx is the sole route to port 8001, which
    is why `allow 172.16.120.80; deny all;` on the nginx `location /internal/`
    block is the primary gate and this function is defence in depth. The
    backend's 8001 is `expose`-only in docker-compose.yml (never published) so
    that stays true; if it is ever published to the host, X-Real-IP becomes
    caller-controlled and this check is worth no more than a comment.

    The fall back to request.client.host covers running the app directly with
    no proxy in front (local development, the test client).
    """
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        # nginx sends a single address here, but take the first entry defensively
        # so a comma-joined value can never be parsed as one bogus address.
        return real_ip.split(",")[0].strip()
    return request.client.host if request.client else None


def require_portal_ip(request: Request) -> str:
    """Refuse anything whose source address is not in PORTAL_IP_WHITELIST.

    Fails closed: an unset or empty whitelist allows nobody.
    """
    caller = client_ip(request)

    try:
        allowed = settings.portal_ip_whitelist
    except ValueError as exc:
        # A malformed PORTAL_IP_WHITELIST entry. Refuse rather than fall back
        # to a partially-parsed (and therefore unknown) whitelist.
        logger.error("[portal_auth] PORTAL_IP_WHITELIST is not parseable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Portal integration is misconfigured",
        )

    if not allowed:
        logger.error(
            "[portal_auth] PORTAL_IP_WHITELIST is empty; refusing call from %s", caller
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Portal access is not configured"
        )

    if caller is None:
        logger.warning("[portal_auth] refusing call with no determinable source address")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    try:
        address = ipaddress.ip_address(caller)
    except ValueError:
        logger.warning("[portal_auth] refusing call from unparseable address %r", caller)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    if not any(address in network for network in allowed):
        logger.warning("[portal_auth] refusing call from non-whitelisted address %s", caller)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    return caller


def _claim_jti(jti: str, ttl_seconds: int) -> None:
    """Spend a token id, or refuse a replay.

    SET NX is the atomic test-and-set that makes this safe against two
    concurrent requests carrying the same token: exactly one gets the key.
    The TTL is the token's own remaining lifetime, so keys expire on their own
    and the set cannot grow without bound — a `jti` is only worth remembering
    for as long as the token bearing it would still pass the exp check.

    Fails CLOSED on a Redis error (503), unlike the rate limiter, which fails
    open: the endpoints behind this create licenses, and accepting a write
    whose replay status is unknown is worse than being briefly unavailable.
    """
    try:
        first_use = redis_client.set(
            f"{JTI_KEY_PREFIX}{jti}", "1", nx=True, ex=ttl_seconds
        )
    except redis.RedisError as exc:
        logger.error("[portal_auth] Redis unavailable for jti replay check: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Portal token verification is temporarily unavailable",
        )

    if not first_use:
        # Genuine replay, or the portal reusing one token for two calls.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_TOKEN,
            headers={"WWW-Authenticate": "Bearer"},
        )


def verify_portal_jwt(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(portal_bearer),
) -> dict:
    """Validate a portal service token and return its claims.

    Checks, beyond the signature: iss, aud, exp, iat, a lifetime no longer
    than PORTAL_JWT_MAX_LIFETIME_SECONDS, and single use of `jti`.
    """
    caller = client_ip(request)

    if not settings.PORTAL_JWT_SECRET:
        logger.error("[portal_auth] PORTAL_JWT_SECRET is not set; refusing portal call")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Portal integration is not configured",
        )

    if credentials is None or not credentials.credentials:
        raise _reject("missing bearer token", caller)

    try:
        claims = jwt.decode(
            credentials.credentials,
            settings.PORTAL_JWT_SECRET,
            # Pinned to the one algorithm the portal uses. Never derived from
            # the token's own header, which is what makes an "alg": "none" or
            # an HS256-signed-with-the-public-key downgrade impossible.
            algorithms=["HS256"],
            issuer=settings.PORTAL_JWT_ISSUER,
            audience=settings.PORTAL_JWT_AUDIENCE,
            leeway=settings.PORTAL_JWT_LEEWAY_SECONDS,
            # Required, not merely checked when present: without this a token
            # with no exp would never expire.
            options={"require": ["exp", "iat", "iss", "aud", "jti"]},
        )
    except PyJWTError as exc:
        raise _reject(f"token did not verify ({exc})", caller)

    exp = claims.get("exp")
    iat = claims.get("iat")
    if not isinstance(exp, (int, float)) or not isinstance(iat, (int, float)):
        raise _reject("exp/iat are not numeric", caller)

    # Cap the lifetime. decode has already confirmed the token is unexpired; this
    # additionally refuses a token that was minted to live far longer than the
    # integration's 5 minutes, which would otherwise be a long-lived
    # credential travelling over the portal's plain-HTTP hop.
    if exp - iat > settings.PORTAL_JWT_MAX_LIFETIME_SECONDS:
        raise _reject(
            f"token lifetime {exp - iat:.0f}s exceeds the "
            f"{settings.PORTAL_JWT_MAX_LIFETIME_SECONDS}s maximum",
            caller,
        )

    jti = claims.get("jti")
    if not jti or not isinstance(jti, str):
        raise _reject("missing or non-string jti", caller)

    # Remember the jti only for as long as the token could still be presented
    # again, plus the leeway that would let it pass the exp check. Clamped to
    # at least 1s because Redis rejects a non-positive TTL, and to the
    # configured maximum so a bad clock cannot pin a key indefinitely.
    remaining = exp - time.time() + settings.PORTAL_JWT_LEEWAY_SECONDS
    ttl = max(1, min(int(remaining), settings.PORTAL_JWT_MAX_LIFETIME_SECONDS))
    _claim_jti(jti, ttl)

    logger.info("[portal_auth] accepted portal call from %s (jti=%s)", caller, jti)
    return claims
