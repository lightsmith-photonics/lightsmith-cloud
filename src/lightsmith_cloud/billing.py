"""Plans, quotas, metering and Stripe (test mode only).

Unit of account: **1 credit = 1 GPU-second on the base GPU tier**. Larger
tiers have a credit multiplier, and CPU runs a fraction (``LIGHTSMITH_CPU_CREDIT_MULTIPLIER``).
Usage is metered per job from the solver subprocess's wall time.

Quotas are per calendar month (UTC). A plan includes ``monthly_credits``; paid
plans may go beyond them (overage) up to a hard limit, which the org can lower
(``credit_limit``) and an operator can replace (``credit_cap_override``).
Admission holds the estimates of the org's queued and running jobs against the
quota, so a burst of submissions cannot overshoot it.

Stripe: one customer per org; a subscription with the plan's flat monthly price
plus the plan's metered overage price, both on one Billing Meter
(``LIGHTSMITH_STRIPE_METER_EVENT_NAME``). This service decides what is overage:
only credits beyond the included ones are sent to the meter, as whole units
(the floor of the month's running total, so per-job rounding never adds up).
Each usage record sends at most one meter event, whose identifier is the record
id, so retries never double-bill.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import GpuTier, Settings
from .db import Org, StripeEvent, UsageRecord, credits_reserved, credits_used, month_start, utcnow
from .errors import ApiError
from .lib import osc

GiB = 2**30


@dataclass(frozen=True)
class Plan:
    name: str
    monthly_credits: int  # included credits per calendar month (UTC)
    overage: bool  # may exceed included credits (billed through the metered price)
    max_concurrent_jobs: int
    max_runtime_s: int
    max_memory_bytes: int  # largest estimate() memory_bytes accepted
    host_memory_bytes: int  # host RSS cap of the solver process
    rate_limit_per_minute: int
    max_spec_bytes: int
    hard_limit_credits: int  # most credits per month, overage included (protects against runaway bills)
    # For display. Stripe holds the prices actually charged; `lightsmith-server stripe-setup` creates
    # them from these numbers.
    monthly_price_usd: float = 0.0
    overage_usd_per_credit: float = 0.0

    def public(self) -> dict[str, Any]:
        return asdict(self)


PLANS: dict[str, Plan] = {
    "free": Plan("free", monthly_credits=1_800, overage=False, max_concurrent_jobs=1, max_runtime_s=300,
                 max_memory_bytes=16 * GiB, host_memory_bytes=8 * GiB, rate_limit_per_minute=60,
                 max_spec_bytes=256_000, hard_limit_credits=1_800),
    "pro": Plan("pro", monthly_credits=36_000, overage=True, max_concurrent_jobs=4, max_runtime_s=3_600,
                max_memory_bytes=72 * GiB, host_memory_bytes=32 * GiB, rate_limit_per_minute=300,
                max_spec_bytes=2_000_000, hard_limit_credits=360_000,
                monthly_price_usd=29.0, overage_usd_per_credit=0.0005),
    "team": Plan("team", monthly_credits=180_000, overage=True, max_concurrent_jobs=16, max_runtime_s=4 * 3_600,
                 max_memory_bytes=72 * GiB, host_memory_bytes=64 * GiB, rate_limit_per_minute=1_200,
                 max_spec_bytes=2_000_000, hard_limit_credits=1_800_000,
                 monthly_price_usd=129.0, overage_usd_per_credit=0.0004),
}
PAID_PLANS = ("pro", "team")

ACTIVE_SUBSCRIPTION = ("active", "trialing")
# Statuses of a subscription that still exists: a new Checkout would create a second one.
LIVE_SUBSCRIPTION = ("active", "trialing", "past_due", "unpaid", "paused")


def effective_plan(org: Org) -> Plan:
    """Paid plans apply while the Stripe subscription is active (or when an admin set the plan
    without a subscription, i.e. no subscription status). A failed payment (past_due) drops the
    org to the free plan's limits until an invoice is paid."""
    plan = PLANS.get(org.plan, PLANS["free"])
    if org.subscription_status is not None and org.subscription_status not in ACTIVE_SUBSCRIPTION:
        return PLANS["free"]
    return plan


# --- metering ---------------------------------------------------------------

