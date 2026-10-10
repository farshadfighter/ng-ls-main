"""
The audit trail, for auditors and admins (app/audit.py): search, and export as CSV. Read-only;
there is no way to change or delete an entry.
"""
import csv
import io
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from .. import audit
from ..core.security import AdminPrincipal, request_ip, require_auditor
from ..database import get_db

router = APIRouter(prefix="/api/admin/audit", tags=["admin-audit"])

EXPORT_LIMIT = 50_000


class AuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    at: datetime
    actor: str
    actor_role: Optional[str] = None
    ip: Optional[str] = None
    action: str
    target: Optional[str] = None
    outcome: str
    detail: Optional[Dict[str, Any]] = None


class AuditPage(BaseModel):
    total: int
    items: List[AuditEventOut]


def _filters(actor: Optional[str] = Query(default=None, max_length=64),
             action: Optional[str] = Query(default=None, max_length=64),
             outcome: Optional[str] = Query(default=None, pattern="^(success|failure)$"),
             target: Optional[str] = Query(default=None, max_length=255),
             since: Optional[datetime] = None, until: Optional[datetime] = None) -> dict:
    return dict(actor=actor, action=action, outcome=outcome, target=target, since=since, until=until)


@router.get("", response_model=AuditPage)
def search(filters: dict = Depends(_filters), limit: int = Query(default=100, ge=1, le=500),
           offset: int = Query(default=0, ge=0), db: Session = Depends(get_db),
           principal: AdminPrincipal = Depends(require_auditor)):
    rows, total = audit.search(db, limit=limit, offset=offset, **filters)
    return AuditPage(total=total, items=rows)


def _csv_cell(value) -> str:
    """A cell that a spreadsheet will not run as a formula (CSV injection)."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@router.get("/export.csv")
def export_csv(request: Request, filters: dict = Depends(_filters), db: Session = Depends(get_db),
               principal: AdminPrincipal = Depends(require_auditor)):
    """The matching events (newest first, at most 50,000) as CSV. The export itself is audited."""
    rows, total = audit.search(db, limit=EXPORT_LIMIT, offset=0, **filters)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "at", "actor", "role", "ip", "action", "target", "outcome", "detail"])
    for r in rows:
        writer.writerow([_csv_cell(v) for v in (r.id, r.at.isoformat(), r.actor, r.actor_role, r.ip,
                                                  r.action, r.target, r.outcome, r.detail)])
    audit.record("audit.export", audit.SUCCESS, actor=principal.username, role=principal.role,
                 ip=request_ip(request), detail={"rows": len(rows), "total": total,
                                                 **{k: str(v) for k, v in filters.items() if v}})
    return StreamingResponse(iter([buffer.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="audit.csv"'})
