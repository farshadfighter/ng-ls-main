"""
The audit trail: one row in audit_events per security-relevant event (app/models.py AuditEvent).

What is recorded
----------------
* admin authentication: login success and failure, lockout, logout, password change;
* admin account management: create, change, enable/disable, unlock, password reset;
* every change to a license from the admin console: create, plan change, revoke, permanent
  delete, machine release, offline option, offline file issued;
* license creation by the portal;
* from licensed NGCorion installations: activations (successful or not) and refused
  validations and heartbeats. Successful validations and heartbeats are not recorded - one per
  installation per hour would bury everything else - they are visible on the license itself
  (last_validated_at, last_heartbeat_at);
* start-up of the server, with the id of the key it signs with.

Each row: when (database time), who (admin user name, "portal", "client" or "system"), the
role, the source address, what ("area.verb"), the target (license key, user name), the outcome
and a small detail object. Passwords, tokens and keys are never written to it.

Rows are written in a session of their own and committed at once, so an event is kept even when
the request that caused it fails afterwards (a refused login is exactly such a case). The table
is append-only (a database trigger refuses UPDATE and DELETE). Each event also goes to the log
(logger "audit"), as one JSON line, for a log collector or SIEM.
"""
import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from . import models
from .database import SessionLocal

logger = logging.getLogger("audit")

SUCCESS = "success"
FAILURE = "failure"


def _clip(value: Optional[str], size: int) -> Optional[str]:
    if value is None:
        return None
    value = str(value)
    return value if len(value) <= size else value[: size - 1] + "…"


def record(action: str, outcome: str, *, actor: str, role: Optional[str] = None, ip: Optional[str] = None,
           target: Optional[str] = None, detail: Optional[Dict[str, Any]] = None) -> None:
    """Write one audit event. Never raises: a failure to audit is logged as an error, but does
    not turn the action itself into a failure."""
    row = models.AuditEvent(
        actor=_clip(actor or "unknown", 64), actor_role=_clip(role, 16), ip=_clip(ip, 64),
        action=_clip(action, 64), target=_clip(target, 255), outcome=outcome, detail=detail or None,
    )
    db = SessionLocal()
    try:
        db.add(row)
        db.commit()
        at = row.at
    except Exception as exc:  # noqa: BLE001 - auditing must not break the request
        db.rollback()
        logger.error("could not write audit event %s/%s by %s: %s", action, outcome, actor, exc)
        at = None
    finally:
        db.close()
    logger.info(json.dumps({
        "at": at.isoformat() if isinstance(at, datetime) else None, "actor": actor, "role": role,
        "ip": ip, "action": action, "target": target, "outcome": outcome, "detail": detail or None,
    }, ensure_ascii=False, default=str))


def search(db: Session, *, actor: Optional[str] = None, action: Optional[str] = None,
           outcome: Optional[str] = None, target: Optional[str] = None,
           since: Optional[datetime] = None, until: Optional[datetime] = None,
           limit: int = 100, offset: int = 0):
    """Audit events, newest first, filtered; (rows, total)."""
    query = db.query(models.AuditEvent)
    if actor:
        query = query.filter(models.AuditEvent.actor == actor.lower())
    if action:
        # "license" matches every license.* action; "license.create" just that one.
        if "." in action:
            query = query.filter(models.AuditEvent.action == action)
        else:
            query = query.filter(models.AuditEvent.action.like(f"{action}.%"))
    if outcome:
        query = query.filter(models.AuditEvent.outcome == outcome)
    if target:
        query = query.filter(models.AuditEvent.target == target)
    if since:
        query = query.filter(models.AuditEvent.at >= since)
    if until:
        query = query.filter(models.AuditEvent.at < until)
    total = query.count()
    rows = (query.order_by(models.AuditEvent.at.desc(), models.AuditEvent.id.desc())
            .offset(offset).limit(limit).all())
    return rows, total
