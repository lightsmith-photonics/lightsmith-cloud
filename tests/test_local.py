"""Local mode end to end: real solver subprocesses, SQLite, filesystem results."""

from __future__ import annotations

import json

import numpy as np
import lightsmith as osc
import pytest
from fastapi.testclient import TestClient

from conftest import gold_sphere, make_local_app, wait
from lightsmith_cloud.config import Settings
from lightsmith_cloud.sandbox import EstimateError


def test_info_endpoints(local_client):
    assert local_client.get("/v1/health").json()["mode"] == "local"
    assert local_client.get("/v1/schema?kind=scattering").json() == osc.json_schema()
    names = {b["name"] for b in local_client.get("/v1/backends").json()}
    assert {"miepython", "lightsmith-dda"} <= names
    assert "numpy" in local_client.get("/v1/doctor").json()


def test_estimate(local_client):
    r = local_client.post("/v1/estimate", json={"simulation": gold_sphere(), "method": "dda", "device": "cpu"})
    assert r.status_code == 200, r.text
    est = r.json()
    assert est["method"] == "dda" and est["memory_bytes"] > 0 and est["dipoles"] > 0
    assert "credits_estimate" not in est  # hosted only


def test_mie_job_end_to_end(local_client):
    sim = gold_sphere()
    r = local_client.post("/v1/jobs", json={"simulation": sim, "label": "mie test"})
    assert r.status_code == 202, r.text
    job = wait(local_client, r.json()["job_id"])
    assert job["status"] == "succeeded", job["error"]
    assert job["progress"] == 1.0 and job["error"] is None
    ref = osc.solve(sim, "mie")
    np.testing.assert_allclose(job["result"]["c_ext_nm2"], ref.c_ext_nm2, rtol=1e-12)
    assert job["result"]["method"] == "mie"
    assert job["usage"]["runtime_s"] > 0 and job["usage"]["credits"] is None
    raw = local_client.get(job["result_url"])
    assert raw.status_code == 200 and raw.json()["c_ext_nm2"] == job["result"]["c_ext_nm2"]
    listed = local_client.get("/v1/jobs?mine=1").json()["jobs"]
    assert any(j["job_id"] == job["job_id"] and j["result"] is None for j in listed)


def glass_sphere(**extra) -> dict:
    return {"particles": [{"shape": {"kind": "sphere", "radius_nm": 50},
                           "material": {"kind": "constant_index", "n_real": 1.5, "n_imag": 0.02}}],
            "medium_index": 1.33, "wavelengths": {"start_nm": 450, "stop_nm": 700, "num": 3},
            "options": {"dda": {"dipoles_per_diameter": 12}}, **extra}


def test_dda_job_matches_library_and_mie(local_client):
    sim = glass_sphere()
    r = local_client.post("/v1/jobs", json={"simulation": sim, "method": "dda", "device": "cpu"})
    job = wait(local_client, r.json()["job_id"])
    assert job["status"] == "succeeded", job["error"]
    assert job["result"]["method"] == "dda" and job["device_used"]["device"] == "cpu"
    # The service returns exactly what the library computes in-process...
    direct = osc.solve(sim, "dda", "cpu")
    np.testing.assert_allclose(job["result"]["c_ext_nm2"], direct.c_ext_nm2, rtol=1e-9)
    # ...and DDA on a weakly absorbing dielectric sphere agrees with Mie (measured: 0.13% at 12 dipoles/diameter).
    np.testing.assert_allclose(job["result"]["c_ext_nm2"], osc.solve(sim, "mie").c_ext_nm2, rtol=0.02)


