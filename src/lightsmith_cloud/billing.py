"""Plans, quotas, metering and Stripe (test mode only).

Unit of account: **1 credit = 1 GPU-second on the base GPU tier**. Larger
tiers have a credit multiplier, and CPU runs a fraction (``LIGHTSMITH_CPU_CREDIT_MULTIPLIER``).
Usage is metered per job from the solver subprocess's wall time.

Stripe: one customer per org; a subscription with a flat plan price plus a
metered price driven by a Billing Meter (``LIGHTSMITH_STRIPE_METER_EVENT_NAME``).
Each finished job sends one meter event whose identifier is the usage record
id, so retries never double-bill. Included credits are best modelled on the
Stripe side as the first graduated tier of the metered price at $0.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import GpuTier, Settings
from .db import Org, StripeEvent, UsageRecord, utcnow
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

    def public(self) -> dict[str, Any]:
        return asdict(self)


PLANS: dict[str, Plan] = {
    "free": Plan("free", monthly_credits=1_800, overage=False, max_concurrent_jobs=1, max_runtime_s=300,
                 max_memory_bytes=16 * GiB, host_memory_bytes=8 * GiB, rate_limit_per_minute=60,
                 max_spec_bytes=256_000),
    "pro": Plan("pro", monthly_credits=36_000, overage=True, max_concurrent_jobs=4, max_runtime_s=3_600,
                max_memory_bytes=72 * GiB, host_memory_bytes=32 * GiB, rate_limit_per_minute=300,
                max_spec_bytes=2_000_000),
    "team": Plan("team", monthly_credits=180_000, overage=True, max_concurrent_jobs=16, max_runtime_s=4 * 3_600,
                 max_memory_bytes=72 * GiB, host_memory_bytes=64 * GiB, rate_limit_per_minute=1_200,
                 max_spec_bytes=2_000_000),
}

ACTIVE_SUBSCRIPTION = ("active", "trialing")


def effective_plan(org: Org) -> Plan:
    """Paid plans apply while the Stripe subscription is active (or when an admin set the plan
    without a subscription, i.e. no subscription status)."""
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


def check_quota(s: Session, org: Org, plan: Plan, used: float, credits_needed: float) -> None:
    if not plan.overage and used + credits_needed > plan.monthly_credits:
        raise ApiError(402, "QuotaExceeded",
                       f"this job needs about {credits_needed:g} credits and {used:g} of the "
                       f"{plan.monthly_credits} monthly credits on the {plan.name} plan are used; "
                       "upgrade the plan or wait for the next month", plan=plan.name)


# --- Stripe -----------------------------------------------------------------

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

    def price_for(self, plan: str) -> str | None:
        return {"pro": self.settings.stripe_price_pro, "team": self.settings.stripe_price_team}.get(plan)

    def plan_for_price(self, price_id: str | None) -> str | None:
        return {v: k for k, v in (("pro", self.settings.stripe_price_pro),
                                  ("team", self.settings.stripe_price_team)) if v}.get(price_id or "")

    def check_redirect(self, url: str) -> str:
        base = self.settings.public_web_url.rstrip("/")
        if not (url == base or url.startswith(base + "/")):
            raise ApiError(422, "ValidationError", f"redirect URLs must start with {base}")
        return url

    def ensure_customer(self, s: Session, org: Org) -> str:
        self._require()
        if not org.stripe_customer_id:
            # Only the org id: names and emails belong to the platform, which can update the customer.
            c = self.client.v1.customers.create(params={"metadata": {"org_id": org.id}})
            org.stripe_customer_id = c.id
            s.flush()
        return org.stripe_customer_id

    def checkout_url(self, s: Session, org: Org, plan: str, success_url: str, cancel_url: str) -> str:
        self._require()
        price = self.price_for(plan)
        if not price:
            raise ApiError(422, "ValidationError", f"plan {plan!r} has no Stripe price configured")
        items: list[dict] = [{"price": price, "quantity": 1}]
        if self.settings.stripe_price_metered:
            items.append({"price": self.settings.stripe_price_metered})
        sess = self.client.v1.checkout.sessions.create(params={
            "mode": "subscription", "customer": self.ensure_customer(s, org), "client_reference_id": org.id,
            "line_items": items, "success_url": self.check_redirect(success_url),
            "cancel_url": self.check_redirect(cancel_url),
            "subscription_data": {"metadata": {"org_id": org.id, "plan": plan}},
        })
        return sess.url

    def portal_url(self, s: Session, org: Org, return_url: str) -> str:
        self._require()
        sess = self.client.v1.billing_portal.sessions.create(params={
            "customer": self.ensure_customer(s, org), "return_url": self.check_redirect(return_url)})
        return sess.url

    def report_usage(self, s: Session, org: Org, rec: UsageRecord) -> None:
        """Send one meter event for a usage record (idempotent by record id). Errors are stored, not raised."""
        if not self.enabled or not org.stripe_customer_id or rec.stripe_reported_at or rec.credits <= 0:
            return
        try:
            self.client.v1.billing.meter_events.create(params={
                "event_name": self.settings.stripe_meter_event_name,
                "identifier": f"usage_{rec.id}",
                "payload": {"stripe_customer_id": org.stripe_customer_id, "value": str(math.ceil(rec.credits))},
                "timestamp": int(rec.created_at.timestamp()),
            })
            rec.stripe_reported_at, rec.stripe_error = utcnow(), None
        except Exception as e:  # network or API error: keep for the next sync
            rec.stripe_error = f"{type(e).__name__}: {e}"[:500]

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
        """Update orgs from a verified event. Returns 'applied', 'duplicate' or 'ignored'."""
        if s.get(StripeEvent, event["id"]) is not None:
            return "duplicate"
        s.add(StripeEvent(id=event["id"], type=event["type"]))
        obj = event["data"]["object"]
        typ = event["type"]
        org = None
        if typ == "checkout.session.completed":
            org = s.get(Org, obj.get("client_reference_id") or "")
            if org is not None:
                org.stripe_customer_id = obj.get("customer") or org.stripe_customer_id
                org.stripe_subscription_id = obj.get("subscription") or org.stripe_subscription_id
        elif typ.startswith("customer.subscription."):
            org = self._org_for(s, obj)
            if org is not None:
                org.stripe_subscription_id = obj.get("id")
                org.subscription_status = "canceled" if typ.endswith(".deleted") else obj.get("status")
                plan = (obj.get("metadata") or {}).get("plan")
                for item in ((obj.get("items") or {}).get("data") or []):
                    plan = self.plan_for_price((item.get("price") or {}).get("id")) or plan
                if typ.endswith(".deleted"):
                    org.plan = "free"
                elif plan in PLANS:
                    org.plan = plan
        elif typ in ("invoice.payment_failed", "invoice.paid"):
            org = self._org_for(s, obj)
            if org is not None and typ == "invoice.payment_failed":
                org.subscription_status = "past_due"
        return "applied" if org is not None else "ignored"

    @staticmethod
    def _org_for(s: Session, obj) -> Org | None:
        cust = obj.get("customer")
        if cust:
            org = s.execute(select(Org).where(Org.stripe_customer_id == cust)).scalar_one_or_none()
            if org is not None:
                return org
        org_id = (obj.get("metadata") or {}).get("org_id")
        return s.get(Org, org_id) if org_id else None
