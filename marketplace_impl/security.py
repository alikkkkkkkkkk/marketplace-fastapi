from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import uuid

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .errors import ApiException
from .models import RefreshToken, User

password_hasher = PasswordHasher()


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    role: str


_current_principal: ContextVar[Principal | None] = ContextVar("current_principal", default=None)


def clear_principal():
    return _current_principal.set(None)


def reset_principal(token):
    _current_principal.reset(token)


def get_current_principal() -> Principal:
    principal = _current_principal.get()
    if principal is None:
        raise ApiException(401, "TOKEN_INVALID", "Access token is missing or invalid")
    return principal


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _encode(payload: dict) -> str:
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(user: User) -> str:
    now = _now()
    exp = now + timedelta(minutes=settings.access_token_minutes)
    return _encode(
        {
            "sub": str(user.id),
            "role": user.role,
            "type": "access",
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
        }
    )


def create_refresh_token(user: User, db) -> str:
    now = _now()
    exp = now + timedelta(days=settings.refresh_token_days)
    jti = uuid.uuid4()
    db.add(RefreshToken(jti=jti, user_id=user.id, expires_at=exp, revoked=False))
    return _encode(
        {
            "sub": str(user.id),
            "role": user.role,
            "type": "refresh",
            "jti": str(jti),
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
        }
    )


def _decode(token: str, invalid_code: str) -> dict:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        code = "TOKEN_EXPIRED" if invalid_code == "TOKEN_INVALID" else invalid_code
        raise ApiException(401, code, "Token has expired") from exc
    except jwt.PyJWTError as exc:
        raise ApiException(401, invalid_code, "Token is invalid") from exc


def authenticate_access_token(token: str) -> Principal:
    payload = _decode(token, "TOKEN_INVALID")
    if payload.get("type") != "access" or not payload.get("sub") or not payload.get("role"):
        raise ApiException(401, "TOKEN_INVALID", "Access token is invalid")
    try:
        principal = Principal(uuid.UUID(payload["sub"]), str(payload["role"]))
    except (ValueError, TypeError) as exc:
        raise ApiException(401, "TOKEN_INVALID", "Access token is invalid") from exc
    _current_principal.set(principal)
    return principal


def rotate_refresh_token(token: str) -> tuple[User, str, str]:
    payload = _decode(token, "REFRESH_TOKEN_INVALID")
    if payload.get("type") != "refresh" or not payload.get("sub") or not payload.get("jti"):
        raise ApiException(401, "REFRESH_TOKEN_INVALID", "Refresh token is invalid")
    try:
        user_id = uuid.UUID(payload["sub"])
        jti = uuid.UUID(payload["jti"])
    except (ValueError, TypeError) as exc:
        raise ApiException(401, "REFRESH_TOKEN_INVALID", "Refresh token is invalid") from exc

    with SessionLocal() as db, db.begin():
        stored = db.execute(
            select(RefreshToken).where(RefreshToken.jti == jti).with_for_update()
        ).scalar_one_or_none()
        if stored is None or stored.revoked or stored.user_id != user_id or stored.expires_at <= _now():
            raise ApiException(401, "REFRESH_TOKEN_INVALID", "Refresh token is invalid")
        user = db.get(User, user_id)
        if user is None:
            raise ApiException(401, "REFRESH_TOKEN_INVALID", "Refresh token is invalid")
        stored.revoked = True
        access = create_access_token(user)
        refresh = create_refresh_token(user, db)
        db.flush()
        return user, access, refresh