def test_compare_job_reports_per_method_progress(local_client):
    r = local_client.post("/v1/jobs", json={"simulation": glass_sphere(), "kind": "compare",
                                            "methods": ["mie", "dda"], "device": "cpu"})
    assert r.status_code == 202, r.text
    jid = r.json()["job_id"]
    job = wait(local_client, jid)
    assert job["status"] == "succeeded", job["error"]
    res = job["result"]
    assert res["reference"] == "mie" and set(res["results"]) == {"mie", "dda"}
    assert res["relative_difference"]["dda"]["c_ext_nm2"]["max"] < 0.02
    with local_client.websocket_connect(f"/v1/jobs/{jid}/events") as ws:
        events = []
        while True:
            ev = ws.receive_json()
            events.append(ev)
            if ev["type"] == "end":
                break
    done = [e["method"] for e in events if e.get("stage") == "method_done"]
    assert done == ["mie", "dda"]
    statuses = [e["status"] for e in events if e["type"] == "status"]
    assert statuses == ["queued", "running", "succeeded"] and events[-1] == {"type": "end", "status": "succeeded"}
    seqs = [e["seq"] for e in events if "seq" in e]
    assert seqs == sorted(seqs)


def test_events_replay_after(local_client):
    r = local_client.post("/v1/jobs", json={"simulation": gold_sphere(num=2)})
    jid = r.json()["job_id"]
    wait(local_client, jid)
    with local_client.websocket_connect(f"/v1/jobs/{jid}/events?after=2") as ws:
        first = ws.receive_json()
    assert first["seq"] == 3


def test_cancel_running_job(local_client):
    sim = gold_sphere(num=200, options={"dda": {"dipoles_per_diameter": 40}})
    jid = local_client.post("/v1/jobs", json={"simulation": sim, "method": "dda", "device": "cpu"}).json()["job_id"]
    for _ in range(300):
        if local_client.get(f"/v1/jobs/{jid}").json()["status"] == "running":
            break
    r = local_client.delete(f"/v1/jobs/{jid}")
    assert r.status_code == 202
    job = wait(local_client, jid, timeout=60)
    assert job["status"] == "cancelled" and job["error"]["error"] == "Cancelled"
    assert local_client.delete(f"/v1/jobs/{jid}").json()["status"] == "cancelled"  # idempotent


def test_timeout_kills_job(tmp_path, estimator):
    app = make_local_app(tmp_path, estimator, job_timeout_s=3)
    sim = gold_sphere(num=200, options={"dda": {"dipoles_per_diameter": 40}})
    with TestClient(app) as c:
        jid = c.post("/v1/jobs", json={"simulation": sim, "method": "dda", "device": "cpu"}).json()["job_id"]
        job = wait(c, jid, timeout=90)
    assert job["status"] == "failed" and job["error"]["error"] == "Timeout"
    assert job["usage"]["runtime_s"] < 30


def test_memory_cap_kills_job(tmp_path, estimator):
    app = make_local_app(tmp_path, estimator, job_memory_bytes=200 * 2**20)
    sim = gold_sphere(num=50, options={"dda": {"dipoles_per_diameter": 48}})
    with TestClient(app) as c:
        jid = c.post("/v1/jobs", json={"simulation": sim, "method": "dda", "device": "cpu"}).json()["job_id"]
        job = wait(c, jid, timeout=120)
    assert job["status"] == "failed"
    assert job["error"]["error"] in ("MemoryLimitExceeded", "MemoryError"), job["error"]


def test_solver_error_is_reported(local_client):
    # Tabulated data that does not cover the wavelengths: the library raises in the child.
    sim = gold_sphere()
    sim["particles"][0]["material"] = {"kind": "tabulated", "wavelength_nm": [500, 600],
                                       "n_real": [0.5, 0.3], "n_imag": [2.0, 3.0]}
    job = wait(local_client, local_client.post("/v1/jobs", json={"simulation": sim}).json()["job_id"])
    assert job["status"] == "failed"
    assert job["error"]["error"] == "MaterialRangeError" and "tabulated range" in job["error"]["message"]


def test_job_survives_restart_as_failed(tmp_path, estimator):
    from lightsmith_cloud.db import Job

    app = make_local_app(tmp_path, estimator)
    ctx = app.state.ctx
    with ctx.db.session() as s:
        s.add(Job(id="a" * 32, org_id="local", kind="solve", status="running", spec=gold_sphere(), device="cpu"))
    with TestClient(app) as c:
        j = c.get("/v1/jobs/" + "a" * 32).json()
    assert j["status"] == "failed" and j["error"]["error"] == "ServerShutdown"


