"""Solver child process. Never imported by the server; run as
``python -I -m lightsmith_cloud._child <request.json>`` (one job) or
``... --serve`` (a long-lived estimate worker reading JSON lines on stdin).

The request is plain JSON validated by lightsmith's pydantic models; nothing
in it is executed. Protocol: one JSON object per line on the original stdout.
Anything the solvers print goes to stderr, which the parent keeps as logs.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from typing import Any


def _finite(x: Any) -> Any:
    """JSON has no NaN/Inf: encode non-finite floats as null."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: _finite(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_finite(v) for v in x]
    return x


def _protocol_stream():
    """Keep the real stdout for the protocol and point fd 1 / sys.stdout at stderr."""
    proto = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return proto


def _limit_resources(req: dict) -> None:
    if sys.platform != "win32":
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        mem = req.get("memory_bytes")
        # RLIMIT_AS breaks CUDA (it reserves huge virtual ranges), so only cap CPU runs this way.
        if mem and str(req.get("device", "auto")) in ("cpu", "torch-cpu"):
            resource.setrlimit(resource.RLIMIT_AS, (int(mem), int(mem)))
    gpu_mem = req.get("gpu_memory_bytes")
    if gpu_mem:
        try:
            import torch

            if torch.cuda.is_available():
                total = torch.cuda.get_device_properties(0).total_memory
                torch.cuda.set_per_process_memory_fraction(min(1.0, gpu_mem / total), 0)
        except ImportError:
            pass


def _solver(osc, emit):
    """osc.solve, forwarding fine-grained progress when the library supports callbacks
    (see proposals/library-progress.diff); otherwise progress stays coarse."""
    import inspect

    if "progress" not in inspect.signature(osc.solve).parameters:
        return lambda problem, method, device, lo, hi: osc.solve(problem, method, device)

    def solve(problem, method, device, lo, hi):
        last = [0.0]

        def cb(e: dict) -> None:
            now = time.monotonic()
            if e.get("stage") not in ("solve_started", "solve_done") and now - last[0] < 0.5:
                return  # throttle: at most two events per second
            last[0] = now
            info = {k: v for k, v in e.items() if k not in ("stage", "fraction") and isinstance(v, (str, int, float))}
            emit(type="progress", stage=e.get("stage", "solving"), progress=lo + (hi - lo) * (e.get("fraction") or 0.0),
                 **{"method": method, **info})

        return osc.solve(problem, method, device, progress=cb)

    return solve


INVERSE_METHODS = ("auto", "inverse", "lightsmith-inverse")


def _optimize(problem, method, device, emit):
    """Inverse design through lightsmith.inverse.topopt.optimize, streaming one progress event per
    iteration (throttled to 2/s, first and last always sent). None if this path does not apply."""
    from lightsmith_cloud.lib import inverse_optimizer

    optimize = inverse_optimizer()
    if optimize is None or getattr(problem, "kind", None) != "optimization" or method not in INVERSE_METHODS:
        return None
    max_iter = max(1, int(getattr(getattr(problem, "optimizer", None), "max_iter", 1) or 1))
    last = [0.0]

    def callback(state) -> None:
        now = time.monotonic()
        final = state.iteration + 1 >= max_iter
        if state.iteration and not final and now - last[0] < 0.5:
            return None
        last[0] = now
        emit(type="progress", stage="iteration", method="inverse", iteration=state.iteration, max_iter=max_iter,
             objective=state.objective, binarization=state.binarization, beta=state.beta,
             evaluations=state.evaluations, elapsed_s=round(state.elapsed_s, 3),
             progress=0.1 + 0.85 * min(1.0, (state.iteration + 1) / max_iter))
        return None

    return optimize(problem, device=device, callback=callback)


