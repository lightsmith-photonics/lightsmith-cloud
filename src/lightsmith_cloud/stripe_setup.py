"""Create the Stripe test-mode objects Lightsmith Cloud bills with: ``lightsmith-server stripe-setup``.

- One Billing Meter (``LIGHTSMITH_STRIPE_METER_EVENT_NAME``, sum of ``value`` per customer).
- One product per paid plan with a flat monthly price, and one "overage credits" product with a
  metered price per plan on that meter. Amounts come from ``billing.PLANS``.
- A customer-portal configuration: invoices, payment methods, cancel at period end. Plan switches go
  through ``POST /v1/billing/plan`` because the portal cannot update subscriptions with metered items.

The key is read from ``STRIPE_SECRET_KEY`` and must be a test key (``Settings`` refuses live keys).
It is never printed or stored. Re-running is safe: objects are found again by their fixed product
ids, price lookup keys, meter event name and portal metadata, and nothing is created twice. The
output is the ``.env`` lines to add (object ids, which are not secrets). The webhook signing secret
is not created here: ``stripe listen`` prints one for local testing, and the dashboard shows one for
a deployed endpoint.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable

from .billing import PAID_PLANS, PLANS
from .config import Settings

PORTAL_TAG = {"lightsmith": "cloud"}


def plan_objects(settings: Settings) -> list[dict[str, Any]]:
    """The products and prices to create, with the environment variable each price id goes to."""
    name = settings.hosted_product_name
    out = []
    for plan in PAID_PLANS:
        p = PLANS[plan]
        out.append({"env": f"LIGHTSMITH_STRIPE_PRICE_{plan.upper()}", "lookup_key": f"lightsmith_{plan}_monthly",
                    "product": {"id": f"lightsmith_cloud_{plan}", "name": f"{name} {plan.title()}",
                                "description": f"{p.monthly_credits:,} GPU credits per month included "
                                               "(1 credit = 1 GPU-second on the base tier)"},
                    "price": {"currency": "usd", "unit_amount": int(round(p.monthly_price_usd * 100)),
                              "recurring": {"interval": "month"}, "nickname": f"{plan} monthly"}})
        cents = Decimal(str(p.overage_usd_per_credit)) * 100
        out.append({"env": f"LIGHTSMITH_STRIPE_PRICE_OVERAGE_{plan.upper()}", "lookup_key": f"lightsmith_{plan}_overage",
                    "product": {"id": "lightsmith_cloud_overage", "name": f"{name} overage credits",
                                "description": "GPU credits beyond those included in the plan"},
                    "price": {"currency": "usd", "unit_amount_decimal": format(cents.normalize(), "f"),
                              "recurring": {"interval": "month", "usage_type": "metered"},
                              "nickname": f"{plan} overage per credit"}})
    return out


def _missing(e: Exception) -> bool:
    return getattr(e, "code", None) == "resource_missing" or getattr(e, "http_status", None) == 404


def setup(client, settings: Settings, *, portal: bool = True, log: Callable[[str], None] = print) -> dict[str, str]:
    """Find or create every object; returns {env var: id}."""
    v1 = client.v1
    event_name = settings.stripe_meter_event_name
    meter = next((m for m in v1.billing.meters.list(params={"status": "active", "limit": 100}).auto_paging_iter()
                  if m.event_name == event_name), None)
    if meter is None:
        meter = v1.billing.meters.create(params={
            "display_name": f"{settings.hosted_product_name} credits (overage)", "event_name": event_name,
            "default_aggregation": {"formula": "sum"},
            "customer_mapping": {"type": "by_id", "event_payload_key": "stripe_customer_id"},
            "value_settings": {"event_payload_key": "value"}})
        log(f"created meter {meter.id} ({event_name})")
    else:
        log(f"found meter {meter.id} ({event_name})")
    env = {"LIGHTSMITH_STRIPE_METER_EVENT_NAME": event_name}

    specs = plan_objects(settings)
    existing = {p.lookup_key: p for p in v1.prices.list(params={
        "lookup_keys": [s["lookup_key"] for s in specs], "active": True, "limit": 100}).data}
    products: dict[str, Any] = {}
    for spec in specs:
        want = dict(spec["price"])
        if want["recurring"].get("usage_type") == "metered":
            want["recurring"] = {**want["recurring"], "meter": meter.id}
        price = existing.get(spec["lookup_key"])
        if price is not None:
            same = Decimal(str(price.unit_amount_decimal)) == Decimal(str(want.get("unit_amount_decimal",
                                                                                     want.get("unit_amount"))))
            log(f"found price {price.id} ({spec['lookup_key']})"
                + ("" if same else "; its amount differs from billing.PLANS. Stripe prices are immutable: archive "
                                   "it in the dashboard (test mode) and run this again to create the new one"))
            env[spec["env"]] = price.id
            continue
        pid = spec["product"]["id"]
        if pid not in products:
            try:
                products[pid] = v1.products.retrieve(pid)
            except Exception as e:
                if not _missing(e):
                    raise
                products[pid] = v1.products.create(params={**spec["product"], "metadata": PORTAL_TAG})
                log(f"created product {pid}")
        price = v1.prices.create(params={**want, "product": pid, "lookup_key": spec["lookup_key"],
                                         "metadata": PORTAL_TAG})
        log(f"created price {price.id} ({spec['lookup_key']})")
        env[spec["env"]] = price.id

    if portal:
        found = next((c for c in v1.billing_portal.configurations.list(params={"active": True, "limit": 100})
                      .auto_paging_iter() if c.metadata and c.metadata.to_dict().get("lightsmith") == "cloud"), None)
        if found is None:
            found = v1.billing_portal.configurations.create(params={
                "business_profile": {"headline": f"{settings.hosted_product_name} billing"},
                "features": {"invoice_history": {"enabled": True},
                             "payment_method_update": {"enabled": True},
                             "subscription_cancel": {"enabled": True, "mode": "at_period_end",
                                                     "proration_behavior": "none"},
                             "subscription_update": {"enabled": False}},
                "metadata": PORTAL_TAG})
            log(f"created portal configuration {found.id}")
        else:
            log(f"found portal configuration {found.id}")
        env["LIGHTSMITH_STRIPE_PORTAL_CONFIGURATION"] = found.id
    return env


def dry_run(settings: Settings) -> str:
    lines = [f"meter: event_name={settings.stripe_meter_event_name}, sum of payload.value by stripe_customer_id"]
    for spec in plan_objects(settings):
        price = spec["price"]
        amount = (f"{price['unit_amount'] / 100:.2f} USD/month" if "unit_amount" in price
                  else f"{price['unit_amount_decimal']} US cents per credit, metered")
        lines.append(f"{spec['env']}: product {spec['product']['id']}, lookup_key {spec['lookup_key']}, {amount}")
    lines.append("portal configuration: invoices, payment methods, cancel at period end")
    return "\n".join(lines)


def main(settings: Settings, *, dry: bool = False, portal: bool = True, client=None) -> int:
    import sys

    if dry:
        print(dry_run(settings))
        return 0
    key = settings.stripe_secret_key.get_secret_value() if settings.stripe_secret_key else ""
    if not key and client is None:
        print("stripe-setup needs a Stripe TEST secret key in the environment variable STRIPE_SECRET_KEY "
              "(sk_test_... or rk_test_...). Use --dry-run to see what would be created.", file=sys.stderr)
        return 2
    if client is None:
        import stripe

        client = stripe.StripeClient(key)
    env = setup(client, settings, portal=portal, log=lambda m: print(m, file=sys.stderr))
    print("# Add to .env (Stripe test-mode object ids):")
    for k, v in env.items():
        print(f"{k}={v}")
    return 0
