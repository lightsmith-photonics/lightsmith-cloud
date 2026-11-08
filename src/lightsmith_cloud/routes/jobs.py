"""Core API: health, schema, backends, doctor, estimate, jobs and job events."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections import OrderedDict
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response, WebSocket
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from starlette.websockets import WebSocketDisconnect

from .. import __version__
from ..auth import Principal, authenticate
from ..billing import admit, credits_estimate, effective_plan, estimate_seconds, pick_tier
from ..context import Context
from ..db import ACTIVE, TERMINAL, Job, JobEvent, Org, append_events, as_utc
from ..deps import anonymous, check_id, get_ctx, principal
from ..errors import ApiError, not_found
from ..executor import finish
from ..lib import osc
from ..limits import check_device, check_limits, parse_simulation, resolve_methods
from ..problem import backends_for, kind_of, n_wavelengths, problem_json_schema
from ..sandbox import EstimateError
from ..schemas import EstimateRequest, JobCreated, JobList, JobRequest, JobView, DiscretizationRequest, SupportsRequest
from ..sweep import swept_specs

router = APIRouter(prefix="/v1")

# --- public / informational -------------------------------------------------


@router.get("/health", dependencies=[Depends(anonymous)])
def health(request: Request) -> dict[str, Any]:
    ctx = get_ctx(request)
    out: dict[str, Any] = {"status": "ok", "service": ctx.settings.service_name, "mode": ctx.settings.mode,
                           "version": __version__, "library": {"name": osc.__name__, "version": osc.__version__}}
    if ctx.settings.hosted and ctx.redis is not None:
        try:
            ctx.redis.ping()
            from rq import Queue

            out["queues"] = {t.queue: len(Queue(t.queue, connection=ctx.redis)) for t in ctx.settings.tiers}
        except Exception as e:
            out["status"], out["redis_error"] = "degraded", type(e).__name__
    return out


@router.get("/schema", dependencies=[Depends(anonymous)])
def schema(kind: str | None = Query(None, pattern="^scattering$")) -> dict[str, Any]:
    """JSON Schema of every problem kind: the envelope `{"problem": <union on kind>}`.
    `?kind=scattering` returns the scattering Simulation schema alone."""
    return osc.json_schema() if kind == "scattering" else problem_json_schema()


def _options_schema(b) -> dict | None:
    model = getattr(b, "options_model", None)  # proposed library attribute; None until backends declare it
    try:
        return model.model_json_schema() if model is not None else None
    except Exception:
        return None


@router.get("/backends", dependencies=[Depends(anonymous)])
def backends() -> list[dict[str, Any]]:
    return [{**b.info(), "options_schema": _options_schema(b)} for b in osc.all_backends()]


@router.post("/supports")
def supports(body: SupportsRequest, request: Request, p: Principal = Depends(principal)) -> list[dict[str, Any]]:
    """For each backend of the problem's kind: whether it is installed and whether it can run this problem."""
    problem = parse_simulation(body.simulation)
    check_limits(problem, get_ctx(request).settings)
    out = []
    for b in backends_for(problem):
        avail, why_not = b.available()
        ok, reason = b.supports(problem) if avail else (False, "")
        out.append({"name": b.name, "method": b.method, "available": avail, "unavailable_reason": why_not or None,
                    "supported": bool(avail and ok), "reason": reason or None})
    return out


@router.get("/doctor")
def doctor(request: Request, p: Principal = Depends(principal)) -> dict[str, Any]:
    ctx = get_ctx(request)
    out = osc.doctor()
    if ctx.settings.hosted:
        out = {"api_host": out, "gpu_tiers": [{"queue": t.queue, "memory_gib": t.memory_bytes / 2**30,
                                               "credit_multiplier": t.credit_multiplier}
                                              for t in ctx.settings.tiers]}
    return out


# --- estimate ---------------------------------------------------------------

_cache: OrderedDict[str, dict] = OrderedDict()
_cache_lock = threading.Lock()


def run_estimate(ctx: Context, spec: dict, method: str, device: str) -> dict:
    key = hashlib.sha256(json.dumps([spec, method, device], sort_keys=True).encode()).hexdigest()
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return dict(_cache[key])
    try:
        est = ctx.estimator.estimate(spec, method, device)
    except EstimateError as e:
        raise ApiError(500 if e.error == "WorkerCrashed" else 422, e.error, e.message) from None
    with _cache_lock:
        _cache[key] = est
        while len(_cache) > 256:
            _cache.popitem(last=False)
    return dict(est)