def _compare(osc, problem, methods, device, reference, emit) -> dict:
    # Mirrors lightsmith.compare, solving method by method so progress can be reported.
    from lightsmith_cloud.problem import backends_for

    solve = _solver(osc, emit)
    if not methods:
        methods = [b.name for b in backends_for(problem) if b.available()[0] and b.supports(problem)[0]]
    results = {}
    for i, m in enumerate(methods):
        emit(type="progress", stage="method_started", method=m, progress=0.1 + 0.85 * i / len(methods))
        results[m] = solve(problem, m, device, 0.1 + 0.85 * i / len(methods), 0.1 + 0.85 * (i + 1) / len(methods))
        emit(type="progress", stage="method_done", method=m, progress=0.1 + 0.85 * (i + 1) / len(methods))
    if reference is None:
        reference = next((m for m, r in results.items() if getattr(r, "method", None) == "mie"), methods[0])
    ref = results[reference]
    diffs = {}
    for m, r in results.items():
        if m == reference:
            continue
        if isinstance(r, osc.Result) and isinstance(ref, osc.Result):
            diffs[m] = {q: osc.relative_difference(r, ref, q) for q in ("c_ext_nm2", "c_sca_nm2", "c_abs_nm2")}
        elif hasattr(r, "difference"):
            diffs[m] = r.difference(ref)
    return {"reference": reference, "results": {k: v.model_dump(mode="json") for k, v in results.items()},
            "relative_difference": diffs}


def _device_info(provenance: dict) -> dict:
    return {k: provenance.get(k) for k in ("framework", "device", "gpu_name") if k in provenance}


def run_one(path: str) -> int:
    proto = _protocol_stream()

    def emit(**e):
        proto.write(json.dumps(_finite(e)) + "\n")

    with open(path, encoding="utf-8") as f:
        req = json.load(f)
    try:
        _limit_resources(req)
        emit(type="progress", stage="started", progress=0.02)
        from lightsmith_cloud.lib import osc
        from lightsmith_cloud.problem import parse_problem

        problem = parse_problem(req["simulation"])
        device = req.get("device", "auto")
        t0 = time.perf_counter()
        if req["kind"] in ("solve", "optimize"):
            method = req.get("method") or "auto"
            emit(type="progress", stage="method_started", method=method, progress=0.1)
            res = _optimize(problem, method, device, emit)
            if res is None:
                res = _solver(osc, emit)(problem, method, device, 0.1, 0.95)
            emit(type="progress", stage="method_done", method=getattr(res, "backend", method), progress=0.95)
            out: dict = res.model_dump(mode="json")
            used = _device_info(out.get("provenance") or {})
            warnings = list(out.get("warnings") or [])
            result_kind = out.get("kind") or "cross_sections"
        elif req["kind"] == "estimate":
            out = osc.estimate(problem, req.get("method") or "auto", device)
            used, warnings, result_kind = {k: out[k] for k in ("framework", "device") if k in out}, [], "estimate"
        elif req["kind"] == "compare":
            out = _compare(osc, problem, req.get("methods"), device, req.get("reference"), emit)
            provs = [r.get("provenance") or {} for r in out["results"].values()]
            used = next((_device_info(p) for p in provs if p.get("device") not in (None, "cpu")),
                        _device_info(provs[0]) if provs else {})
            warnings = [f"{m}: {w}" for m, r in out["results"].items() for w in (r.get("warnings") or [])]
            result_kind = "compare"
        else:
            raise ValueError(f"unknown job kind {req['kind']!r}")
        solve_s = time.perf_counter() - t0
        with open(req["result_path"], "w", encoding="utf-8") as f:
            json.dump(_finite(out), f, separators=(",", ":"), allow_nan=False)
        emit(type="done", device=used, warnings=warnings[:100], solve_s=solve_s, result_kind=result_kind)
        return 0
    except BaseException as e:  # report everything, including MemoryError and KeyboardInterrupt
        emit(type="error", error=type(e).__name__, message=str(e)[:4000])
        return 1


def serve() -> int:
    """Estimate worker: {"id", "simulation", "method", "device"} in, {"id", "ok", "result"|"error"} out."""
    proto = _protocol_stream()
    from .lib import osc

    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        try:
            est = osc.estimate(req["simulation"], req.get("method") or "auto", req.get("device") or "auto")
            reply = {"id": req.get("id"), "ok": True, "result": _finite(est)}
        except Exception as e:
            reply = {"id": req.get("id"), "ok": False, "error": type(e).__name__, "message": str(e)[:4000]}
        proto.write(json.dumps(reply, default=str) + "\n")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        sys.exit(serve())
    sys.exit(run_one(sys.argv[1]))