def _cuda() -> bool:
    try:
        import torch

        return torch.cuda.is_available()
    except ImportError:
        return False


@pytest.mark.gpu
@pytest.mark.skipif(not _cuda(), reason="needs a CUDA GPU")
def test_dda_job_on_gpu(local_client):
    sim = glass_sphere()
    job = wait(local_client, local_client.post("/v1/jobs", json={"simulation": sim, "method": "dda",
                                                                 "device": "gpu"}).json()["job_id"])
    assert job["status"] == "succeeded", job["error"]
    assert job["device_used"]["device"].startswith("cuda") and job["device_used"]["gpu_name"]
    # single precision on the GPU
    np.testing.assert_allclose(job["result"]["c_ext_nm2"], osc.solve(sim, "mie").c_ext_nm2, rtol=0.02)


def test_layer_stack_problem_kind(local_client):
    """Any lightsmith problem kind runs through the same job API (here a quarter-wave film, TMM)."""
    stack = {"kind": "layer_stack", "superstrate": {"kind": "constant_index", "n_real": 1.0},
             "layers": [{"thickness_nm": 100.0, "material": {"kind": "constant_index", "n_real": 1.38}}],
             "substrate": {"kind": "constant_index", "n_real": 1.52},
             "wavelengths": [500.0, 552.0, 600.0]}
    r = local_client.post("/v1/jobs", json={"simulation": stack})
    assert r.status_code == 202, r.text
    job = wait(local_client, r.json()["job_id"])
    assert job["status"] == "succeeded", job["error"]
    direct = osc.solve(stack).model_dump(mode="json")
    assert job["result"] == direct | {"provenance": job["result"]["provenance"]}
    assert local_client.post("/v1/jobs", json={"simulation": stack, "method": "dda"}).json()["error"] \
        == "UnsupportedProblem"
    assert job["result_kind"] == "spectrum" and job["problem_kind"] == "layer_stack"
    support = local_client.post("/v1/supports", json={"spec": stack}).json()
    assert {b["name"] for b in support if b["supported"]} >= {"lightsmith-tmm"}
    assert all(b["name"] != "lightsmith-dda" for b in support)


def emission_problem(**extra) -> dict:
    return {"kind": "emission", "medium_index": 1.33, "wavelengths": [500.0, 600.0],
            "particles": [{"shape": {"kind": "sphere", "radius_nm": 40},
                           "material": {"kind": "constant_index", "n_real": 2.0}}],
            "dipole": {"position_nm": [0, 0, 60], "orientation": [0, 0, 1]}, **extra}


def test_emission_problem_kind(local_client):
    job = wait(local_client, local_client.post("/v1/jobs", json={"simulation": emission_problem()}).json()["job_id"])
    assert job["status"] == "succeeded", job["error"]
    assert job["problem_kind"] == "emission" and job["result_kind"] == "emission"
    assert len(job["result"]["purcell"]) == 2 and min(job["result"]["purcell"]) > 0
    too_many = emission_problem(radiation_pattern={"theta_deg": [0.0] * 2000, "phi_deg": [0.0] * 2000,
                                                   "wavelengths_nm": [500.0]})
    r = local_client.post("/v1/jobs", json={"simulation": too_many})
    assert r.status_code == 422 and r.json()["error"] == "SpecTooLarge", r.text
    crowded = emission_problem()
    crowded["particles"] = crowded["particles"] * 1001
    r = local_client.post("/v1/jobs", json={"simulation": crowded})
    assert r.status_code == 422 and r.json()["error"] == "SpecTooLarge" and "particles" in r.json()["message"]


def test_schema_is_problem_envelope(local_client):
    from lightsmith_cloud.problem import problem_json_schema

    assert local_client.get("/v1/schema").json() == problem_json_schema()
    assert local_client.get("/v1/schema?kind=scattering").json() == osc.json_schema()
    assert all("options_schema" in b for b in local_client.get("/v1/backends").json())


