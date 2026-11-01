"""Paid GPU compute end to end with Stripe in test mode: subscribe, run GPU jobs, meter the overage,
invoices paid and failed, plan changes, quotas and spending limits, admin views, and the setup
command. Stripe is the real `stripe` library over recorded fixtures (tests/stripe_fake.py); the
solver is replaced by fixed GPU run times so credits are exact. Set STRIPE_MOCK_URL to also check
every request against stripe-mock."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import gold_sphere, wait
from stripe_fake import FakeStripeHTTP, fixture, invoice, listing, not_found, subscription
from test_hosted import ADMIN, JWT_SECRET, PEPPER, WHSEC, _db_url, new_key, org_of, post_hook, sign, token
from lightsmith_cloud import executor, stripe_setup, worker
from lightsmith_cloud.app import create_app
from lightsmith_cloud.billing import PLANS, StripeBilling, overage_split
from lightsmith_cloud.config import Settings
from lightsmith_cloud.context import build_context
from lightsmith_cloud.db import Base, Org, UsageRecord
from lightsmith_cloud.executor import WorkerInfo
from lightsmith_cloud.sandbox import Outcome

PRICES = dict(stripe_price_pro="price_pro_test", stripe_price_team="price_team_test",
              stripe_price_overage_pro="price_pro_overage_test", stripe_price_overage_team="price_team_overage_test")
ADMIN_H = {"Authorization": f"Bearer {ADMIN}"}
CUSTOMER, SUB = "cus_test_life", "sub_test_life"


def stripe_routes(http: FakeStripeHTTP, state: dict) -> None:
    http.on("POST", "/v1/customers", lambda p: fixture("customer", id=CUSTOMER, metadata={"org_id": p["metadata[org_id]"]}))
    http.on("POST", "/v1/checkout/sessions", lambda p: fixture(
        "checkout_session", id="cs_test_life", url="https://checkout.stripe.com/c/pay/cs_test_life",
        customer=p["customer"], mode="subscription", client_reference_id=p["client_reference_id"]))
    http.on("POST", "/v1/billing_portal/sessions", lambda p: fixture(
        "portal_session", customer=p["customer"], url="https://billing.stripe.com/p/session/test_life"))
    http.on("GET", r"/v1/subscriptions/(sub_\w+)", lambda p, sid: state["sub"] if sid == SUB else not_found("sub"))
    http.on("POST", r"/v1/subscriptions/(sub_\w+)", lambda p, sid: state["sub"])
    http.on("POST", "/v1/billing/meter_events", lambda p: fixture(
        "meter_event", identifier=p["identifier"], event_name=p["event_name"],
        payload={"stripe_customer_id": p["payload[stripe_customer_id]"], "value": p["payload[value]"]}))


@pytest.fixture
def paid(tmp_path, estimator, monkeypatch):
    settings = Settings(mode="hosted", data_dir=tmp_path, database_url=_db_url(tmp_path), allow_sqlite=True,
                        auto_migrate=True, api_key_pepper=PEPPER, platform_jwt_secret=JWT_SECRET, admin_token=ADMIN,
                        stripe_secret_key="sk_test_fixture", stripe_webhook_secret=WHSEC, **PRICES,
                        stripe_portal_configuration="bpc_test_life", gpu_tiers="gpu-24g:24:1.0,gpu-80g:80:3.0")
    http, state = FakeStripeHTTP(), {}
    stripe_routes(http, state)
    redis = fakeredis.FakeRedis()
    ctx = build_context(settings, redis=redis, billing=StripeBilling(settings, client=http.client()),
                        with_estimator=False)
    ctx.estimator = estimator

    runtimes: list[float] = []

    def fake_solver(request, workdir, limits, *, on_event, should_cancel):
        """A GPU run of a fixed length: the solver's own correctness is covered by the other tests."""
        on_event({"type": "progress", "progress": 0.5, "stage": "solving"})
        with open(request["result_path"], "w") as f:
            json.dump({"kind": "cross_sections", "method": "fixture"}, f)
        return Outcome("succeeded", runtimes.pop(0), device={"framework": "torch", "device": "cuda:0"},
                       result_kind="cross_sections")

    monkeypatch.setattr(executor, "run_job", fake_solver)
    worker.configure(ctx, WorkerInfo(gpu_memory_bytes=24 * 2**30, queues=["gpu-24g", "gpu-80g"]))
    clock = iter(range(int(time.time()) - 10_000, int(time.time())))
    with TestClient(create_app(ctx=ctx)) as c:
        yield SimpleNamespace(client=c, ctx=ctx, http=http, state=state, runtimes=runtimes, next_ts=lambda: next(clock),
                              work=lambda: worker.make_worker(["gpu-24g", "gpu-80g"], redis).work(burst=True))
    worker._override = None
    ctx.db.dispose()


