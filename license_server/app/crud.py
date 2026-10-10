from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from . import models, offline, plans, schemas
from .core.portal_events import LicenseEvent, build_event_payload, notifier
from datetime import datetime, timedelta, timezone
import logging
import secrets
import string
import hashlib
from typing import Optional

logger = logging.getLogger(__name__)

# Portal notification (app/core/portal_events.py) is attached here rather than
# in the routers, so that every path that changes a license emits an event —
# the admin API, the portal API and the licensed app all funnel through this
# module. A notifier call is always AFTER the commit it reports (an event for a
# change that then rolls back is not self-correcting, unlike a lost event), and
# never raises: notifier.notify swallows everything.
#
# Which transitions emit, and which deliberately do not:
#
#   created  create_license, create_license_from_portal
#   updated  activate_license (first bind), reset_binding, change_plan,
#            set_offline_allowed, issue_offline_license
#   revoked  deactivate_license, delete_license
#   expired  the sweep in portal_events.py, not this module
#
#   heartbeat()          no event. A liveness ping per license that changes
#                        only last_heartbeat_at and the reported asset count —
#                        none of which is in the payload. It would be pure volume.
#   validate_license()   no event (it only touches last_validated_at).
#
# Nothing here changes a license because its machine was offline. NGCorion
# falls back to the pilot entitlement by itself after 48 hours without a
# signed answer, and returns to the license's own plan when it reaches this
# server again; an offline period never costs the customer the plan they paid
# for. (Up to licensing v2, validate and heartbeat overwrote the plan with the
# pilot plan for good.)

def generate_license_key() -> str:
    """generate license key: XXXX-XXXX-XXXX-XXXX"""
    chars = string.ascii_uppercase + string.digits
    parts = [''.join(secrets.choice(chars) for _ in range(4)) for _ in range(4)]
    return '-'.join(parts)

def generate_organization_token(org_name: str, customer_email: str) -> str:
    """generate unique token for each company"""
    unique_string = f"{org_name}:{customer_email}:{secrets.token_hex(16)}"
    return hashlib.sha256(unique_string.encode()).hexdigest()

def plan_fields(plan_code: str, addon_packs: int = 0) -> dict:
    """The License columns a plan sets: plan_code, max_assets, addon_packs.

    plans.UnknownPlan / plans.InvalidAddons (both ValueError) for a plan this
    server does not sell or add-ons that plan cannot take.
    """
    plan = plans.resolve(plan_code)
    packs = plans.check_addons(plan, addon_packs)
    return {"plan_code": plan.code, "max_assets": plan.max_assets(packs), "addon_packs": packs}

def create_license(db: Session, license_data: schemas.LicenseCreate) -> models.License:
    license_key = generate_license_key()
    org_token = generate_organization_token(license_data.organization_name, license_data.customer_email)

    plan = plans.resolve(license_data.plan_code)
    expires_at = datetime.now(timezone.utc) + timedelta(days=plan.duration_days)

    db_license = models.License(
        license_key=license_key,
        organization_token=org_token,
        customer_name=license_data.customer_name,
        customer_email=license_data.customer_email,
        organization_name=license_data.organization_name,
        **plan_fields(plan.code, license_data.addon_packs),
        expires_at=expires_at,
        is_pilot_mode=plan.pilot,
        offline_allowed=license_data.offline_allowed,
        # Optional portal linkage, chosen by the admin from the phase-4 cache.
        # source stays at its 'manual' default: a person created this, so it
        # falls outside the portal's idempotency index on order_id.
        user_uuid7=license_data.user_uuid7,
        order_id=license_data.order_id,
        order_number=license_data.order_number,
    )

    db.add(db_license)
    db.commit()
    db.refresh(db_license)

    notifier.notify(LicenseEvent.CREATED, db_license)
    return db_license

def get_license_by_key(db: Session, license_key: str) -> models.License:
    return db.query(models.License).filter(models.License.license_key == license_key).first()