def test_studio_aliases_and_estimate_job(local_client):
    """Studio sends spec/operation and reads id/operation/message; estimate also runs as a job."""
    r = local_client.post("/v1/jobs", json={"spec": gold_sphere(), "operation": "estimate", "method": "dda",
                                            "device": "cpu"})
    assert r.status_code == 202, r.text
    assert r.json()["id"] == r.json()["job_id"]
    job = wait(local_client, r.json()["id"])
    assert job["status"] == "succeeded" and job["operation"] == "estimate" and job["result_kind"] == "estimate"
    assert job["result"]["method"] == "dda" and job["result"]["memory_bytes"] > 0 and job["message"] == "succeeded"
    est = local_client.post("/v1/estimate", json={"spec": gold_sphere(), "method": "dda", "device": "cpu"}).json()
    assert est["estimated_runtime_s"] > 0


def bend_optimization(max_iter=3) -> dict:
    """The small 2D waveguide-bend topology optimisation from the library's own tests."""
    from lightsmith import inverse
    from lightsmith.problems import DeviceProblem, Port, Prism, Rectangle, Structure

    si, sio2, half = osc.ConstantIndex(n_real=3.48), osc.ConstantIndex(n_real=1.44), 600.0

    def wg(cx, cy, sx, sy):
        return Structure(geometry=Prism(profile=Rectangle(center_nm=(cx, cy), size_nm=(sx, sy)), z_min_nm=-110,
                                        z_max_nm=110), material=si)

    dev = DeviceProblem(
        dimension=2, background=sio2, structures=[wg(-5000 - half, 0, 10000, 400), wg(0, 5000 + half, 400, 10000)],
        domain_size_nm=(2 * half + 1400, 2 * half + 1400, 0), wavelengths=[1550.0],
        ports=[Port(name="o1", center_nm=(-half - 450, 0, 0), size_nm=(0, 1500, 1), direction="+"),
               Port(name="o2", center_nm=(0, half + 450, 0), size_nm=(1500, 0, 1), direction="-")],
        options={"fdfd": {"dl_nm": 40.0, "pml_cells": 10}})
    prob = inverse.OptimizationProblem(
        device=dev,
        design_regions=[inverse.DesignRegion(size_nm=(2 * half, 2 * half, 220), material_low=sio2, material_high=si,
                                             filter_radius_nm=80)],
        objective=[inverse.SParameterTerm(out_port="o2", in_port="o1")],
        optimizer=inverse.OptimizerSettings(method="mma", max_iter=max_iter, beta_every=1000))
    return prob.model_dump(mode="json")


def test_optimize_job_streams_iterations(local_client):
    pytest.importorskip("torch")
    spec = bend_optimization(max_iter=3)
    assert local_client.post("/v1/jobs", json={"simulation": gold_sphere(), "kind": "optimize"}).status_code == 422
    r = local_client.post("/v1/jobs", json={"simulation": spec, "kind": "optimize", "device": "cpu"})
    assert r.status_code == 202, r.text
    jid = r.json()["job_id"]
    job = wait(local_client, jid, timeout=600)
    assert job["status"] == "succeeded", job["error"]
    assert job["problem_kind"] == "optimization" and job["result_kind"] == job["result"].get("kind")
    with local_client.websocket_connect(f"/v1/jobs/{jid}/events") as ws:
        events = []
        while not events or events[-1]["type"] != "end":
            events.append(ws.receive_json())
    its = [e for e in events if e.get("stage") == "iteration"]
    assert [e["iteration"] for e in its][0] == 0 and its[-1]["iteration"] == 2 and its[-1]["max_iter"] == 3
    assert all(isinstance(e["objective"], float) for e in its)
    assert [e["progress"] for e in its] == sorted(e["progress"] for e in its)


