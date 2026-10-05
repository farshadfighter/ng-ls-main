from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .database import engine, Base
from .routers import licenses, admin, admin_portal, internal_portal
from .middleware.logging import LoggingMiddleware
from .middleware.rate_limit import RateLimitMiddleware
from .core.config import settings
from .core.portal_events import notifier as portal_notifier
from .core.portal_sync import syncer as portal_syncer
from .utils.fingerprint import get_vm_fingerprint
from .schemas import FingerprintResponse

Base.metadata.create_all(bind=engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run the portal background workers alongside the app.

    Two independent pieces:
      * the license-event notifier (app/core/portal_events.py), which pushes
        create/update/revoke/expire events out to the portal;
      * the user/order cache sync (app/core/portal_sync.py), which pulls the
        portal's users and orders in every PORTAL_SYNC_INTERVAL_SECONDS.

    Started here rather than lazily on first use so that a misconfiguration
    (PORTAL_BASE_URL or PORTAL_JWT_SECRET missing) is logged once at boot,
    where someone will see it, instead of being discovered when the first
    license silently fails to notify or the admin's user list turns up empty.

    Starting either is a no-op when the integration is not configured, so a
    deployment with no portal is unaffected.

    On shutdown the notifier's queue is given a few seconds to drain, so a
    planned restart delivers what it already holds rather than dropping it.
    See app/core/portal_events.py for why delivery is nonetheless best-effort.
    A sync interrupted mid-run simply resumes from scratch on the next tick —
    it is an idempotent upsert, so a partial run costs nothing.
    """
    portal_notifier.start()
    portal_syncer.start()
    try:
        yield
    finally:
        # Reverse order: stop pulling before draining what is queued to push.
        portal_syncer.stop()
        portal_notifier.stop()


app = FastAPI(
    title="License Server API",
    description="License management and validation system",
    version="1.0.0",
    lifespan=lifespan,
)

# Add middleware
app.add_middleware(LoggingMiddleware)
app.add_middleware(RateLimitMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(licenses.router)
app.include_router(admin.router)
# Admin browsing of the portal user/order cache, under the same /api/admin
# prefix as admin.router but in its own file. Ordinary admin auth — these are
# NOT portal-facing endpoints.
app.include_router(admin_portal.router)
# Mounted at /internal/portal, outside the /api prefix, so nginx can restrict
# it to PORTAL_IP_WHITELIST separately from the public /api/ block. That nginx
# `location /internal/` block is required — without it these paths hit the
# SPA fallback and return 200 with index.html. See the router's module docstring.
app.include_router(internal_portal.router)

@app.get("/")
def root():
    return {"message": "License Server API is running"}

@app.get("/health")
def health_check():
    return {"status": "healthy"}

@app.get("/api/fingerprint", response_model=FingerprintResponse)
def get_fingerprint():
    """Get the hardware fingerprint of the machine THIS PROCESS is running on.

    Not called by anything in this repo (the admin UI's Test panel generates
    a random fingerprint client-side instead). Per
    license_server/LICENSE_SYSTEM_COMPLETE_GUIDE.md, the intended caller is
    the main app (e.g. NGCorion) hitting its own local license-server
    process at startup, before it has a license_key/organization_token to
    authenticate with — so this has to stay reachable pre-auth.

    Deliberately unauthenticated:
      - No secret is at risk: the response is a one-way sha256 hash of local
        MAC/machine-id/hostname/CPU strings (see app/utils/fingerprint.py),
        not those identifiers themselves.
      - The caller can't be authenticated yet anyway — this is called before
        activation, to produce the vm_fingerprint that activation itself
        needs.
    If this is ever deployed such that a REMOTE caller could reach this
    endpoint on a DIFFERENT machine than the one requesting activation, stop:
    it would return the license SERVER's own fingerprint, not the caller's,
    which breaks the one-VM-binding model entirely. This only makes sense
    called locally, in-process, on the machine being licensed.

    Returns:
        FingerprintResponse: Object containing the VM fingerprint string
    """
    fingerprint = get_vm_fingerprint()
    return {"fingerprint": fingerprint}