def get_license_by_id(db: Session, license_id: int) -> models.License:
    """Look a license up by its integer primary key.

    The rest of this module keys on license_key; this exists because the portal
    integration's `license_id` is licenses.id — that is the handle the portal
    stores against its order and passes back to
    GET /internal/portal/licenses/{license_id}.
    """
    return db.query(models.License).filter(models.License.id == license_id).first()

def get_license_by_order_id(db: Session, order_id: int) -> models.License:
    """The portal-created license for a portal order, if one exists.

    Filtered to source='portal_auto' to match the partial unique index that
    guarantees at most one row here (see models.License.__table_args__). A
    manually created license that happens to carry the same order_id is not
    treated as the portal's — the portal only ever owns rows it created.
    """
    return (
        db.query(models.License)
        .filter(
            models.License.order_id == order_id,
            models.License.source == models.LicenseSource.PORTAL_AUTO.value,
        )
        .first()
    )

def get_license_by_token(db: Session, org_token: str) -> models.License:
    return db.query(models.License).filter(models.License.organization_token == org_token).first()

def activate_license(db: Session, license_key: str, vm_fingerprint: str,
                     client_computed: bool = False) -> tuple[bool, str, models.License]:
    """Bind a license to a machine (one machine per license) and return it.

    `client_computed` is True for NGCorion releases with signed licensing,
    which compute the fingerprint on the licensed machine (they send a nonce).
    A license whose binding was not made that way - bound to the license
    server's own fingerprint by an older release - may move once to a
    client-computed fingerprint.
    """
    license = get_license_by_key(db, license_key)
    
    if not license:
        return False, "license key not found", None
    
    if not license.is_active:
        return False, "license is deactivate", None
    
    if license.expires_at < datetime.now(timezone.utc):
        return False, "license is expierd", None
    
    # One machine per license.
    if license.vm_fingerprint and license.vm_fingerprint != vm_fingerprint:
        if client_computed and license.fingerprint_source != "client":
            # Bound by an NGCorion release that asked THIS server for the
            # fingerprint (GET /api/fingerprint), so every installation got the
            # license server's own fingerprint and the binding meant nothing.
            # Such a license may move once to a real, client-computed
            # fingerprint; after that the normal one-machine rule applies.
            logger.warning(
                "[License] %s had a legacy binding (license server's own "
                "fingerprint); rebinding it to the client's machine",
                license.license_key,
            )
            license.vm_fingerprint = None
        else:
            return False, "this license already activate in another VM", None
    
    if not license.vm_fingerprint:
        license.vm_fingerprint = vm_fingerprint
        license.fingerprint_source = "client" if client_computed else None
        license.activated_at = datetime.now(timezone.utc)
        license.last_heartbeat_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(license)

        # Only on the first bind. A re-activation with the same fingerprint
        # falls through here unchanged and is not an event.
        #
        # Note that activation changes no field the event payload carries —
        # activated_at is not one of them. The event is the signal; the portal
        # reads the detail from GET /internal/portal/licenses/{license_id},
        # which does expose activated_at and is_activated.
        notifier.notify(LicenseEvent.UPDATED, license)

    return True, "license activate successfully", license

def reset_binding(db: Session, license_key: str) -> models.License:
    """Release a license from its machine (admin action, e.g. after a hardware move or a
    reinstall); the next activation binds it again. None when the key is unknown.
    """
    license = get_license_by_key(db, license_key)
    if license is None:
        return None
    license.vm_fingerprint = None
    license.fingerprint_source = None
    db.commit()
    db.refresh(license)
    notifier.notify(LicenseEvent.UPDATED, license)
    return license