def hook(h, typ: str, obj: dict, *, eid: str | None = None, created: int | None = None) -> str:
    created = created or h.next_ts()
    payload = json.dumps({"id": eid or f"evt_{typ.replace('.', '_')}_{created}", "object": "event",
                          "api_version": "2026-10-28.endive", "created": created, "livemode": False, "type": typ,
                          "data": {"object": obj}, "pending_webhooks": 1,
                          "request": {"id": None, "idempotency_key": None}}).encode()
    r = post_hook(h, payload, sign(payload))
    assert r.status_code == 200, r.text
    return r.json()["outcome"]


def run_job(h, headers, runtime_s: float) -> dict:
    r = h.client.post("/v1/jobs", json={"simulation": gold_sphere()}, headers=headers)
    assert r.status_code == 202, r.text
    h.runtimes.append(runtime_s)
    h.work()
    job = wait(h.client, r.json()["job_id"], headers=headers)
    assert job["status"] == "succeeded", job["error"]
    return {**job, "created": r.json()}


def test_subscription_lifecycle(paid):
    h, c, user = paid, paid.client, token("ada")
    org = org_of(h, user)
    period_end = int(time.time()) + 20 * 86400
    pro_items = [(PRICES["stripe_price_pro"], False), (PRICES["stripe_price_overage_pro"], True)]

    # 1. Free org: what the billing page shows before subscribing.
    b = c.get("/v1/billing", headers=user).json()
    assert b["org"]["effective_plan"] == "free" and b["actions"]["checkout"] == ["pro", "team"]
    assert b["actions"]["change_plan"] == [] and not b["actions"]["portal"] and b["test_mode"]
    assert [p["purchasable"] for p in b["plans"]] == [False, True, True]

    # 2. Checkout: the customer is created once (idempotency key) and the session has the flat and metered prices.
    r = c.post("/v1/billing/checkout", headers=user, json={
        "plan": "pro", "success_url": "http://localhost:3000/billing?ok=1", "cancel_url": "http://localhost:3000/pricing"})
    assert r.status_code == 200 and r.json()["url"] == "https://checkout.stripe.com/c/pay/cs_test_life"
    [cust] = h.http.requests[:1]
    assert cust.path == "/v1/customers" and cust.headers["Idempotency-Key"] == f"customer-{org}"
    [sess] = h.http.sent("POST", "/v1/checkout/sessions")
    assert sess["mode"] == "subscription" and sess["customer"] == CUSTOMER and sess["client_reference_id"] == org
    assert (sess["line_items[0][price]"], sess["line_items[0][quantity]"]) == ("price_pro_test", "1")
    assert sess["line_items[1][price]"] == "price_pro_overage_test" and "line_items[1][quantity]" not in sess
    assert sess["subscription_data[metadata][plan]"] == "pro"

    # 3. Stripe confirms: checkout completed, subscription created.
    done = fixture("checkout_session", id="cs_test_life", mode="subscription", customer=CUSTOMER, subscription=SUB,
                   client_reference_id=org, status="complete")
    assert hook(h, "checkout.session.completed", done) == "applied"
    h.state["sub"] = subscription(SUB, CUSTOMER, org, pro_items, period_end=period_end)
    assert hook(h, "customer.subscription.created", h.state["sub"]) == "applied"
    b = c.get("/v1/billing", headers=user).json()
    assert b["org"]["plan"] == b["org"]["effective_plan"] == "pro" and b["org"]["subscription_status"] == "active"
    assert b["org"]["current_period_end"].startswith(time.strftime("%Y-%m-%d", time.gmtime(period_end)))
    assert b["actions"] == {"checkout": [], "change_plan": ["team"], "portal": True}
    r = c.post("/v1/billing/checkout", headers=user, json={
        "plan": "team", "success_url": "http://localhost:3000/a", "cancel_url": "http://localhost:3000/b"})
    assert r.status_code == 409 and r.json()["error"] == "AlreadySubscribed"
    portal = c.post("/v1/billing/portal", headers=user, json={"return_url": "http://localhost:3000/billing"}).json()
    assert portal["url"].startswith("https://billing.stripe.com/")
    assert h.http.sent("POST", "/v1/billing_portal/sessions")[0]["configuration"] == "bpc_test_life"

    # 4. GPU jobs near the end of the included credits: soft-limit warning, then overage metered in whole units.
    key = new_key(h, "ada")
    with h.ctx.db.session() as s:
        s.add(UsageRecord(org_id=org, job_id="a" * 32, credits=35_900))
    assert c.get("/v1/usage", headers=key).json()["state"] == "soft_limit"
    j1 = run_job(h, key, runtime_s=120.0)  # 120 GPU-s on the base tier = 120 credits
    assert "monthly credits" in j1["created"]["quota_warning"] and j1["usage"]["credits"] == 120.0
    usage = c.get("/v1/usage", headers=key).json()
    assert usage["state"] == "overage" and usage["overage_credits"] == 20.0 and usage["credits_remaining"] == 0
    [ev] = h.http.sent("POST", "/v1/billing/meter_events")
    assert ev["event_name"] == "lightsmith_credits" and ev["payload[stripe_customer_id]"] == CUSTOMER
    assert ev["payload[value]"] == "20"
    with h.ctx.db.session() as s:
        rec = s.execute(select(UsageRecord).where(UsageRecord.job_id == j1["id"])).scalar_one()
        assert ev["identifier"] == f"usage_{rec.id}" and rec.billable_units == 20 and rec.stripe_reported_at

    j2 = run_job(h, key, runtime_s=0.6)  # 20.6 overage credits in total: nothing new to bill yet
    assert "billed at $0.0005" in j2["created"]["quota_warning"]
    assert len(h.http.sent("POST", "/v1/billing/meter_events")) == 1
    h.http.fail["/v1/billing/meter_events"] = 1  # Stripe is down for the next report
    run_job(h, key, runtime_s=0.5)  # 21.1 in total: one more unit, whose report fails
    assert c.get("/v1/admin/usage", headers=ADMIN_H).json()["totals"]["units_unreported_to_stripe"] == 1
    assert c.post("/v1/admin/billing/sync", headers=ADMIN_H).json() == {"checked": 1, "reported": 1, "failed": 0}
    sent = h.http.sent("POST", "/v1/billing/meter_events")
    assert [e["payload[value]"] for e in sent] == ["20", "1", "1"] and sent[1]["identifier"] == sent[2]["identifier"]
    assert c.post("/v1/admin/billing/sync", headers=ADMIN_H).json()["checked"] == 0

    # 5. The invoice is paid; then an upgrade to team through the API (Stripe confirms by webhook).
    assert hook(h, "invoice.paid", invoice(CUSTOMER, SUB, "paid")) == "applied"
    r = c.post("/v1/billing/plan", headers=user, json={"plan": "team"})
    assert r.json() == {"status": "pending", "plan": "team", "previous_plan": "pro"}
    [upd] = h.http.sent("POST", f"/v1/subscriptions/{SUB}")
    assert upd["items[0][id]"] == f"si_{SUB}_1" and upd["items[0][price]"] == "price_team_test"
    assert upd["items[1][id]"] == f"si_{SUB}_2" and upd["items[1][price]"] == "price_team_overage_test"
    assert upd["proration_behavior"] == "create_prorations"
    team_items = [(PRICES["stripe_price_team"], False), (PRICES["stripe_price_overage_team"], True)]
    h.state["sub"] = subscription(SUB, CUSTOMER, org, team_items, period_end=period_end)
    before_upgrade = h.next_ts()
    assert hook(h, "customer.subscription.updated", h.state["sub"]) == "applied"
    assert c.get("/v1/me", headers=key).json()["plan"]["name"] == "team"
    assert c.post("/v1/billing/plan", headers=user, json={"plan": "team"}).json()["status"] == "unchanged"

    # 6. A renewal payment fails: the org drops to the free plan's limits, so jobs are refused.
    failed = invoice(CUSTOMER, SUB, "open")
    assert hook(h, "invoice.payment_failed", failed, eid="evt_failed_1") == "applied"
    b = c.get("/v1/billing", headers=user).json()
    assert b["payment_issue"] and b["org"]["plan"] == "team" and b["org"]["effective_plan"] == "free"
    assert b["usage"]["state"] == "blocked" and b["actions"]["change_plan"] == []
    r = c.post("/v1/jobs", json={"simulation": gold_sphere()}, headers=key)
    assert r.status_code == 402 and r.json()["error"] == "QuotaExceeded" and r.json()["plan"] == "free"
    # Redelivery and a late, older event change nothing.
    assert hook(h, "invoice.payment_failed", failed, eid="evt_failed_1") == "duplicate"
    assert hook(h, "customer.subscription.updated", h.state["sub"], created=before_upgrade - 1) == "stale"
    assert c.get("/v1/me", headers=key).json()["org"]["subscription_status"] == "past_due"

    # 7. The retry succeeds: paid limits are back.
    assert hook(h, "invoice.paid", invoice(CUSTOMER, SUB, "paid")) == "applied"
    assert c.get("/v1/billing", headers=user).json()["org"]["effective_plan"] == "team"
    run_job(h, key, runtime_s=10.0)

    # 8. Cancelled at period end, then deleted: back to free, and Checkout is offered again.
    h.state["sub"] = subscription(SUB, CUSTOMER, org, team_items, period_end=period_end, cancel_at_period_end=True)
    assert hook(h, "customer.subscription.updated", h.state["sub"]) == "applied"
    assert c.get("/v1/billing", headers=user).json()["org"]["cancel_at_period_end"] is True
    assert hook(h, "customer.subscription.deleted", {**h.state["sub"], "status": "canceled"}) == "applied"
    b = c.get("/v1/billing", headers=user).json()
    assert b["org"]["plan"] == "free" and b["org"]["subscription_status"] == "canceled"
    assert b["actions"]["checkout"] == ["pro", "team"] and b["org"]["current_period_end"] is None

    # 9. Admin views.
    detail = c.get(f"/v1/admin/orgs/{org}", headers=ADMIN_H).json()
    assert detail["stripe_customer_id"] == CUSTOMER and detail["effective_plan"] == "free"
    assert sum(r["billable_units"] for r in detail["usage_records"]) == 21
    assert all(r["stripe_reported_at"] for r in detail["usage_records"] if r["billable_units"])
    outcomes = {e["type"]: e["outcome"] for e in detail["stripe_events"]}
    assert outcomes["checkout.session.completed"] == "applied" and "stale" in [e["outcome"] for e in detail["stripe_events"]]
    totals = c.get("/v1/admin/usage", headers=ADMIN_H).json()["totals"]
    assert totals["jobs"] == 5 and totals["billable_units"] == 21 and totals["units_unreported_to_stripe"] == 0
    assert totals["overage_credits"] == pytest.approx(21.1)
    days = c.get("/v1/admin/usage/daily", headers=ADMIN_H).json()["days"]
    assert sum(d["billable_units"] for d in days) == 21