def _validated(ctx: Context, sim, spec: dict, plan) -> None:
    max_bytes = plan.max_spec_bytes if plan else ctx.settings.max_body_bytes
    size = len(json.dumps(spec))
    if size > max_bytes:
        raise ApiError(413, "SpecTooLarge", f"simulation is {size} bytes; the limit is {max_bytes}")
    check_limits(sim, ctx.settings, spec)


def _hosted_estimate(ctx: Context, sim, spec: dict, methods: list[str], plan) -> dict:
    """Estimate on the API host (CPU, double precision: an upper bound for GPU memory) and price it."""
    ests = [run_estimate(ctx, spec, m, "cpu") for m in methods]
    mem = max(int(e.get("memory_bytes") or 0) for e in ests)  # optional: not every backend reports it
    tier = pick_tier(ctx.settings, mem)
    secs = [credits_estimate(e, n_wavelengths(sim), tier, ctx.settings) for e in ests]
    out = dict(ests[0]) if len(ests) == 1 else {"per_method": dict(zip(methods, ests))}
    seconds = round(sum(c["seconds_estimate"] for c in secs), 1)
    out.update(memory_bytes=mem, credits_estimate=sum(c["credits_estimate"] for c in secs),
               seconds_estimate=seconds, estimated_runtime_s=seconds,
               queue=tier.queue if tier else None, credit_basis=secs[0]["credit_basis"],
               runtime_basis=min((c["runtime_basis"] for c in secs), key=["rough", "calibrated", "backend"].index),
               fits_plan=bool(tier) and mem <= plan.max_memory_bytes, plan=plan.name)
    return out


@router.post("/estimate")
def estimate(body: EstimateRequest, request: Request, p: Principal = Depends(principal)) -> dict[str, Any]:
    ctx = get_ctx(request)
    sim = parse_simulation(body.simulation)
    spec = sim.model_dump(mode="json")
    device = check_device(body.device)
    resolve_methods(sim, "solve", body.method, None, ctx.settings)  # UnknownMethod / UnsupportedProblem, as jobs
    if not ctx.settings.hosted:
        _validated(ctx, sim, spec, None)
        est = run_estimate(ctx, spec, body.method, device)
        est.setdefault("estimated_runtime_s", round(estimate_seconds(est, n_wavelengths(sim)), 1))
        return est
    with ctx.db.session() as s:
        plan = effective_plan(s.get(Org, p.org_id))
    _validated(ctx, sim, spec, plan)
    return _hosted_estimate(ctx, sim, spec, [body.method], plan)


# --- discretization ---------------------------------------------------------


@router.post("/discretization")
def discretization(body: DiscretizationRequest, request: Request, p: Principal = Depends(principal)) -> dict[str, Any]:
    """What the backend will discretise for this problem (grid, mesh, dipole lattice, harmonics, cells, plane
    waves), with its estimate: `lightsmith.discretization`, run in the same sandboxed child as estimate. Never solves."""
    ctx = get_ctx(request)
    sim = parse_simulation(body.simulation)
    spec = sim.model_dump(mode="json")
    resolve_methods(sim, "solve", body.method, None, ctx.settings)  # UnknownMethod / UnsupportedProblem, as jobs
    if ctx.settings.hosted:
        with ctx.db.session() as s:
            plan = effective_plan(s.get(Org, p.org_id))
        _validated(ctx, sim, spec, plan)
    else:
        _validated(ctx, sim, spec, None)
    if ctx.estimator is None:
        raise ApiError(501, "NotImplemented", "this server runs no estimate worker")
    try:
        return ctx.estimator.discretization(spec, body.method)
    except EstimateError as e:
        if e.error == "NotImplementedError":  # a library without discretization()
            raise ApiError(501, "NotImplemented", e.message) from None
        raise ApiError(500 if e.error == "WorkerCrashed" else 422, e.error, e.message) from None


# --- jobs -------------------------------------------------------------------


