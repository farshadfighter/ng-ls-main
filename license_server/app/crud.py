from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from . import models, schemas
from .core.portal_events import LicenseEvent, build_event_payload, notifier
from datetime import datetime, timedelta, timezone
import logging
import secrets
import string
import hashlib

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
#   updated  activate_license (first bind), downgrade_to_pilot (plan change)
#   revoked  deactivate_license, delete_license
#   expired  the sweep in portal_events.py, not this module
#
#   heartbeat()          no event. A daily liveness ping per license that
#                        changes only last_heartbeat_at — none of which is in
#                        the payload. It would be pure volume.
#   consume_operation()  no event. It moves used_audits/used_hardens, and the
#                        event payload the integration defines carries neither,
#                        so the portal would receive a message identical to the
#                        previous one. Add usage fields to the payload first if
#                        the portal wants to track consumption.
#   validate_license()   no event for the success path (it only touches
#                        last_validated_at). Its downgrade path does emit, via
#                        downgrade_to_pilot.

def generate_license_key() -> str:
    """generate license key: XXXX-XXXX-XXXX-XXXX"""
    chars = string.ascii_uppercase + string.digits
    parts = [''.join(secrets.choice(chars) for _ in range(4)) for _ in range(4)]
    return '-'.join(parts)

def generate_organization_token(org_name: str, customer_email: str) -> str:
    """generate unique token for each company"""
    unique_string = f"{org_name}:{customer_email}:{secrets.token_hex(16)}"
    return hashlib.sha256(unique_string.encode()).hexdigest()

def get_plan_limits(plan_type: models.PlanType) -> dict:
    """each plan limit

    Asset Management has no license entitlement — only audit and hardening
    operations are quota-gated.
    """
    limits = {
        models.PlanType.PILOT: {
            "max_audits": 2,
            "max_hardens": 2,
            "duration_days": 30
        },
        models.PlanType.PLAN_100: {
            "max_audits": 100,
            "max_hardens": 100,
            "duration_days": 365
        },
        models.PlanType.PLAN_250: {
            "max_audits": 250,
            "max_hardens": 250,
            "duration_days": 365
        },
        models.PlanType.PLAN_500: {
            "max_audits": 500,
            "max_hardens": 500,
            "duration_days": 365
        },
        models.PlanType.UNLIMITED: {
            "max_audits": None,
            "max_hardens": None,
            "duration_days": 365
        }
    }
    return limits.get(plan_type, limits[models.PlanType.PILOT])

