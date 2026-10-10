"""HTTP client for the portal's read endpoints (phase 4), and the shared
outbound service token.

    GET {PORTAL_BASE_URL}/internal/ls/users?page=&per_page=&q=
    GET {PORTAL_BASE_URL}/internal/ls/orders?page=&per_page=

Both are paginated and both authenticate with the same token this server
already sends on license events: HS256 over PORTAL_JWT_SECRET, with
iss="license-server" and aud="portal" — the reverse of the pair we accept
inbound, which is what stops one of our own tokens being replayed against
/internal/portal/* (see app/core/portal_auth.py).

Tolerant parsing, on purpose
----------------------------
The exact response envelope and field names the portal returns were not
specified, and this code could not be tested against the real portal. So
rather than assume one shape and break on contact, both are read tolerantly:

* ``extract_records`` accepts a bare JSON array, or an object wrapping the rows
  under any of "items"/"data"/"results"/"users"/"orders" — the four or five
  conventions a Django/Laravel/FastAPI portal is likely to use.
* the per-record field mapping (``USER_FIELDS`` / ``ORDER_FIELDS`` in
  app/core/portal_sync.py) tries several plausible key names per column.

Every record is also stored whole in the cache table's ``raw`` JSONB column, so
nothing the portal sends is lost even when a mapping guesses wrong: a
mis-mapped column can be recomputed from ``raw`` without a re-sync.

If the portal's real shape turns out to be none of these, the fix is one entry
in those tables, not a rewrite. Check the logged warning — it names the keys it
actually received.
"""
import logging
import time
import uuid
from typing import Any, Optional

import requests
import jwt

from .config import settings

logger = logging.getLogger(__name__)

USERS_PATH = "/internal/ls/users"
ORDERS_PATH = "/internal/ls/orders"

# Envelope keys to look for, in order, before falling back to a bare list.
_RECORD_KEYS = ("items", "data", "results", "records", "users", "orders", "rows")


class PortalClientError(RuntimeError):
    """A portal read failed. Carries a message already safe to log."""


def create_portal_token() -> str:
    """Mint a short-lived service token for one outbound request.

    One token per request, never reused: we reject replayed `jti` values on our
    own inbound endpoints, so the portal plausibly does the same, and a token
    reused across a retry (or across the pages of one sync) would be refused.

    This is the single place the outbound claims are built —
    portal_events.create_event_token delegates here — so the iss/aud reversal
    cannot drift between the event notifier and the sync.
    """
    now = int(time.time())
    return jwt.encode(
        {
            "iss": settings.PORTAL_EVENT_JWT_ISSUER,
            "aud": settings.PORTAL_EVENT_JWT_AUDIENCE,
            "iat": now,
            "exp": now + settings.PORTAL_EVENT_JWT_LIFETIME_SECONDS,
            "jti": str(uuid.uuid4()),
        },
        settings.PORTAL_JWT_SECRET,
        algorithm="HS256",
    )


def extract_records(body: Any, path: str) -> list[dict]:
    """Pull the list of rows out of whatever envelope the portal used.

    Returns [] for an empty page. Raises PortalClientError if the body is
    neither a list nor an object containing one, because that means the
    endpoint returned something entirely unexpected (an HTML error page, say)
    and continuing would silently cache nothing.
    """
    if isinstance(body, list):
        return [r for r in body if isinstance(r, dict)]

    if isinstance(body, dict):
        for key in _RECORD_KEYS:
            value = body.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
        raise PortalClientError(
            f"{path}: no record list found in the response. Looked for "
            f"{', '.join(_RECORD_KEYS)}; the body's keys were "
            f"{sorted(body)[:12]}. Add the right key to _RECORD_KEYS in "
            f"app/core/portal_client.py."
        )

    raise PortalClientError(
        f"{path}: expected a JSON list or object, got {type(body).__name__}"
    )


def _get_page(path: str, page: int, per_page: int, q: Optional[str]) -> list[dict]:
    """One page. Raises PortalClientError on any failure."""
    url = f"{settings.PORTAL_BASE_URL.rstrip('/')}{path}"
    params: dict[str, Any] = {"page": page, "per_page": per_page}
    if q:
        params["q"] = q

    try:
        response = requests.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {create_portal_token()}"},
            timeout=settings.PORTAL_SYNC_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise PortalClientError(f"GET {url} page={page}: {type(exc).__name__}: {exc}")

    if not response.ok:
        # Truncated: an HTML error page would otherwise fill the log.
        raise PortalClientError(
            f"GET {url} page={page}: HTTP {response.status_code}: "
            f"{response.text[:200]}"
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise PortalClientError(f"GET {url} page={page}: response is not JSON ({exc})")

    return extract_records(body, path)


def fetch_paginated(
    path: str, q: Optional[str] = None, per_page: Optional[int] = None
) -> list[dict]:
    """Every record from a paginated portal endpoint.

    Termination is by short page — a page returning fewer rows than requested
    is the last one — rather than by reading a `total` or `next` field, because
    that rule holds for every pagination convention and needs no agreement
    about the envelope's metadata.

    PORTAL_SYNC_MAX_PAGES bounds the walk. Without it, a portal that ignores
    the `page` parameter (and so returns a full first page forever) would spin
    here until the process died. Hitting the cap logs a warning and returns
    what was collected; it is a guard rail, not a normal outcome.
    """
    per_page = per_page or settings.PORTAL_SYNC_PAGE_SIZE
    records: list[dict] = []
    seen_pages = 0

    for page in range(1, settings.PORTAL_SYNC_MAX_PAGES + 1):
        batch = _get_page(path, page, per_page, q)
        seen_pages += 1
        records.extend(batch)

        if len(batch) < per_page:
            break
    else:
        logger.warning(
            "[portal_sync] %s: stopped at the %d-page cap with %d records; "
            "either there is genuinely more data (raise PORTAL_SYNC_MAX_PAGES) "
            "or the portal is ignoring the `page` parameter",
            path,
            settings.PORTAL_SYNC_MAX_PAGES,
            len(records),
        )

    logger.info(
        "[portal_sync] %s: fetched %d record(s) over %d page(s)",
        path,
        len(records),
        seen_pages,
    )
    return records


def fetch_users(q: Optional[str] = None) -> list[dict]:
    return fetch_paginated(USERS_PATH, q=q)


def fetch_orders(q: Optional[str] = None) -> list[dict]:
    """Orders. `q` is accepted for symmetry but the integration spec documents
    no `q` on the orders endpoint, so the sync never passes one; order search
    in the admin UI runs against the local cache."""
    return fetch_paginated(ORDERS_PATH, q=q)
