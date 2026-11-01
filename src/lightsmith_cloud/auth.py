"""Who is calling: API keys, platform JWTs, the admin token, or the local user.

Identity lives in the platform (apps/web). This service keeps no user
profiles: it stores the ids from the platform's JWT on jobs, keys and usage,
plus an ``orgs`` row per org id for plan, quota and billing state.

- **Platform JWT**: HS256 with ``PLATFORM_JWT_SECRET``. Claims: ``sub`` (user
  id, required), ``role`` (``admin`` unlocks admin endpoints), optional
  ``org_id`` (the acting org; default: the personal org ``user:<sub>``) and
  ``orgs`` (other org ids the user may select with ``X-Org-Id``). ``aud`` is
  checked against ``LIGHTSMITH_JWT_AUDIENCE`` (default ``compute``).
- **API keys** look like ``lsk_<prefix>_<secret>``; only an HMAC-SHA256 of the
  key (keyed with ``LIGHTSMITH_API_KEY_PEPPER``) is stored. Keys act for one
  org and never carry the admin role.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .db import ApiKey, Org, utcnow
from .errors import ApiError

LOCAL_ORG = "local"  # the single org (and user) of local mode
SYSTEM_ORG = "system"  # jobs submitted with the admin token
ORG_ID_RE = re.compile(r"^[A-Za-z0-9:_.@\-]{1,200}$")


@dataclass(frozen=True)
class Principal:
    kind: str  # local | api_key | jwt | admin_token
    org_id: str
    user_id: str | None = None
    api_key_id: str | None = None
    role: str = "user"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin" or self.kind == "local"

    @property
    def rate_key(self) -> str:
        return self.api_key_id or self.user_id or self.org_id


def _unauthorized(msg: str) -> ApiError:
    return ApiError(401, "Unauthorized", msg, headers={"WWW-Authenticate": "Bearer"})


def ensure_org(s: Session, org_id: str, plan: str = "free") -> Org:
    """The org's billing row, created on first sight (ids come from the platform)."""
    if not ORG_ID_RE.match(org_id):
        raise ApiError(401, "Unauthorized", "invalid org id in token")
    org = s.get(Org, org_id)
    if org is None:
        org = Org(id=org_id, plan=plan)
        s.add(org)
        s.flush()
    return org


def ensure_local_tenant(s: Session) -> None:
    ensure_org(s, LOCAL_ORG, plan="local")
    ensure_org(s, SYSTEM_ORG)


# --- API keys ---------------------------------------------------------------

def hash_key(key: str, pepper: str) -> str:
    return hmac.new(pepper.encode(), key.encode(), hashlib.sha256).hexdigest()


def new_api_key(s: Session, settings: Settings, org_id: str, user_id: str | None, name: str) -> tuple[ApiKey, str]:
    prefix = secrets.token_hex(6)
    key = f"lsk_{prefix}_{secrets.token_urlsafe(32)}"
    row = ApiKey(org_id=org_id, user_id=user_id, name=name[:200], prefix=prefix,
                 hash=hash_key(key, settings.api_key_pepper.get_secret_value()))
    s.add(row)
    s.flush()
    return row, key


def _from_api_key(s: Session, settings: Settings, token: str) -> Principal:
    parts = token.split("_", 2)
    if len(parts) != 3 or not parts[1]:
        raise _unauthorized("malformed API key")
    row = s.execute(select(ApiKey).where(ApiKey.prefix == parts[1])).scalar_one_or_none()
    expected = row.hash if row else "0" * 64
    ok = hmac.compare_digest(expected, hash_key(token, settings.api_key_pepper.get_secret_value()))
    if row is None or not ok or row.revoked_at is not None:
        raise _unauthorized("invalid or revoked API key")
    row.last_used_at = utcnow()
    # Keys never carry the admin role, even when an admin created them (least privilege).
    return Principal("api_key", row.org_id, row.user_id, row.id, "user")


# --- platform JWT -----------------------------------------------------------

def _from_jwt(s: Session, settings: Settings, token: str, org_header: str | None) -> Principal:
    secret = settings.platform_jwt_secret.get_secret_value() if settings.platform_jwt_secret else ""
    if not secret:
        raise _unauthorized("platform tokens are not accepted (PLATFORM_JWT_SECRET is not set)")
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"], audience=settings.jwt_audience or None,
                            issuer=settings.jwt_issuer or None,
                            options={"require": ["sub"], "verify_aud": bool(settings.jwt_audience)})
    except jwt.PyJWTError as e:
        raise _unauthorized(f"invalid token: {e}") from None
    sub = str(claims["sub"])[:200]
    role = "admin" if claims.get("role") == "admin" else "user"
    org_id = str(claims.get("org_id") or f"user:{sub}")[:200]
    if org_header and org_header != org_id:
        allowed = claims.get("orgs") if isinstance(claims.get("orgs"), list) else []
        if org_header not in [str(o) for o in allowed] and role != "admin":
            raise ApiError(403, "Forbidden", "the token does not grant access to that org")
        org_id = org_header
    ensure_org(s, org_id)
    return Principal("jwt", org_id, sub, None, role)


def authenticate(s: Session, settings: Settings, authorization: str | None, org_header: str | None = None,
                 api_key_header: str | None = None) -> Principal:
    if not settings.hosted:
        return Principal("local", LOCAL_ORG, LOCAL_ORG, None, "admin")
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif api_key_header:
        token = api_key_header.strip()
    if not token:
        raise _unauthorized("missing Authorization: Bearer <api key or platform token> (or X-API-Key)")
    if token.startswith("lsk_"):
        return _from_api_key(s, settings, token)
    admin = settings.admin_token.get_secret_value() if settings.admin_token else ""
    if admin and hmac.compare_digest(token.encode(), admin.encode()):
        return Principal("admin_token", SYSTEM_ORG, None, None, "admin")
    if token.count(".") == 2:
        return _from_jwt(s, settings, token, org_header)
    raise _unauthorized("unrecognised credential")