def job_view(ctx: Context, job: Job, *, include_result: bool = True, include_spec: bool = False) -> dict[str, Any]:
    result, url = None, None
    if job.result_key:
        url = f"/v1/jobs/{job.id}/result"
        if include_result and (job.result_bytes or 0) <= ctx.settings.inline_result_max_bytes:
            result = json.loads(ctx.store.get(job.result_key))
    est = {k: v for k, v in (job.estimate or {}).items()} or None
    return {
        "job_id": job.id, "id": job.id, "kind": job.kind, "operation": job.kind, "problem_kind": job.problem_kind,
        "status": job.status, "progress": round(job.progress or 0.0, 4), "message": _message(job),
        "stage": job.stage, "method": job.method, "methods": job.methods, "reference": job.reference,
        "device": job.device, "label": job.label, "created_at": as_utc(job.created_at),
        "started_at": as_utc(job.started_at), "finished_at": as_utc(job.finished_at), "error": job.error,
        "warnings": job.warnings, "device_used": job.device_used, "estimate": est,
        "usage": {"runtime_s": job.runtime_s, "gpu_seconds": job.gpu_seconds, "credits": job.credits},
        "result": result, "result_kind": job.result_kind, "result_url": url, "result_bytes": job.result_bytes,
        "sweep": job.params if job.kind == "sweep" else None,
        "simulation": job.spec if include_spec else None,
    }


def _message(job: Job) -> str | None:
    if job.error:
        return job.error.get("message")
    if job.status == "running" and job.stage:
        return job.stage.replace("_", " ")
    return job.status


def _load(s, p: Principal, job_id: str) -> Job:
    job = s.get(Job, check_id(job_id))
    if job is None or (job.org_id != p.org_id and not p.is_admin):
        raise not_found()
    return job


@router.post("/jobs", status_code=202, response_model=JobCreated)
def create_job(body: JobRequest, request: Request, p: Principal = Depends(principal)) -> dict[str, Any]:
    ctx = get_ctx(request)
    s_ = ctx.settings
    sim = parse_simulation(body.simulation)
    if body.kind == "optimize" and kind_of(sim) != "optimization":
        raise ApiError(422, "ValidationError", f"optimize jobs need an optimization problem, not {kind_of(sim)!r}")
    spec = sim.model_dump(mode="json")
    device = check_device(body.device)
    swept: list = []
    if body.kind == "sweep":
        if body.sweep is None:
            raise ApiError(422, "ValidationError", "sweep jobs need `sweep`: {path, values}")
        try:
            raw = swept_specs(spec, list(body.sweep.path), list(body.sweep.values))
        except ValueError as e:
            raise ApiError(422, "ValidationError", str(e)) from None
        for v, r in zip(body.sweep.values, raw):
            try:
                one = parse_simulation(r)
            except ApiError as e:
                raise ApiError(422, "ValidationError", f"sweep value {v}: {e.message}") from None
            swept.append((one, one.model_dump(mode="json")))
    method, methods = resolve_methods(sim, "compare" if body.kind == "compare" else "solve", body.method,
                                      body.methods, s_)
    if body.reference and body.kind == "compare" and body.reference not in (methods or []):
        raise ApiError(422, "ValidationError", "reference must be one of methods")

    est, mem, queue, ce, plan, warning = None, None, None, None, None, None
    if s_.hosted:
        with ctx.db.session() as s:
            plan = effective_plan(s.get(Org, p.org_id))
        _validated(ctx, sim, spec, plan)
        if swept:
            for one, one_spec in swept:
                _validated(ctx, one, one_spec, plan)
            ests = [_hosted_estimate(ctx, one, one_spec, [method], plan) for one, one_spec in swept]
            worst = max(ests, key=lambda e: e["memory_bytes"])
            est = {**worst, "credits_estimate": sum(e["credits_estimate"] for e in ests),
                   "seconds_estimate": round(sum(e["seconds_estimate"] for e in ests), 1), "sweep_values": len(ests)}
            est["estimated_runtime_s"] = est["seconds_estimate"]
        else:
            est = _hosted_estimate(ctx, sim, spec, methods or [method], plan)
        mem, queue, ce = est["memory_bytes"], est["queue"], est["credits_estimate"]
        if queue is None:
            raise ApiError(422, "TooLarge", f"estimated memory {mem / 2**30:.1f} GiB does not fit any GPU tier",
                           memory_bytes=mem)
        if mem > plan.max_memory_bytes:
            raise ApiError(422, "TooLargeForPlan", f"estimated memory {mem / 2**30:.1f} GiB exceeds the "
                           f"{plan.max_memory_bytes / 2**30:.0f} GiB limit of the {plan.name} plan", plan=plan.name)
    else:
        _validated(ctx, sim, spec, None)
        for one, one_spec in swept:
            _validated(ctx, one, one_spec, None)

    with ctx.db.session() as s:
        if s_.hosted:
            s.execute(select(Org).where(Org.id == p.org_id).with_for_update())  # serialise per-org admission
            active = s.execute(select(func.count()).select_from(Job).where(
                Job.org_id == p.org_id, Job.status.in_(ACTIVE))).scalar_one()
            if active >= plan.max_concurrent_jobs:
                raise ApiError(429, "ConcurrencyLimit", f"{active} jobs already queued or running; the "
                               f"{plan.name} plan allows {plan.max_concurrent_jobs}", plan=plan.name)
            warning = admit(s, s.get(Org, p.org_id), plan, ce, s_)
        job = Job(org_id=p.org_id, user_id=p.user_id, api_key_id=p.api_key_id, kind=body.kind,
                  problem_kind=kind_of(sim), method=method,
                  methods=methods, reference=body.reference, device=device, label=body.label, spec=spec,
                  params=body.sweep.model_dump() if body.sweep else None,
                  spec_bytes=len(json.dumps(spec)), estimate=est, memory_bytes=mem, credits_estimate=ce, queue=queue)
        s.add(job)
        s.flush()
        append_events(s, job, [("status", {"status": "queued"})])
    try:
        ctx.runner.submit(job, timeout_s=plan.max_runtime_s if plan else None)
    except Exception as e:
        with ctx.db.session() as s:
            finish(s, s.get(Job, job.id), "failed", {"error": "QueueUnavailable", "message": str(e)[:500]})
        raise ApiError(503, "QueueUnavailable", "could not enqueue the job; try again shortly") from None
    return {"job_id": job.id, "id": job.id, "status": "queued", "credits_estimate": ce, "queue": queue,
            "quota_warning": warning}


