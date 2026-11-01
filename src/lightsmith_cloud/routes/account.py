"""Hosted-mode business endpoints: identity, API keys, usage, plans, Stripe billing and admin."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import case, func, select

from ..auth import ORG_ID_RE, Principal, new_api_key
from ..billing import ACTIVE_SUBSCRIPTION, LIVE_SUBSCRIPTION, PAID_PLANS, PLANS, effective_plan, quota_status
from ..context import Context
from ..db import ApiKey, Job, Org, StripeEvent, UsageRecord, as_utc, month_start, utcnow
from ..deps import admin, anonymous, check_id, get_ctx, principal
from ..errors import ApiError, not_found
from ..schemas import CapOverride, CheckoutRequest, CreditLimit, KeyCreate, PlanChange, PlanUpdate, PortalRequest

router = APIRouter(prefix="/v1")


@router.get("/me")
def me(request: Request, p: Principal = Depends(principal)) -> dict[str, Any]:
    ctx = get_ctx(request)
    with ctx.db.session() as s:
        org = s.get(Org, p.org_id)
        return {"kind": p.kind, "user_id": p.user_id, "role": p.role, "api_key_id": p.api_key_id,
                "org": {"id": org.id, "plan": org.plan, "subscription_status": org.subscription_status},
                "plan": effective_plan(org).public() if ctx.settings.hosted else None,
                "usage": quota_status(s, org, ctx.settings)}


@router.get("/usage")
def usage(request: Request, p: Principal = Depends(principal)) -> dict[str, Any]:
    ctx = get_ctx(request)
    with ctx.db.session() as s:
        return quota_status(s, s.get(Org, p.org_id), ctx.settings)


def _plans(ctx: Context) -> list[dict[str, Any]]:
    return [{**p.public(), "purchasable": ctx.billing.purchasable(p.name)} for p in PLANS.values()]


@router.get("/plans", dependencies=[Depends(anonymous)])
def plans(request: Request) -> dict[str, Any]:
    return {"credit_unit": "1 credit = 1 GPU-second on the base GPU tier", "currency": "usd",
            "plans": _plans(get_ctx(request))}


# --- API keys -----------------------------------------------------------------


def _key_view(k: ApiKey) -> dict[str, Any]:
    return {"id": k.id, "name": k.name, "prefix": f"lsk_{k.prefix}_", "created_at": as_utc(k.created_at),
            "last_used_at": as_utc(k.last_used_at), "revoked_at": as_utc(k.revoked_at)}


def _require_hosted(request: Request) -> None:
    if not get_ctx(request).settings.hosted:
        raise ApiError(404, "NotFound", "not available in local mode")


def _require_login(p: Principal, what: str) -> None:
    if p.kind != "jwt":
        raise ApiError(403, "Forbidden", f"{what} with a platform login token, not an API key")


@router.post("/keys", status_code=201, dependencies=[Depends(_require_hosted)])
def create_key(body: KeyCreate, request: Request, p: Principal = Depends(principal)) -> dict[str, Any]:
    """Create an API key for the caller's org. The key is shown once."""
    _require_login(p, "API keys are created")
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


def _billing_view(ctx: Context, s, org: Org) -> dict[str, Any]:
    """Everything the platform's billing page shows, in one call."""
    b, status = ctx.billing, org.subscription_status
    subscribed = bool(org.stripe_subscription_id) and status in LIVE_SUBSCRIPTION
    return {
        "billing_enabled": b.enabled, "test_mode": True,
        "org": {"id": org.id, "plan": org.plan, "effective_plan": effective_plan(org).name,
                "subscription_status": status, "current_period_end": as_utc(org.current_period_end),
                "cancel_at_period_end": bool(org.cancel_at_period_end), "has_customer": bool(org.stripe_customer_id)},
        "payment_issue": status in ("past_due", "unpaid", "incomplete"),
        "actions": {
            "checkout": [] if subscribed else [n for n in PAID_PLANS if b.purchasable(n)],
            "change_plan": [n for n in PAID_PLANS if b.purchasable(n) and n != org.plan]
            if subscribed and status in ACTIVE_SUBSCRIPTION else [],
            "portal": b.enabled and bool(org.stripe_customer_id),
        },
        "usage": quota_status(s, org, ctx.settings),
        "plans": _plans(ctx),
    }


@router.get("/billing", dependencies=[Depends(_require_hosted)])
def billing(request: Request, p: Principal = Depends(principal)) -> dict[str, Any]:
    """Billing state of the caller's org: plan, subscription, usage against limits, and what it can do."""
    ctx = get_ctx(request)
    with ctx.db.session() as s:
        return _billing_view(ctx, s, s.get(Org, p.org_id))


