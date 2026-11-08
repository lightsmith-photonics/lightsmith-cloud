"""The solver child forwards library progress callbacks once the library has them."""

from __future__ import annotations

import time
from types import SimpleNamespace

from lightsmith_cloud._child import _finite, _solver


def test_progress_forwarded_when_supported():
    def solve(problem, method, device, progress=None):
        progress({"stage": "solve_started", "fraction": 0.0, "backend": "x"})
        progress({"stage": "dda_solve", "fraction": 0.5, "wavelength_nm": 500.0})
        progress({"stage": "solve_done", "fraction": 1.0})
        return "result"

    events = []
    out = _solver(SimpleNamespace(solve=solve), lambda **e: events.append(e))(None, "dda", "cpu", 0.1, 0.9)
    assert out == "result"
    assert [e["stage"] for e in events] == ["solve_started", "dda_solve", "solve_done"] or \
        [e["stage"] for e in events] == ["solve_started", "solve_done"]  # mid events are throttled to 2/s
    assert events[0]["progress"] == 0.1 and events[-1]["progress"] == 0.9
    assert all(e["method"] == "dda" for e in events)


def test_coarse_without_callback_support():
    calls = []
    osc = SimpleNamespace(solve=lambda problem, method, device: calls.append(method) or "r")
    assert _solver(osc, lambda **e: None)(None, "mie", "cpu", 0.1, 0.9) == "r" and calls == ["mie"]


def test_non_finite_floats_become_null():
    assert _finite({"a": [1.0, float("nan"), float("inf")], "b": (2.0,)}) == {"a": [1.0, None, None], "b": [2.0]}


def test_progress_holds_when_the_fraction_is_unknown():
    """An FDTD run does not know when it will end: events keep the last known fraction, never fall back to the start."""
    def solve(problem, method, device, progress=None):
        progress({"stage": "solve_started", "fraction": 0.0})
        progress({"stage": "dda_solve", "fraction": 0.5})
        time.sleep(0.6)  # past the two-events-per-second throttle
        progress({"stage": "fdtd_steps", "fraction": None, "steps": 100, "max_steps": 1000})
        progress({"stage": "solve_done", "fraction": 1.0})
        return "result"

    events = []
    fn = _solver(SimpleNamespace(solve=solve), lambda **e: events.append(e))
    assert fn({}, "fdtd", "cuda:0", 0.0, 1.0) == "result"
    held = [e for e in events if e["stage"] == "fdtd_steps"]
    assert held and held[0]["progress"] == 0.5 and held[0]["steps"] == 100
