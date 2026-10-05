import ipaddress

from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    # Database
    DATABASE_URL: str
    
    # Security
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    JWT_EXPIRE_HOURS: int = 24
    
    # Redis
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    # No default: unset means the redis.Redis client connects unauthenticated,
    # which is fine for a local dev Redis with no requirepass set but wrong for
    # the compose stack, where docker-compose.yml requires this var.
    REDIS_PASSWORD: Optional[str] = None
    
    # Rate Limiting
    RATE_LIMIT_PER_MINUTE: int = 60
    
    # API
    API_V1_PREFIX: str = "/api"
    
    # Ed25519 key that signs every licensing answer (base64 of the 32-byte seed;
    # make one with scripts/generate_signing_key.py). NGCorion only trusts
    # answers signed by a key whose public half is built into its code, so a
    # fake license server cannot unlock it. No default: unset means the license
    # endpoints answer 503 rather than send answers NGCorion would refuse.
    # Keep it out of git and off every other machine - whoever has it can mint
    # licenses.
    LICENSE_SIGNING_PRIVATE_KEY: Optional[str] = None

    # Admin
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "changeme"

    # Cloudflare Turnstile (admin login). No default: when unset, every login is
    # rejected rather than silently skipping the bot check.
    TURNSTILE_SECRET_KEY: Optional[str] = None

    # CORS — comma-separated origins (scheme + host[:port], no trailing slash,
    # no wildcard) allowed to make credentialed cross-origin requests to this
    # API. nginx normally serves the admin UI and proxies /api/ to this app on
    # the SAME origin (see license-admin-ui/nginx.conf), so the browser treats
    # those calls as same-origin and this setting never comes into play for
    # that path. It matters only for the documented alternate deployment where
    # the built admin UI is pointed at a separately hosted API via
    # VITE_LICENSE_API_URL — list exactly that UI's origin(s) here. No
    # default: an empty list allows no cross-origin caller, which is the safe
    # starting point rather than silently trusting every origin.
    CORS_ALLOWED_ORIGINS: str = ""

    # --- Internal portal integration (http://172.16.120.80) -----------------
    # Shared HS256 secret the portal signs its short-lived service tokens with.
    # Deliberately separate from SECRET_KEY: that one signs admin UI sessions,
    # and a single key for both would let a leaked portal secret mint admin
    # tokens (and let a portal token be replayed as an admin one). No default —
    # unset means every /internal/portal/* call is rejected, rather than the
    # endpoints coming up validating signatures against an empty string.
    PORTAL_JWT_SECRET: Optional[str] = None

    # Expected `iss` and `aud` claims. Must match what the portal puts in the
    # tokens it signs. Defaulted so the three documented env vars are all a
    # deployment needs, but configurable because pinning them in code would
    # make a portal-side rename a code change here.
    PORTAL_JWT_ISSUER: str = "portal"
    PORTAL_JWT_AUDIENCE: str = "license-server"

    # Ceiling on a portal token's lifetime (exp - iat). The integration spec
    # says 5 minutes; 600s leaves headroom without accepting a token the portal
    # accidentally issued for a month. It also bounds how long each `jti` has
    # to be retained in Redis for replay detection.
    PORTAL_JWT_MAX_LIFETIME_SECONDS: int = 600

    # Tolerance for clock drift between the two hosts when checking exp/iat.
    PORTAL_JWT_LEEWAY_SECONDS: int = 30

    # Where the portal lives. License events are POSTed to
    # {PORTAL_BASE_URL}/internal/ls/license-events; phase 4 will read the
    # users/orders endpoints from the same base. Setting this to "" turns
    # outbound notification off entirely (see PortalNotifier.enabled).
    PORTAL_BASE_URL: str = "http://172.16.120.80"

    # --- Outbound license events (app/core/portal_events.py) ----------------
    # iss/aud for the tokens WE sign, the reverse of the inbound pair above.
    # The reversal is load-bearing: it is what stops one of our own
    # notification tokens being replayed against /internal/portal/*, which
    # requires iss="portal"/aud="license-server".
    PORTAL_EVENT_JWT_ISSUER: str = "license-server"
    PORTAL_EVENT_JWT_AUDIENCE: str = "portal"

    # Lifetime of a notification token. 300s = the 5 minutes the integration
    # spec calls for; a fresh one is minted per delivery attempt.
    PORTAL_EVENT_JWT_LIFETIME_SECONDS: int = 300

    # Total attempts per event, not retries on top of the first try: 3 means
    # one POST plus two retries, backing off ~1s then ~2s.
    PORTAL_EVENT_MAX_ATTEMPTS: int = 3
    PORTAL_EVENT_BACKOFF_BASE_SECONDS: float = 1.0

    # Per-request timeout. Short on purpose: an unresponsive portal should be
    # retried, not waited on — no caller is blocked either way, but a long
    # timeout ties up a worker and lets the queue back up.
    PORTAL_EVENT_TIMEOUT_SECONDS: float = 5.0

    # Threads draining the event queue. Two is enough to keep one slow
    # delivery (up to ~7s of backoff) from stalling everything behind it.
    PORTAL_EVENT_WORKERS: int = 2

    # Queue ceiling. Once full, new events are dropped with an ERROR rather
    # than blocking the request that produced them — the whole point of this
    # being fire-and-forget. 1000 absorbs a long portal outage; beyond that,
    # the portal should reconcile via GET /internal/portal/licenses/{id}.
    PORTAL_EVENT_QUEUE_MAXSIZE: int = 1000

    # How long a shutdown waits for the queue to drain before giving up.
    # Kept under Docker's default 10s SIGTERM grace so the drain is not cut
    # short by SIGKILL.
    PORTAL_EVENT_SHUTDOWN_TIMEOUT_SECONDS: float = 5.0

    # How often to look for licenses that have expired since the last sweep,
    # and how many to emit per pass. 0 disables the sweep.
    #
    # Needed because nothing else in this codebase reacts to expiry: expires_at
    # is only checked lazily, when a client calls validate/activate. Without
    # the sweep, a customer who stops using the software would expire and the
    # portal would never hear about it.
    PORTAL_EXPIRY_SWEEP_INTERVAL_SECONDS: int = 300
    PORTAL_EXPIRY_SWEEP_BATCH_SIZE: int = 500

    # --- Portal user/order cache sync (app/core/portal_sync.py) -------------
    # How often to pull GET /internal/ls/users and /internal/ls/orders into
    # portal_users_cache / portal_orders_cache. 900s = 15 minutes. Setting it
    # to 0 disables the periodic sync but leaves POST /api/admin/portal/sync
    # working, which is the right setting if an admin would rather sync on
    # demand than have the portal polled.
    PORTAL_SYNC_INTERVAL_SECONDS: int = 900

    # `per_page` sent to the portal. 50 is the value the integration spec
    # documents in its example URLs.
    PORTAL_SYNC_PAGE_SIZE: int = 50

    # Hard cap on pages walked per endpoint per sync. This is a guard rail, not
    # a limit on real data: a portal that ignores `page` would return a full
    # first page forever and the pagination loop would never terminate. At the
    # default page size this still allows 10,000 records per entity.
    PORTAL_SYNC_MAX_PAGES: int = 200

    # Per-request timeout for a portal read. Longer than the event notifier's
    # 5s because a page of 50 records with a database query behind it is a
    # heavier request than accepting an event, and nothing is waiting on it.
    PORTAL_SYNC_TIMEOUT_SECONDS: float = 15.0

    # Rows per INSERT ... ON CONFLICT statement. Bounded so a large portal does
    # not build one enormous statement and exceed the driver's bind parameter
    # limit.
    PORTAL_SYNC_UPSERT_CHUNK_SIZE: int = 500

    # Comma-separated IPs or CIDR blocks allowed to call /internal/portal/*.
    # No default: unset means no caller is allowed, which fails closed.
    #
    # This is the app-level half of a two-layer check. The other half is
    # `allow`/`deny` in license-admin-ui/nginx.conf, which is the real gate —
    # see app/core/portal_auth.py for why the app cannot safely be the only one.
    PORTAL_IP_WHITELIST: str = ""

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ALLOWED_ORIGINS.split(",") if origin.strip()]

    @property
    def portal_ip_whitelist(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        """PORTAL_IP_WHITELIST parsed into networks.

        Accepts both a bare address ("172.16.120.80", which becomes a /32) and
        a CIDR block ("172.16.120.0/24"), so widening the allowance to a subnet
        later is a config change rather than a code change.

        strict=False so "172.16.120.80/24" is read as the containing network
        instead of raising. An unparseable entry raises ValueError here, at
        first use — better a 500 with a clear cause in the log than an entry
        that is silently dropped and quietly narrows the whitelist.
        """
        return [
            ipaddress.ip_network(entry.strip(), strict=False)
            for entry in self.PORTAL_IP_WHITELIST.split(",")
            if entry.strip()
        ]

settings = Settings()