@router.post("/billing/checkout", dependencies=[Depends(_require_hosted)])
def checkout(body: CheckoutRequest, request: Request, p: Principal = Depends(principal)) -> dict[str, str]:
    _require_login(p, "subscriptions are started")
    ctx = get_ctx(request)
    with ctx.db.session() as s:
        return {"url": ctx.billing.checkout_url(s, s.get(Org, p.org_id), body.plan, body.success_url, body.cancel_url)}


@router.post("/billing/portal", dependencies=[Depends(_require_hosted)])
def portal(body: PortalRequest, request: Request, p: Principal = Depends(principal)) -> dict[str, str]:
    _require_login(p, "the billing portal is opened")
    ctx = get_ctx(request)
    with ctx.db.session() as s:
        return {"url": ctx.billing.portal_url(s, s.get(Org, p.org_id), body.return_url)}


@router.post("/billing/plan", dependencies=[Depends(_require_hosted)])
def change_plan(body: PlanChange, request: Request, p: Principal = Depends(principal)) -> dict[str, Any]:
    """Switch the org's subscription between paid plans (prorated by Stripe). The plan changes when
    Stripe confirms through the webhook; poll GET /v1/billing."""
    _require_login(p, "plans are changed")
    ctx = get_ctx(request)
    with ctx.db.session() as s:
        return ctx.billing.change_plan(s.get(Org, p.org_id), body.plan)


@router.put("/billing/limits", dependencies=[Depends(_require_hosted)])
def set_credit_limit(body: CreditLimit, request: Request, p: Principal = Depends(principal)) -> dict[str, Any]:
    """Set the org's own monthly credit limit (a spending cap below the plan's hard limit)."""
    _require_login(p, "credit limits are set")
    ctx = get_ctx(request)
    with ctx.db.session() as s:
        org = s.get(Org, p.org_id)
        org.credit_limit = body.monthly_credit_limit
        s.flush()
        return quota_status(s, org, ctx.settings)


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
    unreported = UsageRecord.stripe_reported_at.is_(None) & (UsageRecord.billable_units > 0)
    with ctx.db.session() as s:
        q = (select(UsageRecord.org_id, Org.plan, func.count(UsageRecord.id),
                    func.sum(UsageRecord.runtime_s), func.sum(UsageRecord.gpu_seconds), func.sum(UsageRecord.credits),
                    func.sum(UsageRecord.overage_credits), func.sum(UsageRecord.billable_units),
                    func.sum(case((unreported, UsageRecord.overage_credits), else_=0.0)),
                    func.sum(case((unreported, UsageRecord.billable_units), else_=0)))
             .join(Org, Org.id == UsageRecord.org_id)
             .where(UsageRecord.created_at >= since, UsageRecord.created_at < until)
             .group_by(UsageRecord.org_id, Org.plan))
        if org_id:
            q = q.where(UsageRecord.org_id == org_id)
        rows = [{"org_id": r[0], "plan": r[1], "jobs": r[2], "runtime_s": round(r[3] or 0, 3),
                 "gpu_seconds": round(r[4] or 0, 3), "credits": round(r[5] or 0, 3),
                 "overage_credits": round(r[6] or 0, 3), "billable_units": int(r[7] or 0),
                 "credits_unreported_to_stripe": round(r[8] or 0, 3), "units_unreported_to_stripe": int(r[9] or 0)}
                for r in s.execute(q)]
        statuses = dict(s.execute(select(Job.status, func.count()).where(
            Job.created_at >= since, Job.created_at < until).group_by(Job.status)).all())
    keys = ("jobs", "gpu_seconds", "credits", "overage_credits", "billable_units", "units_unreported_to_stripe")
    return {"since": since, "until": until, "orgs": rows, "jobs_by_status": statuses,
            "totals": {k: round(sum(r[k] for r in rows), 3) for k in keys}}


@router.get("/admin/usage/daily", dependencies=[Depends(admin)])
def admin_usage_daily(request: Request, days: int = Query(30, ge=1, le=366)) -> dict[str, Any]:
    ctx = get_ctx(request)
    since = utcnow() - timedelta(days=days)
    with ctx.db.session() as s:
        rows = s.execute(select(UsageRecord.created_at, UsageRecord.gpu_seconds, UsageRecord.credits,
                                UsageRecord.billable_units).where(UsageRecord.created_at >= since)).all()
    by_day: dict[str, dict[str, float]] = {}
    for ts, gs, cr, units in rows:
        d = by_day.setdefault(as_utc(ts).date().isoformat(),
                              {"jobs": 0, "gpu_seconds": 0.0, "credits": 0.0, "billable_units": 0})
        d["jobs"] += 1
        d["gpu_seconds"] += gs or 0
        d["credits"] += cr or 0
        d["billable_units"] += units or 0
    return {"days": [{"date": k, **v} for k, v in sorted(by_day.items())]}