@router.get("/jobs", response_model=JobList, response_model_exclude_none=False)
def list_jobs(request: Request, p: Principal = Depends(principal), mine: bool = False,
              status: str | None = Query(None, pattern="^(queued|running|succeeded|failed|cancelled)$"),
              limit: int = Query(50, ge=1, le=200), before: datetime | None = None) -> dict[str, Any]:
    ctx = get_ctx(request)
    with ctx.db.session() as s:
        q = select(Job).where(Job.org_id == p.org_id)
        if mine and p.user_id:
            q = q.where(Job.user_id == p.user_id)
        if status:
            q = q.where(Job.status == status)
        if before:
            q = q.where(Job.created_at < before)
        jobs = list(s.execute(q.order_by(Job.created_at.desc()).limit(limit)).scalars())
        views = [job_view(ctx, j, include_result=False) for j in jobs]
    nxt = as_utc(jobs[-1].created_at) if len(jobs) == limit else None
    return {"jobs": views, "next_before": nxt}


@router.get("/jobs/{job_id}", response_model=JobView)
def get_job(job_id: str, request: Request, p: Principal = Depends(principal), include_result: bool = True,
            include_spec: bool = False) -> dict[str, Any]:
    ctx = get_ctx(request)
    with ctx.db.session() as s:
        return job_view(ctx, _load(s, p, job_id), include_result=include_result, include_spec=include_spec)


@router.get("/jobs/{job_id}/result", response_class=Response,
            responses={200: {"content": {"application/json": {}}, "description": "Result or compare JSON."}})
def get_result(job_id: str, request: Request, p: Principal = Depends(principal)) -> Response:
    ctx = get_ctx(request)
    with ctx.db.session() as s:
        job = _load(s, p, job_id)
        if not job.result_key:
            raise ApiError(409, "NoResult", f"job is {job.status}; no result available")
        key = job.result_key
    return Response(ctx.store.get(key), media_type="application/json")


