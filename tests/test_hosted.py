"""Hosted mode: auth, quotas, metering, RQ workers (fakeredis), Stripe webhooks (test fixtures, no network)."""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import os
import time
from types import SimpleNamespace

import fakeredis
import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import gold_sphere, wait
from lightsmith_cloud import worker
from lightsmith_cloud.app import create_app
from lightsmith_cloud.billing import PLANS, StripeBilling
from lightsmith_cloud.config import Settings
from lightsmith_cloud.context import build_context
from lightsmith_cloud.db import Base, Org, UsageRecord
from lightsmith_cloud.executor import WorkerInfo

PEPPER, ADMIN, WHSEC = "pepper-test", "admin-token-test", "whsec_test_fixture"
JWT_SECRET = "jwt-secret-test-0123456789abcdef-0123"


class FakeStripe:
    """Records calls made through StripeClient.v1.*; never touches the network."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

        def rec(name, ret):
            def f(params=None, options=None):
                self.calls.append((name, params))
                return ret
            return f

        self.v1 = SimpleNamespace(
            customers=SimpleNamespace(create=rec("customers.create", SimpleNamespace(id="cus_test_1"))),
            checkout=SimpleNamespace(sessions=SimpleNamespace(
                create=rec("checkout.create", SimpleNamespace(url="https://checkout.stripe.com/c/pay/cs_test_1")))),
            billing_portal=SimpleNamespace(sessions=SimpleNamespace(
                create=rec("portal.create", SimpleNamespace(url="https://billing.stripe.com/p/session/test_1")))),
            billing=SimpleNamespace(meter_events=SimpleNamespace(create=rec("meter_events.create", None))),
        )

    def named(self, name):
        return [p for n, p in self.calls if n == name]


# Set LIGHTSMITH_TEST_DATABASE_URL=postgresql+psycopg://... to run these against Postgres
# (the database is wiped before each test); otherwise they use SQLite.
PG_URL = os.environ.get("LIGHTSMITH_TEST_DATABASE_URL")


def _db_url(tmp_path) -> str:
    if not PG_URL:
        return f"sqlite:///{(tmp_path / 'h.db').as_posix()}"
    from sqlalchemy import create_engine, text

    engine = create_engine(PG_URL)
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    engine.dispose()
    return PG_URL


@pytest.fixture
def hosted(tmp_path, estimator):
    settings = Settings(mode="hosted", data_dir=tmp_path, database_url=_db_url(tmp_path), allow_sqlite=True,
                        auto_migrate=True, api_key_pepper=PEPPER, platform_jwt_secret=JWT_SECRET, admin_token=ADMIN,
                        stripe_secret_key="sk_test_fixture", stripe_webhook_secret=WHSEC,
                        stripe_price_pro="price_pro_test", stripe_price_metered="price_metered_test",
                        gpu_tiers="gpu-24g:24:1.0,gpu-80g:80:3.0", public_web_url="http://localhost:3000")
    redis = fakeredis.FakeRedis()
    fake = FakeStripe()
    ctx = build_context(settings, redis=redis, billing=StripeBilling(settings, client=fake), with_estimator=False)
    ctx.estimator = estimator
    app = create_app(ctx=ctx)
    worker.configure(ctx, WorkerInfo(gpu_memory_bytes=0, queues=["gpu-24g", "gpu-80g"]))
    with TestClient(app) as c:
        yield SimpleNamespace(client=c, ctx=ctx, redis=redis, stripe=fake,
                              work=lambda: worker.make_worker(["gpu-24g", "gpu-80g"], redis).work(burst=True))
    worker._override = None
    ctx.db.dispose()


def token(sub="user-1", name="Ada", role="user", secret=JWT_SECRET, **extra) -> dict:
    claims = {"sub": sub, "name": name, "role": role, "aud": "compute", "iss": "lightsmith-web",
              "iat": int(time.time()), "exp": int(time.time()) + 300, **extra}
    return {"Authorization": "Bearer " + jwt.encode(claims, secret, algorithm="HS256")}


def new_key(h, sub="user-1") -> dict:
    r = h.client.post("/v1/keys", json={"name": "ci"}, headers=token(sub))
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['key']}"}


def org_of(h, headers) -> str:
    return h.client.get("/v1/me", headers=headers).json()["org"]["id"]


# --- auth ---------------------------------------------------------------------


def test_auth_required(hosted):
    c = hosted.client
    assert c.get("/v1/health").status_code == 200
    assert c.get("/v1/schema").status_code == 200
    r = c.get("/v1/jobs")
    assert r.status_code == 401 and r.json()["error"] == "Unauthorized" and r.headers["www-authenticate"] == "Bearer"
    assert c.get("/v1/jobs", headers={"Authorization": "Bearer lsk_abc_def"}).status_code == 401
    assert c.get("/v1/jobs", headers=token(secret="wrong-secret-0123456789abcdef-0123456")).status_code == 401
    assert c.get("/v1/jobs", headers=token(exp=int(time.time()) - 10)).status_code == 401
    assert c.get("/v1/jobs", headers={"Authorization": "Bearer nonsense"}).status_code == 401
    assert c.get("/v1/jobs", headers=token(aud="materials-db")).status_code == 401  # token for another service


def test_jwt_provisioning_and_api_keys(hosted):
    c = hosted.client
    me = c.get("/v1/me", headers=token()).json()
    assert me["kind"] == "jwt" and me["org"]["plan"] == "free" and me["plan"]["name"] == "free"
    key = new_key(hosted)
    me2 = c.get("/v1/me", headers=key).json()
    assert me2["kind"] == "api_key" and me2["org"]["id"] == me["org"]["id"]
    # keys are hashed at rest
    secret = key["Authorization"].split()[1]
    with hosted.ctx.db.session() as s:
        from lightsmith_cloud.db import ApiKey
        rows = s.execute(select(ApiKey)).scalars().all()
        assert all(secret not in (r.hash + r.prefix) for r in rows)
    assert c.post("/v1/keys", json={}, headers=key).status_code == 403  # keys cannot mint keys
    keys = c.get("/v1/keys", headers=token()).json()["keys"]
    assert c.delete(f"/v1/keys/{keys[0]['id']}", headers=token()).json()["revoked_at"]
    assert c.get("/v1/me", headers=key).status_code == 401
    # another user cannot see this org's jobs or keys
    assert c.get("/v1/keys", headers=token("user-2")).json()["keys"] == []


def test_admin_endpoints(hosted):
    c = hosted.client
    assert c.get("/v1/admin/usage", headers=token()).status_code == 403
    admin = {"Authorization": f"Bearer {ADMIN}"}
    assert c.get("/v1/admin/usage", headers=admin).status_code == 200
    assert c.get("/v1/admin/usage", headers=token("boss", role="admin")).status_code == 200
    org = org_of(hosted, token())
    assert c.put(f"/v1/admin/orgs/{org}/plan", json={"plan": "pro"}, headers=admin).json()["plan"] == "pro"
    assert c.get("/v1/me", headers=token()).json()["plan"]["name"] == "pro"


# --- jobs, metering, quotas ---------------------------------------------------------


def test_hosted_job_is_queued_run_and_metered(hosted):
    c = hosted.client
    key = new_key(hosted)
    with hosted.ctx.db.session() as s:
        org = s.get(Org, org_of(hosted, key))
        org.stripe_customer_id, org.plan = "cus_test_1", "pro"
        # Only overage goes to Stripe: start just past the included credits (0.9 into overage).
        s.add(UsageRecord(org_id=org.id, job_id="b" * 32, credits=PLANS["pro"].monthly_credits + 0.9))
    est = c.post("/v1/estimate", json={"simulation": gold_sphere()}, headers=key).json()
    assert est["credits_estimate"] >= 1 and est["queue"] == "gpu-24g" and est["fits_plan"]
    r = c.post("/v1/jobs", json={"simulation": gold_sphere()}, headers=key)
    assert r.status_code == 202, r.text
    jid = r.json()["job_id"]
    assert r.json()["queue"] == "gpu-24g"
    assert c.get(f"/v1/jobs/{jid}", headers=key).json()["status"] == "queued"
    hosted.work()
    job = wait(c, jid, headers=key)
    assert job["status"] == "succeeded", job["error"]
    assert job["usage"]["credits"] > 0 and job["result"]["method"] == "mie"
    usage = c.get("/v1/usage", headers=key).json()
    assert usage["credits_used"] == pytest.approx(PLANS["pro"].monthly_credits + 0.9 + job["usage"]["credits"])
    assert usage["state"] == "overage"
    events = hosted.stripe.named("meter_events.create")
    assert len(events) == 1
    assert events[0]["payload"]["stripe_customer_id"] == "cus_test_1"
    with hosted.ctx.db.session() as s:
        rec = s.execute(select(UsageRecord).where(UsageRecord.job_id == jid)).scalar_one()
        assert events[0]["identifier"] == f"usage_{rec.id}" and rec.stripe_reported_at is not None
        assert rec.overage_credits == pytest.approx(rec.credits)
        assert events[0]["payload"]["value"] == str(rec.billable_units) == str(int(0.9 + rec.credits))
    admin = c.get("/v1/admin/usage", headers={"Authorization": f"Bearer {ADMIN}"}).json()
    assert admin["totals"]["jobs"] == 2 and admin["jobs_by_status"] == {"succeeded": 1}
    # other orgs cannot read it
    assert c.get(f"/v1/jobs/{jid}", headers=token("user-2")).status_code == 404


def test_ws_events_auth_by_subprotocol(hosted):
    c = hosted.client
    key = new_key(hosted)
    jid = c.post("/v1/jobs", json={"simulation": gold_sphere(num=2)}, headers=key).json()["job_id"]
    hosted.work()
    secret = key["Authorization"].split()[1]
    with c.websocket_connect(f"/v1/jobs/{jid}/events", subprotocols=["lightsmith.bearer", secret]) as ws:
        evs = []
        while not evs or evs[-1]["type"] != "end":
            evs.append(ws.receive_json())
    assert evs[-1]["status"] == "succeeded"
    assert evs[-2]["type"] == "result" and evs[-2]["result_kind"] == "cross_sections"
    with c.websocket_connect(f"/v1/jobs/{jid}/events?token={secret}") as ws:  # query-param fallback
        assert ws.receive_json()["type"] == "status"
    with c.websocket_connect(f"/v1/jobs/{jid}/events") as ws:
        with pytest.raises(Exception):
            ws.receive_json()


def test_quota_exceeded(hosted):
    c = hosted.client
    key = new_key(hosted)
    with hosted.ctx.db.session() as s:
        s.add(UsageRecord(org_id=org_of(hosted, key), job_id="b" * 32, credits=PLANS["free"].monthly_credits))
    r = c.post("/v1/jobs", json={"simulation": gold_sphere()}, headers=key)
    assert r.status_code == 402 and r.json()["error"] == "QuotaExceeded"


def test_concurrency_limit(hosted):
    c = hosted.client
    key = new_key(hosted)
    assert c.post("/v1/jobs", json={"simulation": gold_sphere()}, headers=key).status_code == 202
    r = c.post("/v1/jobs", json={"simulation": gold_sphere()}, headers=key)
    assert r.status_code == 429 and r.json()["error"] == "ConcurrencyLimit"


def test_plan_memory_limit(hosted):
    sim = gold_sphere(num=2, options={"dda": {"dipoles_per_diameter": 150}})
    r = hosted.client.post("/v1/jobs", json={"simulation": sim, "method": "dda"}, headers=new_key(hosted))
    assert r.status_code == 422 and r.json()["error"] == "TooLargeForPlan", r.text


def test_rate_limit(hosted, monkeypatch):
    monkeypatch.setitem(PLANS, "free", dataclasses.replace(PLANS["free"], rate_limit_per_minute=3))
    from lightsmith_cloud import ratelimit

    monkeypatch.setattr(ratelimit, "clock", lambda: 1_800_000_030.0)  # stay inside one window
    key = new_key(hosted)
    codes = [hosted.client.get("/v1/jobs", headers=key).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3] == 429
    r = hosted.client.get("/v1/jobs", headers=key)
    assert r.json()["error"] == "RateLimited" and int(r.headers["retry-after"]) >= 1


def test_admission_rejects_job_too_big_for_gpu(hosted):
    c = hosted.client
    key = new_key(hosted)
    sim = gold_sphere(num=2, options={"dda": {"dipoles_per_diameter": 16}})
    jid = c.post("/v1/jobs", json={"simulation": sim, "method": "dda"}, headers=key).json()["job_id"]
    worker.configure(hosted.ctx, WorkerInfo(gpu_memory_bytes=1024, queues=["gpu-24g"]))
    hosted.work()
    job = wait(c, jid, headers=key)
    assert job["status"] == "failed" and job["error"]["error"] == "AdmissionRejected"
    assert not job["usage"]["credits"]


def test_cancel_queued_job_removes_it(hosted):
    c = hosted.client
    key = new_key(hosted)
    jid = c.post("/v1/jobs", json={"simulation": gold_sphere()}, headers=key).json()["job_id"]
    assert c.delete(f"/v1/jobs/{jid}", headers=key).json()["status"] == "cancelled"
    hosted.work()
    assert c.get(f"/v1/jobs/{jid}", headers=key).json()["status"] == "cancelled"


# --- Stripe -------------------------------------------------------------------


def stripe_event(typ: str, obj: dict, *, livemode=False, eid="evt_test_1") -> bytes:
    """A Stripe event envelope as delivered to webhooks (API shape, test mode)."""
    return json.dumps({"id": eid, "object": "event", "api_version": "2025-09-30.clover", "created": int(time.time()),
                       "livemode": livemode, "type": typ, "data": {"object": obj}, "pending_webhooks": 1,
                       "request": {"id": None, "idempotency_key": None}}).encode()


def sign(payload: bytes, secret: str = WHSEC, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def subscription(org_id: str, status="active", price="price_pro_test") -> dict:
    return {"id": "sub_test_1", "object": "subscription", "customer": "cus_test_1", "status": status,
            "metadata": {"org_id": org_id},
            "items": {"object": "list", "data": [{"id": "si_test_1", "price": {"id": price, "object": "price"}}]}}


def post_hook(h, payload: bytes, sig: str):
    return h.client.post("/v1/billing/webhook", content=payload,
                         headers={"stripe-signature": sig, "content-type": "application/json"})


def test_webhook_updates_plan(hosted):
    org = org_of(hosted, token())
    payload = stripe_event("customer.subscription.updated", subscription(org))
    r = post_hook(hosted, payload, sign(payload))
    assert r.status_code == 200 and r.json()["outcome"] == "applied", r.text
    me = hosted.client.get("/v1/me", headers=token()).json()
    assert me["org"]["plan"] == "pro" and me["plan"]["name"] == "pro"
    assert post_hook(hosted, payload, sign(payload)).json()["outcome"] == "duplicate"

    past_due = stripe_event("customer.subscription.updated", subscription(org, status="past_due"), eid="evt_test_2")
    post_hook(hosted, past_due, sign(past_due))
    assert hosted.client.get("/v1/me", headers=token()).json()["plan"]["name"] == "free"

    gone = stripe_event("customer.subscription.deleted", subscription(org, status="canceled"), eid="evt_test_3")
    post_hook(hosted, gone, sign(gone))
    assert hosted.client.get("/v1/me", headers=token()).json()["org"]["plan"] == "free"


def test_webhook_rejects_bad_signatures_and_live_events(hosted):
    payload = stripe_event("customer.subscription.updated", subscription("x" * 32))
    for sig in (sign(payload, secret="whsec_wrong"), "t=1,v1=deadbeef", "", sign(payload, ts=int(time.time()) - 3600)):
        r = post_hook(hosted, payload, sig)
        assert r.status_code == 400 and r.json()["error"] == "InvalidSignature"
    tampered = payload.replace(b"price_pro_test", b"price_team_tst")
    assert post_hook(hosted, tampered, sign(payload)).status_code == 400
    live = stripe_event("customer.subscription.updated", subscription("x" * 32), livemode=True)
    r = post_hook(hosted, live, sign(live))
    assert r.status_code == 400 and r.json()["error"] == "LiveModeRefused"


def test_checkout_and_redirect_validation(hosted):
    c = hosted.client
    ok = {"plan": "pro", "success_url": "http://localhost:3000/billing/ok", "cancel_url": "http://localhost:3000/b"}
    r = c.post("/v1/billing/checkout", json=ok, headers=token())
    assert r.status_code == 200 and r.json()["url"].startswith("https://checkout.stripe.com/")
    params = hosted.stripe.named("checkout.create")[0]
    assert params["mode"] == "subscription" and params["customer"] == "cus_test_1"
    assert [i["price"] for i in params["line_items"]] == ["price_pro_test", "price_metered_test"]
    bad = {**ok, "success_url": "https://evil.example.com/phish"}
    assert c.post("/v1/billing/checkout", json=bad, headers=token()).status_code == 422
    assert c.post("/v1/billing/checkout", json={**ok, "success_url": "http://localhost:3000.evil.com/x"},
                  headers=token()).status_code == 422


def test_live_stripe_keys_refused():
    with pytest.raises(ValueError, match="test-mode"):
        Settings(stripe_secret_key="sk_live_123")
    with pytest.raises(ValueError, match="PEPPER"):
        Settings(mode="hosted", allow_sqlite=True)
    with pytest.raises(ValueError, match="Postgres"):
        Settings(mode="hosted", api_key_pepper="x")
    Settings(mode="hosted", api_key_pepper="x", database_url="postgresql+psycopg://u:p@db/x")


def test_identity_is_ids_only(hosted):
    """Profiles live in the platform: only an org row (plan/billing state) is created, once."""
    for _ in range(3):
        me = hosted.client.get("/v1/me", headers=token("same-user", name="Grace")).json()
    assert me["org"]["id"] == "user:same-user" and me["user_id"] == "same-user"
    assert "Grace" not in json.dumps(me)
    from sqlalchemy import inspect

    assert not {"users", "memberships"} & set(inspect(hosted.ctx.db.engine).get_table_names())
    with hosted.ctx.db.session() as s:
        assert s.get(Org, "user:same-user") is not None
        assert set(c.name for c in Org.__table__.columns) == {"id", "plan", "stripe_customer_id",
                                                               "stripe_subscription_id", "subscription_status",
                                                               "current_period_end", "cancel_at_period_end",
                                                               "billing_event_at", "credit_limit",
                                                               "credit_cap_override", "created_at"}


def test_org_claims_and_header(hosted):
    c = hosted.client
    assert c.get("/v1/me", headers=token(org_id="acme")).json()["org"]["id"] == "acme"
    hdr = {**token(org_id="acme"), "X-Org-Id": "globex"}
    assert c.get("/v1/me", headers=hdr).status_code == 403
    hdr = {**token(org_id="acme", orgs=["acme", "globex"]), "X-Org-Id": "globex"}
    assert c.get("/v1/me", headers=hdr).json()["org"]["id"] == "globex"


def test_x_api_key_header(hosted):
    key = new_key(hosted)["Authorization"].split()[1]
    assert hosted.client.get("/v1/me", headers={"X-API-Key": key}).json()["kind"] == "api_key"