def create_license(db: Session, license_data: schemas.LicenseCreate) -> models.License:
    license_key = generate_license_key()
    org_token = generate_organization_token(license_data.organization_name, license_data.customer_email)
    
    limits = get_plan_limits(license_data.plan_type)
    expires_at = datetime.now(timezone.utc) + timedelta(days=limits["duration_days"])
    
    db_license = models.License(
        license_key=license_key,
        organization_token=org_token,
        customer_name=license_data.customer_name,
        customer_email=license_data.customer_email,
        organization_name=license_data.organization_name,
        plan_type=license_data.plan_type,
        max_audits=limits["max_audits"],
        max_hardens=limits["max_hardens"],
        expires_at=expires_at,
        is_pilot_mode=(license_data.plan_type == models.PlanType.PILOT),
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

def activate_license(db: Session, license_key: str, vm_fingerprint: str) -> tuple[bool, str, models.License]:
    license = get_license_by_key(db, license_key)
    
    if not license:
        return False, "license key not found", None
    
    if not license.is_active:
        return False, "license is deactivate", None
    
    if license.expires_at < datetime.now(timezone.utc):
        return False, "license is expierd", None
    
    # چک VM fingerprint - فقط یک ماشین مجاز
    if license.vm_fingerprint and license.vm_fingerprint != vm_fingerprint:
        return False, "this license already activate in another VM", None
    
    if not license.vm_fingerprint:
        license.vm_fingerprint = vm_fingerprint
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
    
    # چک heartbeat - اگر بیش از 48 ساعت قطع بود
    if license.last_heartbeat_at:
        time_since_last = datetime.now(timezone.utc) - license.last_heartbeat_at
        if time_since_last > timedelta(hours=48):
            # تبدیل به Pilot
            downgrade_to_pilot(db, license)
            return False, "لایسنس به دلیل قطع ارتباط به حالت Pilot تبدیل شد", license
    
    license.last_validated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(license)
    
    return True, "license is valid", license

def heartbeat(db: Session, license_key: str, org_token: str, vm_fingerprint: str) -> tuple[bool, str, bool]:
    """چک روزانه - باید هر روز صدا زده شود"""
    license = get_license_by_key(db, license_key)
    
    if not license:
        return False, "license key has not found", False
    
    if license.organization_token != org_token or license.vm_fingerprint != vm_fingerprint:
        return False, "faild to authintication", False
    
    # چک آیا باید downgrade شود
    should_downgrade = False
    if license.last_heartbeat_at:
        time_since_last = datetime.now(timezone.utc) - license.last_heartbeat_at
        if time_since_last > timedelta(hours=48):
            downgrade_to_pilot(db, license)
            should_downgrade = True
    
    license.last_heartbeat_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(license)
    
    return True, "Heartbeat is ok", should_downgrade

def downgrade_to_pilot(db: Session, license: models.License):
    """convert license to Pilot"""
    pilot_limits = get_plan_limits(models.PlanType.PILOT)
    
    license.is_pilot_mode = True
    license.plan_type = models.PlanType.PILOT
    license.max_audits = pilot_limits["max_audits"]
    license.max_hardens = pilot_limits["max_hardens"]

    db.commit()

    # Emitted here rather than at the two call sites (validate_license and
    # heartbeat), so the event cannot be forgotten if a third caller appears.
    # This one genuinely changes the payload: plan_type and is_pilot_mode.
    notifier.notify(LicenseEvent.UPDATED, license)

def consume_operation(db: Session, license_key: str, org_token: str, vm_fingerprint: str,
                     operation_type: str, count: int) -> tuple[bool, str, models.License]:
    """مصرف عملیات (audit, harden)"""

    valid, message, license = validate_license(db, license_key, org_token, vm_fingerprint)

    if not valid:
        return False, message, None

    operation_map = {
        "audit": ("max_audits", "used_audits"),
        "harden": ("max_hardens", "used_hardens"),
    }
    
    if operation_type not in operation_map:
        return False, "Operation is invalid", None
    
    max_field, used_field = operation_map[operation_type]
    max_value = getattr(license, max_field)
    used_value = getattr(license, used_field)

    # چک محدودیت (None = نامحدود)
    # Only enforce the ceiling for capped plans. The usage counter is always
    # incremented — including the Unlimited plan, where max_value is None —
    # so the frontend can report real usage instead of a frozen 0.
    if max_value is not None and used_value + count > max_value:
        return False, f"limit {operation_type} has expierd", license

    setattr(license, used_field, used_value + count)
    db.commit()
    db.refresh(license)

    return True, "operation successfully", license

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

    Differs from create_license (the admin path) in three ways:
      * expires_at honours data.duration_days when the portal sends one,
        because the order decides the term, not the plan catalog;
      * is_pilot_mode is taken from the portal when set, instead of being
        derived from plan_type == PILOT;
      * max_audits/max_hardens still come from get_plan_limits — the portal
        body carries no quota fields, so the plan stays authoritative for
        entitlements even when it is not authoritative for duration.
    """
    existing = get_license_by_order_id(db, data.order_id)
    if existing is not None:
        logger.info(
            "[portal] order_id=%s already has license id=%s; returning it unchanged",
            data.order_id,
            existing.id,
        )
        return existing, False

    limits = get_plan_limits(data.plan_type)
    duration_days = data.duration_days if data.duration_days is not None else limits["duration_days"]

    is_pilot_mode = (
        data.is_pilot_mode
        if data.is_pilot_mode is not None
        else (data.plan_type == models.PlanType.PILOT)
    )

    now = datetime.now(timezone.utc)
    db_license = models.License(
        license_key=generate_license_key(),
        organization_token=generate_organization_token(
            data.organization_name, data.customer_email
        ),
        customer_name=data.customer_name,
        customer_email=data.customer_email,
        organization_name=data.organization_name,
        plan_type=data.plan_type,
        max_audits=limits["max_audits"],
        max_hardens=limits["max_hardens"],
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
        data.plan_type.value,
    )

    # Only on a real creation. An idempotent replay returns above without
    # emitting: the portal is not told a license was created twice, and its
    # retry gets the license in the 200 response body regardless.
    notifier.notify(LicenseEvent.CREATED, db_license)
    return db_license, True
