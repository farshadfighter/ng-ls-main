"""
Admin accounts (admin role only): list, create, change role / enable / disable, reset a
password, unlock. Accounts are never deleted - the audit trail refers to them. Rules:
app/accounts.py.
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy.orm import Session

from .. import accounts, audit, models
from ..core.security import AdminPrincipal, request_ip, require_admin
from ..database import get_db

router = APIRouter(prefix="/api/admin/users", tags=["admin-users"])


class AdminUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    full_name: Optional[str] = None
    email: Optional[str] = None
    role: str
    is_active: bool
    must_change_password: bool
    locked_until: Optional[datetime] = None
    last_login_at: Optional[datetime] = None
    last_login_ip: Optional[str] = None
    password_changed_at: Optional[datetime] = None
    created_at: datetime
    created_by: Optional[str] = None


class AdminUserCreate(BaseModel):
    username: str = Field(max_length=64)
    role: str = Field(max_length=16)
    password: str = Field(max_length=128)  # temporary: its owner must change it at first login
    full_name: Optional[str] = Field(default=None, max_length=255)
    email: Optional[EmailStr] = None


class AdminUserUpdate(BaseModel):
    role: Optional[str] = Field(default=None, max_length=16)
    is_active: Optional[bool] = None
    full_name: Optional[str] = Field(default=None, max_length=255)
    email: Optional[EmailStr] = None


class PasswordReset(BaseModel):
    password: str = Field(max_length=128)


def _user(db: Session, user_id: int) -> models.AdminUser:
    user = db.get(models.AdminUser, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return user


@router.get("", response_model=List[AdminUserOut])
def list_users(db: Session = Depends(get_db), admin: AdminPrincipal = Depends(require_admin)):
    return db.query(models.AdminUser).order_by(models.AdminUser.username).all()


@router.post("", response_model=AdminUserOut, status_code=201)
def create_user(body: AdminUserCreate, request: Request, db: Session = Depends(get_db),
                admin: AdminPrincipal = Depends(require_admin)):
    ip = request_ip(request)
    try:
        user = accounts.create_user(db, username=body.username, role=body.role, password=body.password,
                                    full_name=body.full_name, email=body.email, created_by=admin.username)
    except accounts.AccountError as exc:
        audit.record("user.create", audit.FAILURE, actor=admin.username, role=admin.role, ip=ip,
                     target=body.username[:64], detail={"reason": str(exc)})
        raise HTTPException(status_code=400, detail=str(exc))
    audit.record("user.create", audit.SUCCESS, actor=admin.username, role=admin.role, ip=ip,
                 target=user.username, detail={"role": user.role})
    return user


@router.patch("/{user_id}", response_model=AdminUserOut)
def update_user(user_id: int, body: AdminUserUpdate, request: Request, db: Session = Depends(get_db),
                admin: AdminPrincipal = Depends(require_admin)):
    user = _user(db, user_id)
    ip = request_ip(request)
    try:
        changes = accounts.update_user(db, user, actor_id=admin.user_id, role=body.role,
                                       is_active=body.is_active, full_name=body.full_name, email=body.email)
    except accounts.AccountError as exc:
        audit.record("user.update", audit.FAILURE, actor=admin.username, role=admin.role, ip=ip,
                     target=user.username, detail={"reason": str(exc)})
        raise HTTPException(status_code=409, detail=str(exc))
    if changes:
        audit.record("user.update", audit.SUCCESS, actor=admin.username, role=admin.role, ip=ip,
                     target=user.username, detail=changes)
    return user


@router.post("/{user_id}/reset-password", response_model=AdminUserOut)
def reset_password(user_id: int, body: PasswordReset, request: Request, db: Session = Depends(get_db),
                   admin: AdminPrincipal = Depends(require_admin)):
    """Give an account a temporary password (its owner must change it at next login); ends its
    sessions and lifts a lockout."""
    user = _user(db, user_id)
    ip = request_ip(request)
    try:
        accounts.reset_password(db, user, body.password)
    except accounts.AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    audit.record("user.password_reset", audit.SUCCESS, actor=admin.username, role=admin.role, ip=ip,
                 target=user.username)
    return user


@router.post("/{user_id}/unlock", response_model=AdminUserOut)
def unlock(user_id: int, request: Request, db: Session = Depends(get_db),
           admin: AdminPrincipal = Depends(require_admin)):
    user = _user(db, user_id)
    accounts.unlock(db, user)
    audit.record("user.unlock", audit.SUCCESS, actor=admin.username, role=admin.role,
                 ip=request_ip(request), target=user.username)
    return user
