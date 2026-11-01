"""FastAPI dependencies: context, authentication with rate limiting, admin checks."""

from __future__ import annotations

import re

from fastapi import Depends, Header, Request

from .auth import Principal, authenticate
from .billing import effective_plan
from .context import Context
from .db import Org
from .errors import ApiError, not_found

ID_RE = re.compile(r"^[0-9a-f]{32}$")


def get_ctx(request: Request) -> Context:
    return request.app.state.ctx


def principal(request: Request, authorization: str | None = Header(None),
              x_org_id: str | None = Header(None, max_length=200),
              x_api_key: str | None = Header(None, max_length=200)) -> Principal:
    ctx = get_ctx(request)
    with ctx.db.session() as s:
        p = authenticate(s, ctx.settings, authorization, x_org_id, x_api_key)
        if ctx.settings.hosted and p.role != "admin":
            limit = effective_plan(s.get(Org, p.org_id)).rate_limit_per_minute
        else:
            limit = ctx.settings.rate_limit_per_minute
    ctx.limiter.hit(p.rate_key, limit)
    return p


def anonymous(request: Request) -> None:
    """Per-IP limit for public endpoints in hosted mode."""
    ctx = get_ctx(request)
    if ctx.settings.hosted:
        ip = request.client.host if request.client else "unknown"
        ctx.limiter.hit(f"ip:{ip}", ctx.settings.anonymous_rate_limit_per_minute)


def admin(p: Principal = Depends(principal)) -> Principal:
    if not p.is_admin:
        raise ApiError(403, "Forbidden", "admin role required")
    return p


def check_id(job_id: str) -> str:
    if not ID_RE.match(job_id):
        raise not_found()
    return job_id