def validate_license(db: Session, license_key: str, org_token: str, vm_fingerprint: str) -> tuple[bool, str, models.License]:
    license = get_license_by_key(db, license_key)
    
    if not license:
        return False, "license key not found", None
    
    if license.organization_token != org_token:
        return False, "company token is invalid", None
    
    if not license.is_active:
        return False, "license is deactive", None
    
    if license.expires_at < datetime.now(timezone.utc):
        return False, "license has expierd", None
    
    if license.vm_fingerprint != vm_fingerprint:
        return False, "VM fingerprint is not match", None

    license.last_validated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(license)
    
    return True, "license is valid", license

def heartbeat(db: Session, license_key: str, org_token: str, vm_fingerprint: str,
              asset_count: Optional[int] = None) -> tuple[bool, str]:
    """Liveness ping from the licensed machine, with how many assets it manages.

    Only authenticates the caller and records the contact; whether the license
    is still usable is the router's call for the signed answer.
    """
    license = get_license_by_key(db, license_key)

    if not license:
        return False, "license key has not found"

    if license.organization_token != org_token or license.vm_fingerprint != vm_fingerprint:
        return False, "faild to authintication"

    now = datetime.now(timezone.utc)
    license.last_heartbeat_at = now
    if asset_count is not None:
        license.reported_assets = asset_count
        license.reported_assets_at = now
    db.commit()
    db.refresh(license)

    return True, "Heartbeat is ok"

def change_plan(db: Session, license_key: str, plan_code: Optional[str] = None,
                addon_packs: Optional[int] = None) -> Optional[models.License]:
    """Move a license to another plan and/or another number of add-on packs (an upgrade, add-ons
    bought later, or correcting a license the old server downgraded). The term is unchanged.
    None when the key is unknown; plans.UnknownPlan / InvalidAddons for an impossible change.
    """
    license = get_license_by_key(db, license_key)
    if license is None:
        return None
    code = plan_code or license.plan_code
    plan = plans.resolve(code)
    # Moving to a plan without add-ons drops the packs unless new ones are given.
    if addon_packs is None:
        addon_packs = license.addon_packs if plan.addons_allowed else 0
    fields = plan_fields(plan.code, addon_packs)
    for name, value in fields.items():
        setattr(license, name, value)
    if plan_code is not None:
        license.is_pilot_mode = plan.pilot
    db.commit()
    db.refresh(license)
    notifier.notify(LicenseEvent.UPDATED, license)
    return license

def set_offline_allowed(db: Session, license_key: str, allowed: bool) -> Optional[models.License]:
    """Mark a license offline (air-gapped) or not. Files already issued keep working until their
    valid_until; this only decides whether new ones may be issued. None when the key is unknown."""
    license = get_license_by_key(db, license_key)
    if license is None:
        return None
    license.offline_allowed = bool(allowed)
    db.commit()
    db.refresh(license)
    notifier.notify(LicenseEvent.UPDATED, license)
    return license


class OfflineIssueRefused(Exception):
    """An offline license file cannot be issued for this license (a 409 with the reason)."""


def offline_issue_refusal(license: models.License, request: "offline.OfflineRequest",
                          now: datetime) -> Optional[str]:
    """Why no offline file can be issued for this license and request, or None when one can.
    The one rule, shared by issuing and by the admin UI's file check (inspect_offline_file)."""
    if request.license_key != license.license_key:
        return f"The request is for license {request.license_key}, not {license.license_key}"
    if not license.offline_allowed:
        return "This license is not an offline license; enable the offline option first"
    if not license.is_active:
        return "This license is revoked"
    if license.expires_at <= now:
        return "This license has expired"
    if license.vm_fingerprint and license.vm_fingerprint != request.vm_fingerprint:
        return ("This license is bound to another machine. If NGCorion moved to a new server, "
                "release the machine binding first")
    return None