@router.post("/jobs/{job_id}/cancel", status_code=202, response_model=JobView)
@router.delete("/jobs/{job_id}", status_code=202, response_model=JobView)
def cancel_job(job_id: str, request: Request, p: Principal = Depends(principal)) -> dict[str, Any]:
    ctx = get_ctx(request)
    with ctx.db.session() as s:
        job = _load(s, p, job_id)
        if job.status in TERMINAL:
            return job_view(ctx, job, include_result=False)
        job.cancel_requested = True
        if job.status == "queued":
            finish(s, job, "cancelled", {"error": "Cancelled", "message": "job was cancelled"})
        view = job_view(ctx, job, include_result=False)
    ctx.runner.cancel(job_id)
    return view


# --- events -----------------------------------------------------------------

WS_PROTOCOL = "lightsmith.bearer"


def _ws_auth(ctx: Context, ws: WebSocket) -> tuple[Principal, str | None]:
    """Header ``Authorization: Bearer``; for browsers, subprotocols ``["lightsmith.bearer", <token>]``
    (preferred) or ``?token=`` / ``?api_key=`` (may end up in proxy logs; use short-lived JWTs and wss)."""
    auth, sub = ws.headers.get("authorization"), None
    if not auth:
        q = ws.query_params.get("token") or ws.query_params.get("api_key")
        auth = f"Bearer {q}" if q else None
    protos = ws.scope.get("subprotocols") or []
    if WS_PROTOCOL in protos:
        sub = WS_PROTOCOL
        i = protos.index(WS_PROTOCOL)
        if not auth and i + 1 < len(protos):
            auth = f"Bearer {protos[i + 1]}"
    with ctx.db.session() as s:
        p = authenticate(s, ctx.settings, auth, ws.headers.get("x-org-id"))
    return p, sub


def _events_since(ctx: Context, p: Principal, job_id: str, after: int) -> tuple[list[dict], str]:
    with ctx.db.session() as s:
        job = _load(s, p, job_id)
        rows = s.execute(select(JobEvent).where(JobEvent.job_id == job_id, JobEvent.seq > after)
                         .order_by(JobEvent.seq).limit(500)).scalars()
        evs = []
        for e in rows:
            ev = {"type": e.type, "seq": e.seq, "ts": as_utc(e.ts).isoformat(), **e.data}
            if e.type == "log":
                ev.setdefault("message", ev.get("line"))
            evs.append(ev)
        return evs, job.status


def _final_events(ctx: Context, p: Principal, job_id: str) -> list[dict]:
    """`result` (inline when small enough) or `error`, sent just before `end`."""
    with ctx.db.session() as s:
        job = _load(s, p, job_id)
        if job.status == "succeeded" and job.result_key:
            if (job.result_bytes or 0) <= ctx.settings.inline_result_max_bytes:
                return [{"type": "result", "result_kind": job.result_kind,
                         "result": json.loads(ctx.store.get(job.result_key))}]
            return [{"type": "result", "result_kind": job.result_kind, "result": None,
                     "result_url": f"/v1/jobs/{job.id}/result"}]
        if job.error:
            return [{"type": "error", **job.error}]
        return []


@router.websocket("/jobs/{job_id}/events")
async def job_events(ws: WebSocket, job_id: str, after: int = 0):
    """Stream status, progress and log events; replays from `after` (event seq), then closes when the job ends."""
    ctx: Context = ws.app.state.ctx
    try:
        p, sub = await run_in_threadpool(_ws_auth, ctx, ws)
    except ApiError as e:
        await ws.accept()
        await ws.close(code=4401 if e.status == 401 else 4403, reason=e.message[:120])
        return
    await ws.accept(subprotocol=sub)
    last = after
    try:
        while True:
            try:
                events, status = await run_in_threadpool(_events_since, ctx, p, job_id, last)
            except ApiError:
                await ws.close(code=4404, reason="job not found")
                return
            for ev in events:
                await ws.send_json(ev)
                last = ev["seq"]
            if status in TERMINAL and not events:
                for ev in await run_in_threadpool(_final_events, ctx, p, job_id):
                    await ws.send_json(ev)
                await ws.send_json({"type": "end", "status": status})
                await ws.close(code=1000)
                return
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=0.25)
                if msg.get("type") == "websocket.disconnect":
                    return
            except asyncio.TimeoutError:
                pass
    except (WebSocketDisconnect, RuntimeError):
        return
