"""Hosted-mode business endpoints: identity, API keys, usage, plans, Stripe billing and admin."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import case, func, select

from ..auth import ORG_ID_RE, Principal, new_api_key
from ..billing import PLANS, effective_plan
from ..db import ApiKey, Job, Org, UsageRecord, as_utc, credits_used, month_start, utcnow
from ..deps import admin, anonymous, check_id, get_ctx, principal
from ..errors import ApiError, not_found
from ..schemas import CheckoutRequest, KeyCreate, PlanUpdate, PortalRequest

router = APIRouter(prefix="/v1")


def _usage(s, org: Org) -> dict[str, Any]:
    plan = effective_plan(org)
    start = month_start()
    used = credits_used(s, org.id, start)
    return {"period_start": start, "credits_used": round(used, 3), "credits_included": plan.monthly_credits,
            "credits_remaining": max(0.0, round(plan.monthly_credits - used, 3)), "overage_allowed": plan.overage}


@router.get("/me")
def me(request: Request, p: Principal = Depends(principal)) -> dict[str, Any]:
    ctx = get_ctx(request)
    with ctx.db.session() as s:
        org = s.get(Org, p.org_id)
        return {"kind": p.kind, "user_id": p.user_id, "role": p.role, "api_key_id": p.api_key_id,
                "org": {"id": org.id, "plan": org.plan, "subscription_status": org.subscription_status},
                "plan": effective_plan(org).public() if ctx.settings.hosted else None,
                "usage": _usage(s, org)}


@router.get("/usage")
def usage(request: Request, p: Principal = Depends(principal)) -> dict[str, Any]:
    ctx = get_ctx(request)
    with ctx.db.session() as s:
        return _usage(s, s.get(Org, p.org_id))


@router.get("/plans", dependencies=[Depends(anonymous)])
def plans() -> dict[str, Any]:
    return {"credit_unit": "1 credit = 1 GPU-second on the base GPU tier",
            "plans": [p.public() for p in PLANS.values()]}


# --- API keys -----------------------------------------------------------------


def _key_view(k: ApiKey) -> dict[str, Any]:
    return {"id": k.id, "name": k.name, "prefix": f"lsk_{k.prefix}_", "created_at": as_utc(k.created_at),
            "last_used_at": as_utc(k.last_used_at), "revoked_at": as_utc(k.revoked_at)}


def _require_hosted(request: Request) -> None:
    if not get_ctx(request).settings.hosted:
        raise ApiError(404, "NotFound", "not available in local mode")


@router.post("/keys", status_code=201, dependencies=[Depends(_require_hosted)])
def create_key(body: KeyCreate, request: Request, p: Principal = Depends(principal)) -> dict[str, Any]:
    """Create an API key for the caller's org. The key is shown once."""
    if p.kind != "jwt":
        raise ApiError(403, "Forbidden", "API keys are created with a platform login token, not another key")
    ctx = get_ctx(request)
    with ctx.db.session() as s:
        active = s.execute(select(func.count()).select_from(ApiKey).where(
            ApiKey.org_id == p.org_id, ApiKey.revoked_at.is_(None))).scalar_one()
        if active >= 50:
            raise ApiError(409, "TooManyKeys", "an org can have at most 50 active keys")
        row, key = new_api_key(s, ctx.settings, p.org_id, p.user_id, body.name)
        return {**_key_view(row), "key": key}


@router.get("/keys", dependencies=[Depends(_require_hosted)])
def list_keys(request: Request, p: Principal = Depends(principal)) -> dict[str, Any]:
    ctx = get_ctx(request)
    with ctx.db.session() as s:
        rows = s.execute(select(ApiKey).where(ApiKey.org_id == p.org_id).order_by(ApiKey.created_at)).scalars()
        return {"keys": [_key_view(k) for k in rows]}


@router.delete("/keys/{key_id}", dependencies=[Depends(_require_hosted)])
def revoke_key(key_id: str, request: Request, p: Principal = Depends(principal)) -> dict[str, Any]:
    ctx = get_ctx(request)
    with ctx.db.session() as s:
        k = s.get(ApiKey, check_id(key_id))
        if k is None or k.org_id != p.org_id:
            raise not_found("key")
        k.revoked_at = k.revoked_at or utcnow()
        return _key_view(k)


# --- Stripe -------------------------------------------------------------------


@router.post("/billing/checkout", dependencies=[Depends(_require_hosted)])
def checkout(body: CheckoutRequest, request: Request, p: Principal = Depends(principal)) -> dict[str, str]:
    ctx = get_ctx(request)
    with ctx.db.session() as s:
        return {"url": ctx.billing.checkout_url(s, s.get(Org, p.org_id), body.plan, body.success_url, body.cancel_url)}


@router.post("/billing/portal", dependencies=[Depends(_require_hosted)])
def portal(body: PortalRequest, request: Request, p: Principal = Depends(principal)) -> dict[str, str]:
    ctx = get_ctx(request)
    with ctx.db.session() as s:
        return {"url": ctx.billing.portal_url(s, s.get(Org, p.org_id), body.return_url)}