def issue_offline_license(db: Session, license_key: str, request: "offline.OfflineRequest",
                          issued_by: Optional[str] = None) -> tuple[models.License, datetime]:
    """Check the license for an offline file, bind it to the request's machine and record the
    issue. Returns the license and how long the file is valid; the caller signs the file.

    OfflineIssueRefused when the license is unknown to the request, not offline, revoked,
    expired, or bound to another machine (an admin releases it first: reset_binding).
    """
    license = get_license_by_key(db, license_key)
    if license is None:
        raise OfflineIssueRefused("License not found")
    now = datetime.now(timezone.utc)
    refusal = offline_issue_refusal(license, request, now)
    if refusal:
        raise OfflineIssueRefused(refusal)

    if not license.vm_fingerprint:
        license.vm_fingerprint = request.vm_fingerprint
        license.fingerprint_source = "client"
        license.activated_at = now
    until = offline.valid_until(now, license.expires_at)
    license.offline_issued_at = now
    license.offline_valid_until = until
    if request.asset_count is not None:
        license.reported_assets = request.asset_count
        license.reported_assets_at = now
    db.add(models.OfflineLicenseIssue(
        license_id=license.id, issued_at=now, valid_until=until,
        vm_fingerprint=request.vm_fingerprint, reported_assets=request.asset_count, issued_by=issued_by,
    ))
    db.commit()
    db.refresh(license)
    notifier.notify(LicenseEvent.UPDATED, license)
    return license, until


def _check(ok: bool, text: str) -> dict:
    return {"ok": ok, "text": text}


