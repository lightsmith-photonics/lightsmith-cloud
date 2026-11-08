"""The estimate worker: a slow start is not the problem's fault; a slow estimate is reported as such."""

import sys

import pytest

from lightsmith_cloud import sandbox
from lightsmith_cloud.sandbox import EstimateError, Estimator

WORKER = """
import json, sys, time
time.sleep({start_s})
print(json.dumps({{"ready": True}}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    time.sleep({work_s})
    print(json.dumps({{"id": req["id"], "ok": True, "result": {{"answer": 42}}}}), flush=True)
"""


def worker(tmp_path, monkeypatch, start_s=0.0, work_s=0.0):
    script = tmp_path / "worker.py"
    script.write_text(WORKER.format(start_s=start_s, work_s=work_s), encoding="utf-8")
    monkeypatch.setattr(sandbox, "_command", lambda *a: [sys.executable, str(script)])


def test_slow_start_does_not_count_against_the_request(tmp_path, monkeypatch):
    worker(tmp_path, monkeypatch, start_s=1.5)
    est = Estimator(tmp_path / "w", timeout_s=0.5, startup_timeout_s=60)
    try:
        assert est.estimate({}, "auto", "auto") == {"answer": 42}
        assert est.estimate({}, "auto", "auto") == {"answer": 42}  # and the running worker is reused
    finally:
        est.close()


def test_worker_that_never_starts_says_so(tmp_path, monkeypatch):
    worker(tmp_path, monkeypatch, start_s=60)
    est = Estimator(tmp_path / "w", timeout_s=60, startup_timeout_s=0.5)
    try:
        with pytest.raises(EstimateError) as e:
            est.estimate({}, "auto", "auto")
        assert e.value.error == "Timeout" and "did not start" in e.value.message
    finally:
        est.close()


def test_slow_estimate_is_killed_and_reported(tmp_path, monkeypatch):
    worker(tmp_path, monkeypatch, work_s=60)
    est = Estimator(tmp_path / "w", timeout_s=0.5, startup_timeout_s=60)
    try:
        with pytest.raises(EstimateError) as e:
            est.estimate({}, "auto", "auto")
        assert e.value.error == "Timeout" and "exceeded" in e.value.message
    finally:
        est.close()


def test_worker_dying_at_start_is_reported(tmp_path, monkeypatch):
    script = tmp_path / "worker.py"
    script.write_text("raise SystemExit(3)\n", encoding="utf-8")
    monkeypatch.setattr(sandbox, "_command", lambda *a: [sys.executable, str(script)])
    est = Estimator(tmp_path / "w", timeout_s=60, startup_timeout_s=60)
    try:
        with pytest.raises(EstimateError) as e:
            est.estimate({}, "auto", "auto")
        assert e.value.error == "WorkerCrashed"
    finally:
        est.close()