def test_hard_limits_and_reservations(paid):
    h, c = paid, paid.client
    user = token("bob")
    key = new_key(h, "bob")
    org = org_of(h, key)
    c.put(f"/v1/admin/orgs/{org}/plan", json={"plan": "pro"}, headers=ADMIN_H)
    ce = c.post("/v1/estimate", json={"simulation": gold_sphere()}, headers=key).json()["credits_estimate"]

    # The org's own spending limit: set with a login token only.
    assert c.put("/v1/billing/limits", json={"monthly_credit_limit": 10}, headers=key).status_code == 403
    q = c.put("/v1/billing/limits", json={"monthly_credit_limit": ce - 1}, headers=user).json()
    assert q["hard_limit"] == ce - 1 and q["credit_limit"] == ce - 1
    r = c.post("/v1/jobs", json={"simulation": gold_sphere()}, headers=key)
    assert r.status_code == 402 and "credit limit" in r.json()["message"]
    assert c.put("/v1/billing/limits", json={"monthly_credit_limit": None}, headers=user).json()["hard_limit"] == 360_000

    # Queued jobs hold their estimate: with room for one job, the second is refused until the first ran.
    with h.ctx.db.session() as s:
        s.add(UsageRecord(org_id=org, job_id="c" * 32, credits=360_000 - 1.5 * ce))
    assert c.post("/v1/jobs", json={"simulation": gold_sphere()}, headers=key).status_code == 202
    assert c.get("/v1/usage", headers=key).json()["credits_reserved"] == ce
    r = c.post("/v1/jobs", json={"simulation": gold_sphere()}, headers=key)
    assert r.status_code == 402 and r.json()["credits_reserved"] == ce
    # The operator raises this org's hard limit.
    view = c.put(f"/v1/admin/orgs/{org}/limits", json={"credit_cap_override": 400_000}, headers=ADMIN_H).json()
    assert view["usage"]["hard_limit"] == 400_000
    assert c.post("/v1/jobs", json={"simulation": gold_sphere()}, headers=key).status_code == 202
    assert c.put(f"/v1/admin/orgs/{org}/limits", json={"credit_cap_override": 1}, headers=token()).status_code == 403