def pick_tier(settings: Settings, memory_bytes: int, safety: float = 1.15) -> GpuTier | None:
    """Smallest GPU tier whose memory fits the estimate with some headroom."""
    for t in settings.tiers:
        if memory_bytes * safety <= t.memory_bytes:
            return t
    return None


def credits_for(seconds: float, *, gpu: bool, tier: GpuTier | None, settings: Settings) -> float:
    mult = (tier.credit_multiplier if tier else 1.0) if gpu else settings.cpu_credit_multiplier
    return round(max(seconds, 0.0) * mult, 3)


# Measured 2026-11-01 on an RTX 5070 (shared, loaded machine), lightsmith-dda single precision,
# spheres at 24-72 dipoles per diameter: 1.7e-5 to 3.8e-5 s per lattice cell per solve, nearly
# independent of iteration count (per-wavelength set-up dominates), up to 2e-4 for tiny metal grids.
_DDA_S_PER_CELL_SOLVE = 3.0e-5
# Per-job solver process start-up (imports, device init), measured 2026-11-01 in the CUDA worker
# container on the dev box: trivial Mie jobs took 14.6 s cold and 3.4 s with a warm page cache.
# A warm process pool would cut this.
_STARTUP_S = 6.0
# Placeholder for one FDFD solve inside inverse design; replace with a measurement per backend.
_INVERSE_S_PER_SOLVE = 1.0


def _runtime(est: dict[str, Any], n_wavelengths: int) -> tuple[float, str]:
    """(seconds, basis). Prefers a runtime the backend reports itself; fields vary by backend."""
    for k in ("estimated_runtime_s", "runtime_s_estimate"):
        if isinstance(est.get(k), (int, float)):
            return _STARTUP_S + float(est[k]), "backend"
    if est.get("method") == "dda" and est.get("grid"):
        cells = math.prod(est["grid"])
        return _STARTUP_S + _DDA_S_PER_CELL_SOLVE * cells * max(1, int(est.get("solves", n_wavelengths))), "calibrated"
    if est.get("max_iter"):  # inverse design: forward + adjoint solves per source per iteration
        per_iter = 2.0 * max(1, int(est.get("sources_per_iteration") or 1))
        return _STARTUP_S + _INVERSE_S_PER_SOLVE * per_iter * int(est["max_iter"]), "rough"
    return _STARTUP_S + 0.002 * n_wavelengths, "rough"


def estimate_seconds(est: dict[str, Any], n_wavelengths: int) -> float:
    """Rough run time from estimate() output. Order-of-magnitude only."""
    return _runtime(est, n_wavelengths)[0]


def _uses_gpu(est: dict[str, Any]) -> bool:
    try:
        return bool(osc.get_backend(est["backend"]).gpu)
    except Exception:
        return False


def credits_estimate(est: dict[str, Any], n_wavelengths: int, tier: GpuTier | None, settings: Settings) -> dict:
    secs, basis = _runtime(est, n_wavelengths)
    return {"credits_estimate": math.ceil(credits_for(secs, gpu=_uses_gpu(est), tier=tier, settings=settings)),
            "seconds_estimate": round(secs, 1), "runtime_basis": basis,
            "queue": tier.queue if tier else None,
            "credit_basis": "1 credit = 1 GPU-second on the base tier; rough estimate"}


# --- quotas -----------------------------------------------------------------

def hard_limit(org: Org, plan: Plan) -> int:
    """Most credits the org may use this month: the plan's hard limit (or the operator override),
    lowered by the org's own spending limit."""
    cap = org.credit_cap_override if org.credit_cap_override is not None else plan.hard_limit_credits
    if org.credit_limit is not None:
        cap = min(cap, org.credit_limit)
    return int(cap)