def test_cancel_post_alias(local_client):
    jid = local_client.post("/v1/jobs", json={"simulation": glass_sphere(), "method": "dda"}).json()["job_id"]
    assert local_client.post(f"/v1/jobs/{jid}/cancel").status_code == 202
    assert wait(local_client, jid)["status"] in ("cancelled", "succeeded")


def test_local_database_is_migrated(local_client):
    from lightsmith_cloud import migrate

    assert migrate.current_revision(local_client.app.state.ctx.db.engine) == migrate.head_revision()


def test_hosted_refuses_unmigrated_database(tmp_path):
    from lightsmith_cloud.context import build_context

    s = Settings(mode="hosted", data_dir=tmp_path, allow_sqlite=True, api_key_pepper="x")
    with pytest.raises(RuntimeError, match="lightsmith-server migrate"):
        build_context(s, with_estimator=False)


# --- discretization and sweeps ------------------------------------------------------------------------------------


def test_discretization_shows_what_the_backend_discretises(local_client):
    r = local_client.post("/v1/discretization", json={"simulation": gold_sphere(), "method": "dda"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["backend"] == "lightsmith-dda" and out["layout_kind"] == "dipole_lattice"
    assert out["payload"]["n_dipoles"] == out["estimate"]["dipoles"] > 0
    assert out["summary"]["dipoles"] == out["payload"]["n_dipoles"]
    r = local_client.post("/v1/discretization", json={"simulation": gold_sphere(), "method": "rcwa"})
    assert r.status_code == 422 and r.json()["error"] == "UnsupportedProblem", r.text
    r = local_client.post("/v1/discretization", json={"simulation": gold_sphere(), "method": "nope"})
    assert r.status_code == 422 and r.json()["error"] == "UnknownMethod", r.text


def test_discretization_on_a_library_without_it_is_501(local_client, monkeypatch):
    def no_support(*a, **k):
        raise EstimateError("NotImplementedError", "this lightsmith has no discretization(); upgrade the library")

    monkeypatch.setattr(local_client.app.state.ctx.estimator, "discretization", no_support)
    r = local_client.post("/v1/discretization", json={"simulation": gold_sphere(), "method": "mie"})
    assert r.status_code == 501 and r.json() == {
        "error": "NotImplemented", "message": "this lightsmith has no discretization(); upgrade the library"}


def test_sweep_job_solves_each_value(local_client):
    body = {"simulation": gold_sphere(num=5), "kind": "sweep", "method": "mie",
            "sweep": {"path": ["particles", 0, "shape", "radius_nm"], "values": [20, 30, 40]}}
    r = local_client.post("/v1/jobs", json=body)
    assert r.status_code == 202, r.text
    job = wait(local_client, r.json()["job_id"])
    assert job["status"] == "succeeded", job
    assert job["kind"] == "sweep" and job["sweep"] == {"path": ["particles", 0, "shape", "radius_nm"], "values": [20.0, 30.0, 40.0]}
    res = job["result"]
    assert res["kind"] == "sweep" and res["values"] == [20, 30, 40] and not res["errors"]
    cext = [max(x["c_ext_nm2"]) for x in res["results"]]
    assert cext[0] < cext[1] < cext[2]  # bigger spheres extinguish more


def test_sweep_of_an_option_creates_it_and_bad_values_are_refused(local_client):
    body = {"simulation": gold_sphere(num=3), "kind": "sweep", "method": "dda",
            "sweep": {"path": ["options", "dda", "dipoles_per_diameter"], "values": [8, 10]}}
    job = wait(local_client, local_client.post("/v1/jobs", json=body).json()["job_id"])
    assert job["status"] == "succeeded", job
    a, b = job["result"]["results"]
    assert a["c_ext_nm2"] != b["c_ext_nm2"]  # a finer dipole lattice: a (slightly) different answer
    bad = {**body, "sweep": {"path": ["particles", 0, "shape", "radius_nm"], "values": [10, -5]}}
    r = local_client.post("/v1/jobs", json=bad)
    assert r.status_code == 422 and "sweep value -5" in r.json()["message"]
    assert local_client.post("/v1/jobs", json={**body, "sweep": None}).status_code == 422