def test_free_plan_is_never_billed(paid):
    h = paid
    key = new_key(h, "eve")
    with h.ctx.db.session() as s:
        s.get(Org, org_of(h, key)).stripe_customer_id = "cus_free"
    run_job(h, key, runtime_s=30.0)
    assert h.http.sent("POST", "/v1/billing/meter_events") == []
    q = h.client.get("/v1/usage", headers=key).json()
    assert q["credits_used"] == 30.0 and q["overage_credits"] == 0 and q["state"] == "ok"


def test_webhook_edge_cases(paid):
    h, c = paid, paid.client
    org = org_of(h, token("kim"))
    items = [(PRICES["stripe_price_pro"], False), (PRICES["stripe_price_overage_pro"], True)]
    end = int(time.time()) + 86400
    new = subscription("sub_new", "cus_kim", org, items, period_end=end)
    assert hook(h, "customer.subscription.created", new) == "applied"
    # An older subscription of the same customer ending must not cancel the live one.
    old = subscription("sub_old", "cus_kim", org, items, status="canceled", period_end=end)
    assert hook(h, "customer.subscription.deleted", old) == "ignored"
    # Invoices of another subscription, unknown customers and unhandled types are ignored.
    assert hook(h, "invoice.payment_failed", invoice("cus_kim", "sub_old", "open")) == "ignored"
    assert hook(h, "invoice.paid", invoice("cus_nobody", "sub_x", "paid")) == "ignored"
    assert hook(h, "charge.refunded", {"id": "ch_1", "object": "charge"}) == "ignored"
    me = c.get("/v1/me", headers=token("kim")).json()
    assert me["org"]["plan"] == "pro" and me["org"]["subscription_status"] == "active"
    # Payment failure on the first invoice keeps an incomplete subscription incomplete.
    with h.ctx.db.session() as s:
        s.get(Org, org).subscription_status = "incomplete"
    assert hook(h, "invoice.payment_failed", invoice("cus_kim", "sub_new", "open")) == "applied"
    assert c.get("/v1/me", headers=token("kim")).json()["org"]["subscription_status"] == "incomplete"