@router.post("/billing/webhook", dependencies=[Depends(_require_hosted)])
async def stripe_webhook(request: Request) -> dict[str, str]:
    """Stripe webhook (signature-verified, test mode only). Configure it in the Stripe dashboard."""
    ctx = get_ctx(request)
    payload = await request.body()
    event = ctx.billing.parse_webhook(payload, request.headers.get("stripe-signature"))
    with ctx.db.session() as s:
        outcome = ctx.billing.apply_event(s, event)
    return {"received": event["id"], "outcome": outcome}


# --- admin --------------------------------------------------------------------


@router.get("/admin/usage", dependencies=[Depends(admin)])
def admin_usage(request: Request, since: datetime | None = None, until: datetime | None = None,
                org_id: str | None = None) -> dict[str, Any]:
    """Usage per org over a period (default: this calendar month, UTC)."""
    ctx = get_ctx(request)
    since = since or month_start()
    until = until or utcnow()
    with ctx.db.session() as s:
        q = (select(UsageRecord.org_id, Org.plan, func.count(UsageRecord.id),
                    func.sum(UsageRecord.runtime_s), func.sum(UsageRecord.gpu_seconds), func.sum(UsageRecord.credits),
                    func.sum(case((UsageRecord.stripe_reported_at.is_(None), UsageRecord.credits), else_=0.0)))
             .join(Org, Org.id == UsageRecord.org_id)
             .where(UsageRecord.created_at >= since, UsageRecord.created_at < until)
             .group_by(UsageRecord.org_id, Org.plan))
        if org_id:
            q = q.where(UsageRecord.org_id == org_id)
        rows = [{"org_id": r[0], "plan": r[1], "jobs": r[2], "runtime_s": round(r[3] or 0, 3),
                 "gpu_seconds": round(r[4] or 0, 3), "credits": round(r[5] or 0, 3),
                 "credits_unreported_to_stripe": round(r[6] or 0, 3)} for r in s.execute(q)]
        statuses = dict(s.execute(select(Job.status, func.count()).where(
            Job.created_at >= since, Job.created_at < until).group_by(Job.status)).all())
    return {"since": since, "until": until, "orgs": rows, "jobs_by_status": statuses,
            "totals": {k: round(sum(r[k] for r in rows), 3) for k in ("jobs", "gpu_seconds", "credits")}}


@router.get("/admin/usage/daily", dependencies=[Depends(admin)])
def admin_usage_daily(request: Request, days: int = Query(30, ge=1, le=366)) -> dict[str, Any]:
    ctx = get_ctx(request)
    since = utcnow() - timedelta(days=days)
    with ctx.db.session() as s:
        rows = s.execute(select(UsageRecord.created_at, UsageRecord.gpu_seconds, UsageRecord.credits)
                         .where(UsageRecord.created_at >= since)).all()
    by_day: dict[str, dict[str, float]] = {}
    for ts, gs, cr in rows:
        d = by_day.setdefault(as_utc(ts).date().isoformat(), {"jobs": 0, "gpu_seconds": 0.0, "credits": 0.0})
        d["jobs"] += 1
        d["gpu_seconds"] += gs or 0
        d["credits"] += cr or 0
    return {"days": [{"date": k, **v} for k, v in sorted(by_day.items())]}


@router.get("/admin/orgs", dependencies=[Depends(admin)])
def admin_orgs(request: Request, limit: int = Query(200, ge=1, le=1000)) -> dict[str, Any]:
    ctx = get_ctx(request)
    with ctx.db.session() as s:
        orgs = s.execute(select(Org).order_by(Org.created_at.desc()).limit(limit)).scalars()
        return {"orgs": [{"id": o.id, "plan": o.plan, "subscription_status": o.subscription_status,
                          "stripe_customer_id": o.stripe_customer_id, "created_at": as_utc(o.created_at),
                          "usage": _usage(s, o)} for o in orgs]}


@router.put("/admin/orgs/{org_id}/plan", dependencies=[Depends(admin)])
def admin_set_plan(org_id: str, body: PlanUpdate, request: Request) -> dict[str, Any]:
    if body.plan not in PLANS:
        raise ApiError(422, "ValidationError", f"unknown plan; use one of {sorted(PLANS)}")
    ctx = get_ctx(request)
    with ctx.db.session() as s:
        org = s.get(Org, org_id) if ORG_ID_RE.match(org_id) else None
        if org is None:
            raise not_found("org")
        org.plan = body.plan
        return {"id": org.id, "plan": org.plan}


@router.post("/admin/billing/sync", dependencies=[Depends(admin)])
def admin_billing_sync(request: Request, limit: int = Query(500, ge=1, le=5000)) -> dict[str, Any]:
    """Retry usage records that were not reported to Stripe."""
    ctx = get_ctx(request)
    done = failed = 0
    with ctx.db.session() as s:
        recs = s.execute(select(UsageRecord).where(UsageRecord.stripe_reported_at.is_(None), UsageRecord.credits > 0)
                         .order_by(UsageRecord.created_at).limit(limit)).scalars().all()
        for rec in recs:
            ctx.billing.report_usage(s, s.get(Org, rec.org_id), rec)
            done += rec.stripe_reported_at is not None
            failed += rec.stripe_error is not None
    return {"checked": len(recs), "reported": done, "failed": failed}