def _org_admin_view(ctx: Context, s, o: Org) -> dict[str, Any]:
    return {"id": o.id, "plan": o.plan, "effective_plan": effective_plan(o).name,
            "subscription_status": o.subscription_status, "stripe_customer_id": o.stripe_customer_id,
            "stripe_subscription_id": o.stripe_subscription_id, "current_period_end": as_utc(o.current_period_end),
            "cancel_at_period_end": bool(o.cancel_at_period_end), "credit_limit": o.credit_limit,
            "credit_cap_override": o.credit_cap_override, "created_at": as_utc(o.created_at),
            "usage": quota_status(s, o, ctx.settings)}


def _admin_org(s, org_id: str) -> Org:
    org = s.get(Org, org_id) if ORG_ID_RE.match(org_id) else None
    if org is None:
        raise not_found("org")
    return org


@router.get("/admin/orgs", dependencies=[Depends(admin)])
def admin_orgs(request: Request, limit: int = Query(200, ge=1, le=1000)) -> dict[str, Any]:
    ctx = get_ctx(request)
    with ctx.db.session() as s:
        orgs = s.execute(select(Org).order_by(Org.created_at.desc()).limit(limit)).scalars()
        return {"orgs": [_org_admin_view(ctx, s, o) for o in orgs]}


@router.get("/admin/orgs/{org_id}", dependencies=[Depends(admin)])
def admin_org(org_id: str, request: Request, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    """One org's billing state, its latest usage records (with their Stripe meter status) and the
    Stripe events applied to it."""
    ctx = get_ctx(request)
    with ctx.db.session() as s:
        org = _admin_org(s, org_id)
        recs = s.execute(select(UsageRecord).where(UsageRecord.org_id == org.id)
                         .order_by(UsageRecord.created_at.desc()).limit(limit)).scalars()
        events = s.execute(select(StripeEvent).where(StripeEvent.org_id == org.id)
                           .order_by(StripeEvent.received_at.desc()).limit(limit)).scalars()
        return {**_org_admin_view(ctx, s, org),
                "usage_records": [{"id": r.id, "job_id": r.job_id, "created_at": as_utc(r.created_at),
                                   "queue": r.queue, "runtime_s": r.runtime_s, "credits": r.credits,
                                   "overage_credits": r.overage_credits, "billable_units": r.billable_units,
                                   "stripe_reported_at": as_utc(r.stripe_reported_at), "stripe_error": r.stripe_error}
                                  for r in recs],
                "stripe_events": [{"id": e.id, "type": e.type, "outcome": e.outcome,
                                   "received_at": as_utc(e.received_at)} for e in events]}


@router.put("/admin/orgs/{org_id}/plan", dependencies=[Depends(admin)])
def admin_set_plan(org_id: str, body: PlanUpdate, request: Request) -> dict[str, Any]:
    if body.plan not in PLANS:
        raise ApiError(422, "ValidationError", f"unknown plan; use one of {sorted(PLANS)}")
    ctx = get_ctx(request)
    with ctx.db.session() as s:
        org = _admin_org(s, org_id)
        org.plan = body.plan
        return {"id": org.id, "plan": org.plan}


@router.put("/admin/orgs/{org_id}/limits", dependencies=[Depends(admin)])
def admin_set_limits(org_id: str, body: CapOverride, request: Request) -> dict[str, Any]:
    """Replace the plan's monthly hard limit for one org (null restores the plan's)."""
    ctx = get_ctx(request)
    with ctx.db.session() as s:
        org = _admin_org(s, org_id)
        org.credit_cap_override = body.credit_cap_override
        s.flush()
        return _org_admin_view(ctx, s, org)


@router.post("/admin/billing/sync", dependencies=[Depends(admin)])
def admin_billing_sync(request: Request, limit: int = Query(500, ge=1, le=5000)) -> dict[str, Any]:
    """Retry usage records whose Stripe meter event failed."""
    ctx = get_ctx(request)
    with ctx.db.session() as s:
        return ctx.billing.sync_usage(s, limit)