def next_month(start: datetime) -> datetime:
    return start.replace(year=start.year + start.month // 12, month=start.month % 12 + 1)


def quota_status(s: Session, org: Org, settings: Settings) -> dict[str, Any]:
    """The org's quota this calendar month (UTC). `state` is one of: ok, soft_limit (most of the
    included credits used), overage (paid plan beyond its included credits, billed per credit) or
    blocked (hard limit reached: new jobs are refused)."""
    plan = effective_plan(org)
    start = month_start()
    used = credits_used(s, org.id, start)
    reserved = credits_reserved(s, org.id)
    cap = hard_limit(org, plan)
    included = plan.monthly_credits
    if used >= cap:
        state = "blocked"
    elif plan.overage and used > included:
        state = "overage"
    elif used >= settings.quota_soft_limit_fraction * included:
        state = "soft_limit"
    else:
        state = "ok"
    return {"period_start": start, "period_end": next_month(start), "plan": plan.name,
            "credits_used": round(used, 3), "credits_reserved": round(reserved, 3),
            "credits_included": included, "credits_remaining": max(0.0, round(included - used, 3)),
            "overage_allowed": plan.overage,
            "overage_credits": round(max(0.0, used - included), 3) if plan.overage else 0.0,
            "overage_usd_per_credit": plan.overage_usd_per_credit,
            "soft_limit": round(settings.quota_soft_limit_fraction * included, 3),
            "hard_limit": cap, "credit_limit": org.credit_limit, "state": state}


def admit(s: Session, org: Org, plan: Plan, credits_needed: float, settings: Settings) -> str | None:
    """Admission check for a new job. Refuses it (402) if this month's usage, plus the estimates of
    the org's queued and running jobs, plus this job would pass the hard limit. Returns a warning
    when the job takes the org past its soft limit or into overage, else None.

    A running job is never stopped by the quota; how far it can overshoot its estimate is bounded by
    the plan's runtime cap."""
    used = credits_used(s, org.id, month_start())
    reserved = credits_reserved(s, org.id)
    cap = hard_limit(org, plan)
    after = used + reserved + credits_needed
    if after > cap:
        if org.credit_limit is not None and cap == org.credit_limit:
            hint = "raise the org's monthly credit limit or wait for the next month"
        elif not plan.overage:
            hint = "upgrade the plan or wait for the next month"
        else:
            hint = "this is the plan's monthly hard limit; contact support to raise it"
        raise ApiError(402, "QuotaExceeded",
                       f"this job needs about {credits_needed:g} credits; {used:g} are used this month and "
                       f"{reserved:g} are held by queued or running jobs, against a limit of {cap} on the "
                       f"{plan.name} plan; {hint}", plan=plan.name, hard_limit=cap,
                       credits_used=round(used, 3), credits_reserved=round(reserved, 3))
    included = plan.monthly_credits
    if plan.overage and after > included:
        return (f"this job takes the org past the {included} credits included in the {plan.name} plan; "
                f"credits beyond them are billed at ${plan.overage_usd_per_credit:g} each")
    if after >= settings.quota_soft_limit_fraction * included:
        return f"about {after:g} of the {included} monthly credits on the {plan.name} plan will be used"
    return None


def overage_split(used_before: float, credits: float, plan: Plan) -> tuple[float, int]:
    """(overage credits, whole units to bill) of a usage record of `credits` when `used_before`
    credits were already used this month. Units are the increase of floor(running overage)."""
    if not plan.overage:
        return 0.0, 0
    before = max(0.0, used_before - plan.monthly_credits)
    after = max(0.0, used_before + credits - plan.monthly_credits)
    return round(after - before, 3), math.floor(after + 1e-9) - math.floor(before + 1e-9)


def record_usage(s: Session, org_id: str, **fields: Any) -> UsageRecord:
    """Write a job's usage record with its overage split. Locks the org row so that concurrent
    records of one org see each other's totals (Postgres; SQLite serialises writers anyway)."""
    org = s.execute(select(Org).where(Org.id == org_id).with_for_update()).scalar_one()
    now = utcnow()
    used_before = credits_used(s, org_id, month_start(now))
    rec = UsageRecord(org_id=org_id, created_at=now, **fields)
    rec.overage_credits, rec.billable_units = overage_split(used_before, rec.credits, effective_plan(org))
    s.add(rec)
    s.flush()
    return rec


# --- Stripe -----------------------------------------------------------------

def _epoch(ts: Any) -> datetime | None:
    return datetime.fromtimestamp(int(ts), timezone.utc) if isinstance(ts, (int, float)) and ts else None


def _id(x: Any) -> str | None:
    """Stripe fields hold an id, or the expanded object."""
    return x.get("id") if isinstance(x, dict) else x


def invoice_subscription(inv: dict) -> str | None:
    """The subscription of an invoice: `subscription` before API version 2025-03-31, else
    `parent.subscription_details.subscription`."""
    sub = _id(inv.get("subscription"))
    if not sub:
        details = (inv.get("parent") or {}).get("subscription_details") or {}
        sub = _id(details.get("subscription"))
    return sub


class StripeBilling:
    """Thin wrapper over the Stripe SDK. Disabled when no key is configured."""

    def __init__(self, settings: Settings, client=None):
        self.settings = settings
        key = settings.stripe_secret_key.get_secret_value() if settings.stripe_secret_key else ""
        self.enabled = bool(key) or client is not None
        self._client = client
        self._key = key

    @property
    def client(self):
        if self._client is None:
            import stripe

            self._client = stripe.StripeClient(self._key)
        return self._client

    def _require(self) -> None:
        if not self.enabled:
            raise ApiError(503, "BillingDisabled", "billing is not configured on this server")

    @staticmethod
    def _call(fn, *args, **kw):
        """A Stripe API call; Stripe errors become 502 BillingError with Stripe's message."""
        try:
            return fn(*args, **kw)
        except Exception as e:
            if type(e).__module__.startswith("stripe"):
                msg = getattr(e, "user_message", None) or str(e)
                raise ApiError(502, "BillingError", f"Stripe: {msg}"[:500]) from None
            raise

    def price_for(self, plan: str) -> str | None:
        return {"pro": self.settings.stripe_price_pro, "team": self.settings.stripe_price_team}.get(plan)

    def overage_price_for(self, plan: str) -> str | None:
        own = {"pro": self.settings.stripe_price_overage_pro, "team": self.settings.stripe_price_overage_team}
        return own.get(plan) or (self.settings.stripe_price_metered if plan in own else None)

    def plan_for_price(self, price_id: str | None) -> str | None:
        return {v: k for k, v in (("pro", self.settings.stripe_price_pro),
                                  ("team", self.settings.stripe_price_team)) if v}.get(price_id or "")

    def purchasable(self, plan: str) -> bool:
        return self.enabled and bool(self.price_for(plan))

    def check_redirect(self, url: str) -> str:
        base = self.settings.public_web_url.rstrip("/")
        if not (url == base or url.startswith(base + "/")):
            raise ApiError(422, "ValidationError", f"redirect URLs must start with {base}")
        return url

    def ensure_customer(self, s: Session, org: Org) -> str:
        self._require()
        if not org.stripe_customer_id:
            # Only the org id: names and emails belong to the platform, which can update the customer.
            c = self._call(self.client.v1.customers.create, params={"metadata": {"org_id": org.id}},
                           options={"idempotency_key": f"customer-{org.id}"})
            org.stripe_customer_id = c.id
            s.flush()
        return org.stripe_customer_id

    def checkout_url(self, s: Session, org: Org, plan: str, success_url: str, cancel_url: str) -> str:
        self._require()
        price = self.price_for(plan)
        if not price:
            raise ApiError(422, "ValidationError", f"plan {plan!r} has no Stripe price configured")
        if org.stripe_subscription_id and org.subscription_status in LIVE_SUBSCRIPTION:
            raise ApiError(409, "AlreadySubscribed",
                           f"this org already has a {org.subscription_status} subscription; change the plan with "
                           "POST /v1/billing/plan, or manage it in the billing portal", plan=org.plan)
        items: list[dict] = [{"price": price, "quantity": 1}]
        if self.overage_price_for(plan):
            items.append({"price": self.overage_price_for(plan)})  # metered: no quantity
        meta = {"org_id": org.id, "plan": plan}
        sess = self._call(self.client.v1.checkout.sessions.create, params={
            "mode": "subscription", "customer": self.ensure_customer(s, org), "client_reference_id": org.id,
            "line_items": items, "success_url": self.check_redirect(success_url),
            "cancel_url": self.check_redirect(cancel_url), "metadata": meta,
            "subscription_data": {"metadata": meta},
        })
        return sess.url

    def portal_url(self, s: Session, org: Org, return_url: str) -> str:
        self._require()
        params = {"customer": self.ensure_customer(s, org), "return_url": self.check_redirect(return_url)}
        if self.settings.stripe_portal_configuration:
            params["configuration"] = self.settings.stripe_portal_configuration
        return self._call(self.client.v1.billing_portal.sessions.create, params=params).url

    def change_plan(self, org: Org, plan: str) -> dict[str, Any]:
        """Switch an active subscription between paid plans, prorated. The org's plan changes when
        Stripe's `customer.subscription.updated` webhook arrives."""
        self._require()
        if not self.price_for(plan):
            raise ApiError(422, "ValidationError", f"plan {plan!r} has no Stripe price configured")
        if not org.stripe_subscription_id or org.subscription_status not in ACTIVE_SUBSCRIPTION:
            raise ApiError(409, "NoActiveSubscription",
                           "the org has no active subscription; start one with POST /v1/billing/checkout")
        sub = self._call(self.client.v1.subscriptions.retrieve, org.stripe_subscription_id).to_dict()
        flat = {p for p in (self.price_for(n) for n in PAID_PLANS) if p}
        metered = {p for p in (self.overage_price_for(n) for n in PAID_PLANS) if p}
        items, current = [], None
        for it in (sub.get("items") or {}).get("data") or []:
            price = _id(it.get("price"))
            if price in flat:
                current = self.plan_for_price(price)
                items.append({"id": it["id"], "price": self.price_for(plan), "quantity": 1})
            elif price in metered and price != self.overage_price_for(plan):
                items.append({"id": it["id"], "price": self.overage_price_for(plan)})
        if current is None:
            raise ApiError(409, "BillingError", "the subscription has no Lightsmith plan price; manage it in the portal")
        if current == plan:
            return {"status": "unchanged", "plan": plan}
        self._call(self.client.v1.subscriptions.update, org.stripe_subscription_id, params={
            "items": items, "proration_behavior": "create_prorations",
            "metadata": {"org_id": org.id, "plan": plan}})
        return {"status": "pending", "plan": plan, "previous_plan": current}

    def report_usage(self, s: Session, org: Org, rec: UsageRecord) -> None:
        """Send one meter event with a usage record's billable overage units (idempotent by record
        id). Errors are stored, not raised. Stripe accepts events up to 35 days old."""
        if not self.enabled or not org.stripe_customer_id or rec.stripe_reported_at or rec.billable_units <= 0:
            return
        try:
            self.client.v1.billing.meter_events.create(params={
                "event_name": self.settings.stripe_meter_event_name,
                "identifier": f"usage_{rec.id}",
                "payload": {"stripe_customer_id": org.stripe_customer_id, "value": str(rec.billable_units)},
                "timestamp": int(rec.created_at.timestamp()),
            })
            rec.stripe_reported_at, rec.stripe_error = utcnow(), None
        except Exception as e:  # network or API error: keep for the next sync
            rec.stripe_error = f"{type(e).__name__}: {e}"[:500]

    def sync_usage(self, s: Session, limit: int = 500) -> dict[str, int]:
        """Report usage records whose meter event has not gone through yet."""
        recs = s.execute(select(UsageRecord).where(UsageRecord.stripe_reported_at.is_(None),
                                                   UsageRecord.billable_units > 0)
                         .order_by(UsageRecord.created_at).limit(limit)).scalars().all()
        done = failed = 0
        for rec in recs:
            self.report_usage(s, s.get(Org, rec.org_id), rec)
            done += rec.stripe_reported_at is not None
            failed += rec.stripe_error is not None
        return {"checked": len(recs), "reported": done, "failed": failed}

    # webhooks

    def parse_webhook(self, payload: bytes, sig_header: str | None):
        secret = self.settings.stripe_webhook_secret
        if not secret or not secret.get_secret_value():
            raise ApiError(503, "BillingDisabled", "STRIPE_WEBHOOK_SECRET is not configured")
        import stripe

        try:
            stripe.Webhook.construct_event(payload, sig_header or "", secret.get_secret_value())
        except (ValueError, stripe.SignatureVerificationError) as e:
            raise ApiError(400, "InvalidSignature", f"webhook signature verification failed: {e}") from None
        event = json.loads(payload)  # verified; plain dicts are easier to handle than StripeObjects
        if event.get("livemode"):
            raise ApiError(400, "LiveModeRefused", "this service accepts test-mode Stripe events only")
        return event

    def apply_event(self, s: Session, event) -> str:
        """Update orgs from a verified event, once per event id. Returns 'applied', 'duplicate',
        'stale' (older than state already applied: Stripe does not guarantee order) or 'ignored'."""
        if s.get(StripeEvent, event["id"]) is not None:
            return "duplicate"
        row = StripeEvent(id=event["id"], type=event["type"])
        s.add(row)
        try:
            s.flush()
        except IntegrityError:  # the same event delivered twice at once
            s.rollback()
            return "duplicate"
        org, outcome = self._apply(s, event)
        row.org_id, row.outcome = (org.id if org is not None else None), outcome
        return outcome

    def _apply(self, s: Session, event) -> tuple[Org | None, str]:
        obj = event["data"]["object"]
        typ = event["type"]
        created = int(event.get("created") or 0)
        if typ == "checkout.session.completed":
            if obj.get("mode") not in (None, "subscription"):
                return None, "ignored"
            org = s.get(Org, obj.get("client_reference_id") or "") or self._org_for(s, obj)
            if org is None:
                return None, "ignored"
            org.stripe_customer_id = _id(obj.get("customer")) or org.stripe_customer_id
            org.stripe_subscription_id = _id(obj.get("subscription")) or org.stripe_subscription_id
            return org, "applied"
        if typ.startswith("customer.subscription."):
            org = self._org_for(s, obj)
            if org is None:
                return None, "ignored"
            if org.billing_event_at and created < org.billing_event_at:
                return org, "stale"
            deleted = typ.endswith(".deleted")
            status = "canceled" if deleted else obj.get("status")
            if (org.stripe_subscription_id and obj.get("id") != org.stripe_subscription_id
                    and org.subscription_status in LIVE_SUBSCRIPTION and status not in ACTIVE_SUBSCRIPTION):
                return org, "ignored"  # an older subscription ending while a newer one is live
            items = (obj.get("items") or {}).get("data") or []
            plan = (obj.get("metadata") or {}).get("plan")
            for item in items:
                plan = self.plan_for_price(_id(item.get("price"))) or plan
            org.stripe_customer_id = org.stripe_customer_id or _id(obj.get("customer"))  # invoices find the org by it
            org.stripe_subscription_id = obj.get("id")
            org.subscription_status = status
            org.billing_event_at = created
            if deleted or status in ("canceled", "incomplete_expired"):
                org.plan, org.current_period_end, org.cancel_at_period_end = "free", None, False
                return org, "applied"
            if plan in PLANS:
                org.plan = plan
            # current_period_end moved from the subscription to its items in API version 2025-03-31.
            ends = [e for e in [obj.get("current_period_end"), *(i.get("current_period_end") for i in items)] if e]
            org.current_period_end = _epoch(max(ends)) if ends else org.current_period_end
            org.cancel_at_period_end = bool(obj.get("cancel_at_period_end") or obj.get("cancel_at"))
            return org, "applied"
        if typ in ("invoice.paid", "invoice.payment_succeeded", "invoice.payment_failed"):
            org = self._org_for(s, obj)
            if org is None:
                return None, "ignored"
            sub = invoice_subscription(obj)
            if not sub or (org.stripe_subscription_id and sub != org.stripe_subscription_id):
                return org, "ignored"
            if org.billing_event_at and created < org.billing_event_at:
                return org, "stale"
            if typ == "invoice.payment_failed":
                if org.subscription_status in ACTIVE_SUBSCRIPTION:
                    org.subscription_status = "past_due"  # paid limits stop until an invoice is paid
            elif org.subscription_status in ("past_due", "unpaid"):
                org.subscription_status = "active"
            org.billing_event_at = created
            return org, "applied"
        return None, "ignored"

    @staticmethod
    def _org_for(s: Session, obj) -> Org | None:
        cust = _id(obj.get("customer"))
        if cust:
            org = s.execute(select(Org).where(Org.stripe_customer_id == cust)).scalar_one_or_none()
            if org is not None:
                return org
        org_id = (obj.get("metadata") or {}).get("org_id")
        return s.get(Org, org_id) if org_id else None
