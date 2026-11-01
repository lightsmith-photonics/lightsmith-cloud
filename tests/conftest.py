from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lightsmith_cloud.app import create_app
from lightsmith_cloud.config import Settings
from lightsmith_cloud.sandbox import Estimator

TERMINAL = ("succeeded", "failed", "cancelled")


def gold_sphere(radius=40.0, num=11, **extra) -> dict:
    return {"particles": [{"shape": {"kind": "sphere", "radius_nm": radius},
                           "material": {"kind": "drude", "eps_inf": 9.5, "wp_ev": 9.0, "gamma_ev": 0.07}}],
            "medium_index": 1.33, "wavelengths": {"start_nm": 450, "stop_nm": 700, "num": num}, **extra}


def wait(client: TestClient, job_id: str, timeout: float = 240.0, headers=None) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
        if j["status"] in TERMINAL:
            return j
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} did not finish: {j}")


@pytest.fixture(scope="session")
def estimator(tmp_path_factory) -> Estimator:
    est = Estimator(tmp_path_factory.mktemp("estimator"), timeout_s=120, memory_bytes=4 * 2**30)
    yield est
    est.close()


@pytest.fixture(scope="module")
def local_client(tmp_path_factory, estimator):
    s = Settings(mode="local", data_dir=tmp_path_factory.mktemp("local"), local_workers=2)
    app = create_app(s)
    app.state.ctx.estimator.close()
    app.state.ctx.estimator = estimator
    with TestClient(app) as c:
        yield c
    app.state.ctx.db.dispose()


def make_local_app(tmp: Path, estimator: Estimator, **kw):
    app = create_app(Settings(mode="local", data_dir=tmp, **kw))
    app.state.ctx.estimator.close()
    app.state.ctx.estimator = estimator
    return app