def test_billing_endpoints_need_a_login(paid):
    c = paid.client
    key = new_key(paid, "zed")
    body = {"plan": "pro", "success_url": "http://localhost:3000/a", "cancel_url": "http://localhost:3000/b"}
    assert c.post("/v1/billing/checkout", json=body, headers=key).status_code == 403
    assert c.post("/v1/billing/portal", json={"return_url": "http://localhost:3000"}, headers=key).status_code == 403
    assert c.post("/v1/billing/plan", json={"plan": "team"}, headers=key).status_code == 403
    r = c.post("/v1/billing/plan", json={"plan": "team"}, headers=token("zed"))
    assert r.status_code == 409 and r.json()["error"] == "NoActiveSubscription"
    assert c.get("/v1/billing", headers=key).status_code == 200  # reading is fine with a key
    assert c.get("/v1/billing").status_code == 401


def test_stripe_errors_become_billing_errors(paid):
    paid.http.on("POST", "/v1/checkout/sessions", lambda p: (400, {"error": {
        "type": "invalid_request_error", "message": "No such price: 'price_pro_test'"}}))
    r = paid.client.post("/v1/billing/checkout", headers=token("al"), json={
        "plan": "pro", "success_url": "http://localhost:3000/a", "cancel_url": "http://localhost:3000/b"})
    assert r.status_code == 502 and r.json()["error"] == "BillingError" and "No such price" in r.json()["message"]


