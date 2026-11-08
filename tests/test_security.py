"""Specs are data: validation, size limits, no file access, sandbox environment."""

from __future__ import annotations

import os

import pytest

from conftest import gold_sphere
from lightsmith_cloud.sandbox import child_env
from lightsmith_cloud.storage import LocalStore, result_key


def _post(c, sim, **kw):
    return c.post("/v1/jobs", json={"simulation": sim, **kw})


def test_errors_are_json(local_client):
    r = _post(local_client, {"particles": []})
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "ValidationError" and body["message"] and body["details"]


@pytest.mark.parametrize("extra", [{"file": "/etc/passwd"}, {"__class__": "os.system"}])
def test_unknown_fields_rejected(local_client, extra):
    r = _post(local_client, gold_sphere(**extra))
    assert r.status_code == 422 and r.json()["error"] == "ValidationError"


def test_material_cannot_reference_files(local_client):
    sim = gold_sphere()
    sim["particles"][0]["material"] = {"kind": "library", "name": "../../../etc/passwd"}
    r = _post(local_client, sim)
    assert r.status_code == 422 and "unknown library material" in r.json()["message"]
    sim["particles"][0]["material"] = {"kind": "tabulated", "path": "C:/Windows/win.ini"}
    assert _post(local_client, sim).status_code == 422


def test_size_limits(local_client):
    sim = gold_sphere()
    sim["wavelengths"] = {"start_nm": 400, "stop_nm": 800, "num": 10_000_000}
    r = _post(local_client, sim)
    assert r.status_code == 422 and r.json()["error"] == "SpecTooLarge"

    far = gold_sphere(options={"dda": {"dipoles_per_diameter": 100_000}})
    assert _post(local_client, far, method="dda").json()["error"] == "SpecTooLarge"

    # Two small spheres a millimetre apart: the DDA lattice would be astronomically large.
    spread = gold_sphere()
    p = spread["particles"][0]
    spread["particles"] = [p, {**p, "position_nm": [1e6, 0, 0]}]
    r = local_client.post("/v1/estimate", json={"simulation": spread, "method": "dda"})
    assert r.status_code == 422 and r.json()["error"] == "SpecTooLarge"


def test_body_limit(local_client):
    r = local_client.post("/v1/jobs", content=b"{" + b" " * 3_000_000 + b"}",
                          headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["error"] == "PayloadTooLarge"


def test_bad_device_and_method(local_client):
    assert _post(local_client, gold_sphere(), device="cuda:0; rm -rf /").json()["error"] == "ValidationError"
    assert _post(local_client, gold_sphere(), method="nope").json()["error"] == "UnknownMethod"
    # estimate answers with the same error names as jobs, not the library's exception types
    r = local_client.post("/v1/estimate", json={"simulation": gold_sphere(), "method": "nope"})
    assert r.status_code == 422 and r.json()["error"] == "UnknownMethod", r.text
    pair = gold_sphere()
    pair["particles"] = pair["particles"] * 2
    r = local_client.post("/v1/estimate", json={"simulation": pair, "method": "mie"})
    assert r.status_code == 422 and r.json()["error"] == "UnsupportedProblem", r.text
    r = _post(local_client, gold_sphere(substrate=None), method="mie")
    assert r.status_code == 202
    two = gold_sphere()
    two["particles"] = two["particles"] * 2
    assert _post(local_client, two, method="mie").json()["error"] == "UnsupportedProblem"


@pytest.mark.parametrize("path", ["/v1/jobs/..%2F..%2Fcompute.db", "/v1/jobs/" + "z" * 32,
                                  "/v1/jobs/" + "0" * 32 + "/result"])
def test_job_ids_are_not_paths(local_client, path):
    r = local_client.get(path)
    assert r.status_code == 404 and r.json()["error"] == "NotFound"


def test_store_rejects_bad_keys(tmp_path):
    store = LocalStore(tmp_path)
    for key in ("../x.json.gz", "a/b.json.gz", "x" * 32 + ".json.gz"):
        with pytest.raises(ValueError):
            store.get(key)
    k = result_key("f" * 32)
    store.put(k, b'{"a": 1}')
    assert store.get(k) == b'{"a": 1}'


def test_dns_rebinding_blocked(local_client):
    r = local_client.get("/v1/health", headers={"host": "evil.example.com"})
    assert r.status_code == 400


def test_cors_allows_studio_and_web(local_client):
    for origin in ("http://localhost:5173", "http://localhost:3000"):
        r = local_client.options("/v1/jobs", headers={"origin": origin, "access-control-request-method": "POST"})
        assert r.headers.get("access-control-allow-origin") == origin
    r = local_client.options("/v1/jobs", headers={"origin": "https://evil.example.com",
                                                  "access-control-request-method": "POST"})
    assert "access-control-allow-origin" not in r.headers


def test_child_env_drops_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("PLATFORM_JWT_SECRET", "x")
    env = child_env(tmp_path)
    assert not {"STRIPE_SECRET_KEY", "DATABASE_URL", "PLATFORM_JWT_SECRET"} & set(env)
    assert env["HOME"] == str(tmp_path) and env["PATH"] == os.environ["PATH"]


def test_s3_store_keys_and_bucket_creation():
    from lightsmith_cloud.storage import S3Store

    class FakeS3:
        def __init__(self):
            self.buckets, self.objects = set(), {}

        def head_bucket(self, Bucket):
            if Bucket not in self.buckets:
                raise RuntimeError("404")

        def create_bucket(self, Bucket):
            self.buckets.add(Bucket)

        def put_object(self, Bucket, Key, Body, **kw):
            self.objects[(Bucket, Key)] = Body

        def get_object(self, Bucket, Key):
            from io import BytesIO
            return {"Body": BytesIO(self.objects[(Bucket, Key)])}

    fake = FakeS3()
    store = S3Store("results", client=fake, create_bucket=True)
    assert fake.buckets == {"results"}
    k = result_key("e" * 32)
    store.put(k, b'{"x": 1}')
    assert ("results", "results/" + k) in fake.objects and store.get(k) == b'{"x": 1}'
    with pytest.raises(ValueError):
        store.get("../" + k)
