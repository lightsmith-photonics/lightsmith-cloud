"""Re-record the Stripe response fixtures in this folder from stripe-mock (Stripe's official mock
server, which answers with the fixtures of Stripe's OpenAPI spec and validates every request):

    docker run --rm -d -p 12111:12111 stripe/stripe-mock:latest
    uv run python tests/fixtures/stripe/record.py http://localhost:12111

The tests replay these through the real `stripe` library (see tests/stripe_fake.py), changing only
ids, statuses and prices. No real Stripe account is involved.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import stripe

HERE = Path(__file__).parent


class Recorder(stripe.HTTPClient):
    name = "recorder"

    def __init__(self):
        super().__init__()
        self.inner = stripe.new_default_http_client()
        self.last = None

    def request(self, method, url, headers, post_data=None, *, _usage=None):
        body, code, hdrs = self.inner.request(method, url, headers, post_data)
        self.last = json.loads(body)
        return body, code, hdrs

    def request_stream(self, *a, **kw):
        raise NotImplementedError

    def close(self):
        self.inner.close()


def main(base: str) -> None:
    rec = Recorder()
    c = stripe.StripeClient("sk_test_record", base_addresses={"api": base}, http_client=rec)
    calls = {
        "customer": lambda: c.v1.customers.create(params={"metadata": {"org_id": "x"}}),
        "checkout_session": lambda: c.v1.checkout.sessions.create(params={
            "mode": "subscription", "customer": "cus_x", "line_items": [{"price": "price_x", "quantity": 1}],
            "success_url": "http://localhost:3000/ok", "cancel_url": "http://localhost:3000/no"}),
        "portal_session": lambda: c.v1.billing_portal.sessions.create(params={"customer": "cus_x"}),
        "subscription": lambda: c.v1.subscriptions.retrieve("sub_x"),
        "invoice": lambda: c.v1.invoices.retrieve("in_x"),
        "meter_event": lambda: c.v1.billing.meter_events.create(params={
            "event_name": "lightsmith_credits", "payload": {"stripe_customer_id": "cus_x", "value": "1"}}),
        "meter": lambda: c.v1.billing.meters.create(params={
            "display_name": "x", "event_name": "lightsmith_credits", "default_aggregation": {"formula": "sum"}}),
        "product": lambda: c.v1.products.create(params={"name": "x"}),
        "price": lambda: c.v1.prices.create(params={"currency": "usd", "unit_amount": 1, "product": "prod_x"}),
        "portal_configuration": lambda: c.v1.billing_portal.configurations.create(params={
            "features": {"invoice_history": {"enabled": True}}}),
    }
    for name, call in calls.items():
        call()
        (HERE / f"{name}.json").write_text(json.dumps(rec.last, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print("recorded", name)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:12111")