@pytest.mark.parametrize("used,credits,expected", [
    (0, 100, (0.0, 0)), (35_999, 2.5, (1.5, 1)), (36_000.9, 0.3, (0.3, 1)), (36_001.2, 0.7, (0.7, 0)),
    (40_000, 10.25, (10.25, 10))])
def test_overage_split(used, credits, expected):
    assert overage_split(used, credits, PLANS["pro"]) == pytest.approx(expected)
    assert overage_split(used, credits, PLANS["free"]) == (0.0, 0)


# --- setup command ------------------------------------------------------------


def stateful_stripe() -> FakeStripeHTTP:
    """Just enough of Stripe's state for stripe-setup: meters, products, prices, portal configurations."""
    http = FakeStripeHTTP()
    db: dict[str, dict] = {"meters": {}, "products": {}, "prices": {}, "portal": {}}
    n = iter(range(1, 1000))

    def create_meter(p):
        m = fixture("meter", id=f"mtr_{next(n)}", event_name=p["event_name"], status="active")
        db["meters"][m["id"]] = m
        return m

    def create_product(p):
        db["products"][p["id"]] = fixture("product", id=p["id"], name=p["name"])
        return db["products"][p["id"]]

    def create_price(p):
        amount = p.get("unit_amount_decimal") or p["unit_amount"]
        price = fixture("price", id=f"price_{next(n)}", product=p["product"], lookup_key=p["lookup_key"],
                        unit_amount=int(p["unit_amount"]) if "unit_amount" in p else None, unit_amount_decimal=amount)
        db["prices"][price["id"]] = price
        return price

    def list_prices(p):
        keys = {v for k, v in p.items() if k.startswith("lookup_keys")}
        return listing("/v1/prices", [x for x in db["prices"].values() if x["lookup_key"] in keys])

    def create_portal(p):
        cfg = fixture("portal_configuration", id=f"bpc_{next(n)}", metadata={"lightsmith": p["metadata[lightsmith]"]})
        db["portal"][cfg["id"]] = cfg
        return cfg

    http.on("GET", "/v1/billing/meters", lambda p: listing("/v1/billing/meters", list(db["meters"].values())))
    http.on("POST", "/v1/billing/meters", create_meter)
    http.on("GET", r"/v1/products/(\w+)", lambda p, pid: db["products"].get(pid) or not_found("product"))
    http.on("POST", "/v1/products", create_product)
    http.on("GET", "/v1/prices", list_prices)
    http.on("POST", "/v1/prices", create_price)
    http.on("GET", "/v1/billing_portal/configurations",
            lambda p: listing("/v1/billing_portal/configurations", list(db["portal"].values())))
    http.on("POST", "/v1/billing_portal/configurations", create_portal)
    return http


