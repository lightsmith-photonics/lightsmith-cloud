"""A Stripe HTTP transport for tests: the real `stripe` library (request encoding, response parsing,
errors) talks to handlers that answer with the fixtures recorded from stripe-mock in
fixtures/stripe/ (see record.py there). Nothing touches the network."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

import stripe

FIXTURES = Path(__file__).parent / "fixtures" / "stripe"
MONTH = 30 * 86400


def fixture(name: str, /, **over: Any) -> dict:
    d = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    d.update(over)
    return d


def not_found(what: str) -> tuple[int, dict]:
    return 404, {"error": {"type": "invalid_request_error", "code": "resource_missing", "message": f"No such {what}"}}


class FakeStripeHTTP(stripe.HTTPClient):
    name = "fake"

    def __init__(self):
        super().__init__()
        self.requests: list[SimpleNamespace] = []
        self.routes: list[tuple[str, re.Pattern, Callable]] = []
        self.fail: dict[str, int] = {}  # path -> number of upcoming requests that get a 500

    def on(self, method: str, pattern: str, handler: Callable) -> None:
        """handler(params, *groups) -> body, or (status, body). Later routes win."""
        self.routes.insert(0, (method, re.compile(pattern), handler))

    def client(self) -> stripe.StripeClient:
        return stripe.StripeClient("sk_test_fixture", http_client=self, max_network_retries=0)

    def request(self, method, url, headers, post_data=None, *, _usage=None):
        u = urlsplit(url)
        raw = post_data.decode() if isinstance(post_data, bytes) else (post_data or u.query)
        params = dict(parse_qsl(raw or "", keep_blank_values=True))
        self.requests.append(SimpleNamespace(method=method.upper(), path=u.path, params=params, headers=headers))
        if self.fail.get(u.path):
            self.fail[u.path] -= 1
            return json.dumps({"error": {"type": "api_error", "message": "fixture outage"}}), 500, {}
        for m, rx, handler in self.routes:
            match = rx.fullmatch(u.path)
            if m == method.upper() and match:
                out = handler(params, *match.groups())
                status, body = out if isinstance(out, tuple) else (200, out)
                return json.dumps(body), status, {"request-id": "req_fixture"}
        return json.dumps(not_found(f"route {method} {u.path}")[1]), 404, {}

    def request_stream(self, *a, **kw):
        raise NotImplementedError

    def close(self):
        pass

    def sent(self, method: str, path: str) -> list[dict]:
        return [r.params for r in self.requests if r.method == method and re.fullmatch(path, r.path)]


def listing(url: str, items: list) -> dict:
    return {"object": "list", "url": url, "has_more": False, "data": items}


def subscription(sub_id: str, customer: str, org_id: str, prices: list[tuple[str, bool]], *, status: str = "active",
                 period_end: int, cancel_at_period_end: bool = False) -> dict:
    """A subscription in the current API shape (period on the items), from the recorded fixture."""
    d = fixture("subscription")
    template = d["items"]["data"][0]
    items = []
    for i, (price, metered) in enumerate(prices, start=1):
        it = copy.deepcopy(template)
        it.update(id=f"si_{sub_id}_{i}", subscription=sub_id, current_period_start=period_end - MONTH,
                  current_period_end=period_end, quantity=None if metered else 1)
        it["price"].update(id=price, recurring={**it["price"]["recurring"],
                                                "usage_type": "metered" if metered else "licensed"})
        items.append(it)
    d.update(id=sub_id, customer=customer, status=status, metadata={"org_id": org_id}, livemode=False,
             cancel_at_period_end=cancel_at_period_end, cancel_at=period_end if cancel_at_period_end else None)
    d["items"] = {**d["items"], "data": items, "url": f"/v1/subscription_items?subscription={sub_id}"}
    return d


def invoice(customer: str, sub_id: str, status: str) -> dict:
    d = fixture("invoice", customer=customer, status=status, livemode=False)
    d["parent"] = {"type": "subscription_details", "quote_details": None,
                   "subscription_details": {"metadata": {}, "subscription": sub_id}}
    return d