def _when(ts) -> Optional[datetime]:
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _day(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC")


def inspect_offline_file(db: Session, text: str, public_raw: Optional[bytes], key_id: Optional[str]) -> dict:
    """What an offline request or license file says, checked against this server; changes
    nothing. For the admin UI's Test page.

    A request: its contents, and whether a license file could be issued for it now (the same
    rule as issuing). A license file: its claims, whether this server's key signed it, whether it
    is still within its term, whether it still matches the license's machine binding, and
    whether it is in the issue record.

    public_raw / key_id: this server's signing key; None when signing is not configured (the
    signature is then reported as unchecked). Raises offline.InvalidOfflineRequest /
    InvalidOfflineLicense for text that is neither kind of file.
    """
    now = datetime.now(timezone.utc)
    kind = offline.kind_of(text)

    if kind == "request":
        req = offline.parse_request(text)
        result = {
            "kind": "request", "license_key": req.license_key, "vm_fingerprint": req.vm_fingerprint,
            "asset_count": req.asset_count, "nonce": req.nonce, "created_at": req.created_at,
        }
        checks = [_check(True, "The request file is well formed")]
        license = get_license_by_key(db, req.license_key)
        if license is None:
            checks.append(_check(False, f"There is no license {req.license_key} on this server"))
        else:
            result.update(license_expires_at=license.expires_at, plan_code=license.plan_code,
                          plan_name=license.plan_name, max_assets=license.max_assets,
                          addon_packs=license.addon_packs)
            if not license.vm_fingerprint:
                checks.append(_check(True, "The license is not bound to a machine yet; issuing binds it to this one"))
            elif license.vm_fingerprint == req.vm_fingerprint:
                checks.append(_check(True, "The license is bound to this machine"))
            refusal = offline_issue_refusal(license, req, now)
            if refusal:
                checks.append(_check(False, refusal))
            else:
                until = offline.valid_until(now, license.expires_at)
                checks.append(_check(True, f"A license file can be issued; it would be valid until {_day(until)}"))
        result["checks"] = checks
        result["ok"] = all(c["ok"] for c in checks)
        return result

    if kind != "license":
        raise offline.InvalidOfflineRequest(
            "This is neither an NGCorion offline request (.ngcreq) nor a license file (.ngclic)."
        )

    parsed = offline.read_license_file(text, public_raw)
    signature_valid, claims = parsed.signature_valid, parsed.claims
    limits = claims.get("limits") if isinstance(claims.get("limits"), dict) else {}
    until = _when(claims.get("offline_until"))
    result = {
        "kind": "license", "license_key": claims.get("lic"), "vm_fingerprint": claims.get("fp"),
        "nonce": claims.get("nonce"), "created_at": _when(claims.get("iat")),
        "valid_until": until, "license_expires_at": _when(claims.get("exp")),
        "plan_code": claims.get("plan"), "plan_name": claims.get("plan_name"),
        "max_assets": limits.get("max_assets"), "addon_packs": limits.get("addon_packs"),
        "signature_valid": signature_valid, "key_id": claims.get("kid"), "claims": claims,
    }

    checks = []
    if signature_valid is None:
        checks.append(_check(False, "The signature could not be checked: signing is not configured on this server"))
    elif signature_valid:
        checks.append(_check(True, f"Signed by this server's key ({key_id})"))
    elif claims.get("kid") and claims.get("kid") != key_id:
        checks.append(_check(False, f"Signed with key {claims.get('kid')}, not this server's key ({key_id})"))
    else:
        checks.append(_check(False, "The signature does not match: the file was altered"))

    if not signature_valid:
        # Nothing in an unverified file can be relied on, so it is not compared with the license or
        # the issue record either: an altered file can carry a real license's key and dates.
        checks.append(_check(False, "Its contents are not verified, so they were not checked any further"))
        result["checks"] = checks
        result["ok"] = False
        return result

    if claims.get("typ") == "offline":
        checks.append(_check(True, "It is an offline license file"))
    else:
        checks.append(_check(False, f"It is not an offline license file (type {claims.get('typ')!r})"))
    if until is None:
        checks.append(_check(False, "It names no end date"))
    elif until > now:
        checks.append(_check(True, f"Valid until {_day(until)}"))
    else:
        checks.append(_check(False, f"It ended on {_day(until)}"))

    license = get_license_by_key(db, claims.get("lic")) if isinstance(claims.get("lic"), str) else None
    if license is None:
        checks.append(_check(False, f"There is no license {claims.get('lic')} on this server"))
    else:
        if not license.is_active:
            checks.append(_check(False, "The license has been revoked since. The file still works on its "
                                        "machine until its end date: an issued file cannot be recalled"))
        if license.vm_fingerprint and license.vm_fingerprint != claims.get("fp"):
            checks.append(_check(False, "The license is now bound to another machine"))
        issued = None
        if until is not None:
            issued = (
                db.query(models.OfflineLicenseIssue)
                .filter(models.OfflineLicenseIssue.license_id == license.id,
                        models.OfflineLicenseIssue.vm_fingerprint == claims.get("fp"))
                .all()
            )
            issued = next((i for i in issued if abs((i.valid_until - until).total_seconds()) < 1), None)
        if issued is not None:
            by = f" by {issued.issued_by}" if issued.issued_by else ""
            checks.append(_check(True, f"In the issue record: issued {_day(issued.issued_at)}{by}"))
        else:
            checks.append(_check(False, "Not in this server's issue record"))
    result["checks"] = checks
    result["ok"] = all(c["ok"] for c in checks)
    return result


def offline_issues(db: Session, license_id: int):
    """The offline files issued for a license, newest first."""
    return (
        db.query(models.OfflineLicenseIssue)
        .filter(models.OfflineLicenseIssue.license_id == license_id)
        .order_by(models.OfflineLicenseIssue.issued_at.desc())
        .all()
    )


def get_all_licenses(db: Session, skip: int = 0, limit: int = 100):
    return db.query(models.License).offset(skip).limit(limit).all()

def deactivate_license(db: Session, license_key: str) -> bool:
    license = get_license_by_key(db, license_key)
    if license:
        license.is_active = False
        db.commit()
        notifier.notify(LicenseEvent.REVOKED, license)
        return True
    return False

def delete_license(db: Session, license_key: str) -> bool:
    """Permanently remove a license row from the database.

    Unlike deactivate_license (which only flips is_active to False), this
    hard-deletes the record. Intended for cleaning up legacy/obsolete
    licenses that no longer need to be retained.
    """
    license = get_license_by_key(db, license_key)
    if license:
        # Snapshot BEFORE the delete — the row has to be readable to build the
        # payload — but enqueue only after the commit, so a failed delete does
        # not tell the portal a license was revoked when it still exists.
        payload = build_event_payload(LicenseEvent.REVOKED, license)
        db.delete(license)
        db.commit()
        notifier.notify_snapshot(payload)
        return True
    return False


def create_license_from_portal(
    db: Session, data: schemas.PortalLicenseCreate
) -> tuple[models.License, bool]:
    """Create a license on behalf of a portal order. Idempotent.

    Returns ``(license, created)`` — ``created`` is False when an existing
    license for this order_id was returned instead of a new one.

    Idempotency matters here specifically because the portal calls this as part
    of placing an order and will retry on any timeout or 5xx. Without it, one
    order that timed out after the license was committed would leave the
    customer with two license keys and the portal holding the wrong one.

    Two layers, because a pre-check alone is a race:

      1. a look-up on order_id, which handles the ordinary sequential retry;
      2. the partial unique index uq_licenses_portal_order_id, which catches
         two concurrent calls that both passed step 1 — the loser gets an
         IntegrityError and re-reads the winner's row.

    Differs from create_license (the admin path) in two ways:
      * expires_at honours data.duration_days when the portal sends one,
        because the order decides the term, not the plan catalog;
      * is_pilot_mode is taken from the portal when set, instead of being
        derived from the plan.
    The asset limit always comes from the catalog (plan + add-on packs): the
    portal names what was bought, not what it entitles.
    """
    existing = get_license_by_order_id(db, data.order_id)
    if existing is not None:
        logger.info(
            "[portal] order_id=%s already has license id=%s; returning it unchanged",
            data.order_id,
            existing.id,
        )
        return existing, False

    plan = plans.resolve(data.plan_code)
    duration_days = data.duration_days if data.duration_days is not None else plan.duration_days

    is_pilot_mode = data.is_pilot_mode if data.is_pilot_mode is not None else plan.pilot

    now = datetime.now(timezone.utc)
    db_license = models.License(
        license_key=generate_license_key(),
        organization_token=generate_organization_token(
            data.organization_name, data.customer_email
        ),
        customer_name=data.customer_name,
        customer_email=data.customer_email,
        organization_name=data.organization_name,
        **plan_fields(plan.code, data.addon_packs),
        expires_at=now + timedelta(days=duration_days),
        is_pilot_mode=is_pilot_mode,
        # Portal provenance
        user_uuid7=data.user_uuid7,
        order_id=data.order_id,
        order_number=data.order_number,
        source=models.LicenseSource.PORTAL_AUTO.value,
        # Created from the portal's own request, so the two agree as of now.
        portal_synced_at=now,
        portal_meta=data.meta,
    )

    db.add(db_license)
    try:
        db.commit()
    except IntegrityError:
        # Either the concurrent-create race on order_id, or the (vanishingly
        # unlikely) license_key/organization_token collision. Roll back and
        # check which.
        db.rollback()
        concurrent = get_license_by_order_id(db, data.order_id)
        if concurrent is not None:
            logger.info(
                "[portal] concurrent create for order_id=%s; returning license id=%s",
                data.order_id,
                concurrent.id,
            )
            return concurrent, False
        # Not the order_id index — a genuine failure the caller must hear about.
        raise

    db.refresh(db_license)
    logger.info(
        "[portal] created license id=%s for order_id=%s (order_number=%s, plan=%s)",
        db_license.id,
        data.order_id,
        data.order_number,
        db_license.plan_code,
    )

    # Only on a real creation. An idempotent replay returns above without
    # emitting: the portal is not told a license was created twice, and its
    # retry gets the license in the 200 response body regardless.
    notifier.notify(LicenseEvent.CREATED, db_license)
    return db_license, True
