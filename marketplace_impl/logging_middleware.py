from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import time
import uuid

import jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from .config import settings

logger = logging.getLogger("api")
logging.basicConfig(level=logging.INFO, format="%(message)s")

SENSITIVE_KEYS = {"password", "refresh_token", "access_token", "token"}


def _mask(value):
    if isinstance(value, dict):
        return {k: ("***" if k.lower() in SENSITIVE_KEYS else _mask(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v) for v in value]
    return value


def _user_id_from_header(request: Request):
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    try:
        payload = jwt.decode(
            auth.split(" ", 1)[1],
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
        if payload.get("type") != "access":
            return None
        return payload.get("sub")
    except Exception:
        return None


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        body_for_log = None

        if request.method in {"POST", "PUT", "DELETE"}:
            raw = await request.body()
            if raw:
                try:
                    body_for_log = _mask(json.loads(raw))
                except Exception:
                    body_for_log = "<non-json body>"

        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-Id"] = request_id
            return response
        finally:
            record = {
                "request_id": request_id,
                "method": request.method,
                "endpoint": request.url.path,
                "status_code": status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "user_id": _user_id_from_header(request),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if request.method in {"POST", "PUT", "DELETE"}:
                record["request_body"] = body_for_log
            logger.info(json.dumps(record, ensure_ascii=False, default=str))