def test_stripe_setup_creates_once(capsys):
    http = stateful_stripe()
    settings = Settings(stripe_secret_key="sk_test_fixture")
    assert stripe_setup.main(settings, client=http.client()) == 0
    out = capsys.readouterr()
    env = dict(line.split("=", 1) for line in out.out.splitlines() if "=" in line and not line.startswith("#"))
    assert set(env) == {"LIGHTSMITH_STRIPE_METER_EVENT_NAME", "LIGHTSMITH_STRIPE_PRICE_PRO",
                        "LIGHTSMITH_STRIPE_PRICE_OVERAGE_PRO", "LIGHTSMITH_STRIPE_PRICE_TEAM",
                        "LIGHTSMITH_STRIPE_PRICE_OVERAGE_TEAM", "LIGHTSMITH_STRIPE_PORTAL_CONFIGURATION"}
    assert "sk_test_fixture" not in out.out + out.err  # the key is never printed

    [meter] = http.sent("POST", "/v1/billing/meters")
    assert meter["default_aggregation[formula]"] == "sum"
    assert meter["customer_mapping[event_payload_key]"] == "stripe_customer_id"
    assert meter["value_settings[event_payload_key]"] == "value"
    prices = {p["lookup_key"]: p for p in http.sent("POST", "/v1/prices")}
    assert prices["lightsmith_pro_monthly"]["unit_amount"] == "2900"
    assert prices["lightsmith_team_monthly"]["unit_amount"] == "12900"
    assert prices["lightsmith_pro_overage"]["unit_amount_decimal"] == "0.05"
    assert prices["lightsmith_team_overage"]["unit_amount_decimal"] == "0.04"
    for k in ("lightsmith_pro_overage", "lightsmith_team_overage"):
        assert prices[k]["recurring[usage_type]"] == "metered" and prices[k]["recurring[meter]"].startswith("mtr_")
        assert prices[k]["product"] == "lightsmith_cloud_overage"
    assert sorted(p["id"] for p in http.sent("POST", "/v1/products")) == [
        "lightsmith_cloud_overage", "lightsmith_cloud_pro", "lightsmith_cloud_team"]
    [portal] = http.sent("POST", "/v1/billing_portal/configurations")
    assert portal["features[subscription_cancel][mode]"] == "at_period_end"
    assert portal["features[subscription_update][enabled]"] == "false"

    posts = len([r for r in http.requests if r.method == "POST"])
    assert stripe_setup.main(settings, client=http.client()) == 0  # second run: finds everything
    again = capsys.readouterr().out
    assert len([r for r in http.requests if r.method == "POST"]) == posts
    assert dict(line.split("=", 1) for line in again.splitlines() if "=" in line and not line.startswith("#")) == env


def test_stripe_setup_without_a_key_calls_nothing(capsys, monkeypatch):
    for var in ("STRIPE_SECRET_KEY", "LIGHTSMITH_STRIPE_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert stripe_setup.main(Settings()) == 2
    assert "TEST secret key" in capsys.readouterr().err
    assert stripe_setup.main(Settings(), dry=True) == 0
    assert "29.00 USD/month" in capsys.readouterr().out
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_" + "0" * 24)
    with pytest.raises(ValueError, match="test-mode"):
        Settings()


def test_migrations_match_models(tmp_path):
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine

    from lightsmith_cloud import migrate

    engine = create_engine(f"sqlite:///{(tmp_path / 'm.db').as_posix()}")
    migrate.upgrade(engine)
    with engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
    engine.dispose()


@pytest.mark.skipif(not os.environ.get("STRIPE_MOCK_URL"), reason="set STRIPE_MOCK_URL to a running stripe-mock")
def test_requests_validate_against_stripe_mock(tmp_path):
    """stripe-mock checks every request against Stripe's OpenAPI spec (its answers are generic fixtures)."""
    import stripe

    client = stripe.StripeClient("sk_test_123", base_addresses={"api": os.environ["STRIPE_MOCK_URL"]})
    settings = Settings(stripe_secret_key="sk_test_123", stripe_portal_configuration="bpc_123", **PRICES)
    assert set(stripe_setup.setup(client, settings, log=lambda m: None)) >= {"LIGHTSMITH_STRIPE_PRICE_PRO"}
    billing = StripeBilling(settings, client=client)
    org = Org(id="user:x", plan="pro", stripe_customer_id="cus_123", stripe_subscription_id="sub_123",
              subscription_status="active")
    billing.checkout_url(SimpleNamespace(flush=lambda: None), Org(id="user:y", plan="free", stripe_customer_id="cus_1"),
                         "pro", "http://localhost:3000/a", "http://localhost:3000/b")
    billing.portal_url(None, org, "http://localhost:3000/b")
    rec = UsageRecord(id="r" * 32, org_id=org.id, job_id="j" * 32, credits=5.0, billable_units=5,
                      created_at=datetime.now(timezone.utc))
    billing.report_usage(None, org, rec)
    assert rec.stripe_reported_at is not None, rec.stripe_error
