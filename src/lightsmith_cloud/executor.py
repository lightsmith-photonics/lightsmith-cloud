"""Run one queued job end to end. Shared by the local worker pool and the hosted GPU workers."""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .billing import credits_for, effective_plan, record_usage
from .db import Job, Org, UsageRecord, append_events, utcnow
from .sandbox import Limits, Outcome, run_job
from .storage import result_key

if TYPE_CHECKING:
    from .context import Context

log = logging.getLogger("lightsmith_cloud")

# Failures caused by the service rather than the problem are not charged.
_NOT_BILLED = ("WorkerCrashed", "AdmissionRejected", "ServerShutdown", "WorkerLost", "StorageError")


@dataclass
class WorkerInfo:
    """A hosted GPU worker: one job at a time on one GPU."""

    gpu_memory_bytes: int
    queues: list[str] = field(default_factory=list)


class _Sink:
    """Collects child events into the job's event log, throttling database writes."""

    def __init__(self, ctx: "Context", job_id: str, shutdown: threading.Event | None):
        self.ctx, self.job_id, self.shutdown = ctx, job_id, shutdown
        self.pending: list[tuple[str, dict]] = []
        self.progress: tuple[float, str | None] | None = None
        self.logs = 0
        self.last_flush = self.last_cancel_check = 0.0
        self.cancel = False

    def on_event(self, ev: dict) -> None:
        typ = ev.pop("type", "log")
        if typ == "log":
            self.logs += 1
            if self.logs > self.ctx.settings.max_log_lines:
                if self.logs == self.ctx.settings.max_log_lines + 1:
                    self.pending.append(("log", {"stream": "server", "line": "log truncated"}))
                return
        elif typ == "progress":
            self.progress = (float(ev.get("progress") or 0.0), ev.get("stage"))
        self.pending.append((typ, ev))
        if typ == "progress" or time.monotonic() - self.last_flush > 0.5:
            self.flush()

    def flush(self) -> None:
        if not self.pending and self.progress is None:
            return
        with self.ctx.db.session() as s:
            job = s.get(Job, self.job_id)
            if self.progress is not None:
                job.progress, job.stage = self.progress
            append_events(s, job, self.pending)
        self.pending, self.progress = [], None
        self.last_flush = time.monotonic()

    def should_cancel(self) -> bool:
        if self.shutdown is not None and self.shutdown.is_set():
            return True
        now = time.monotonic()
        if now - self.last_cancel_check > 1.0:
            self.last_cancel_check = now
            self.flush()
            with self.ctx.db.session() as s:
                self.cancel = bool(s.get(Job, self.job_id).cancel_requested)
        return self.cancel


def _device_for(job: Job, worker: WorkerInfo | None) -> str:
    if worker is None or job.device in ("cpu", "torch-cpu"):
        return job.device
    return "cuda:0" if worker.gpu_memory_bytes else "auto"  # one GPU per worker, exposed as device 0


def _limits(ctx: "Context", plan, worker: WorkerInfo | None) -> Limits:
    s = ctx.settings
    if plan is None:
        return Limits(timeout_s=s.job_timeout_s or None, memory_bytes=s.job_memory_bytes or None)
    gpu = int(worker.gpu_memory_bytes * 0.95) if worker and worker.gpu_memory_bytes else None
    return Limits(timeout_s=plan.max_runtime_s, memory_bytes=plan.host_memory_bytes, gpu_memory_bytes=gpu)


def finish(s, job: Job, status: str, error: dict | None = None) -> None:
    job.status, job.error, job.finished_at = status, error, utcnow()
    if status == "succeeded":
        job.progress, job.stage = 1.0, "done"
    append_events(s, job, [("status", {"status": status, **({"error": error} if error else {})})])


def execute_job(ctx: "Context", job_id: str, worker: WorkerInfo | None = None,
                shutdown: threading.Event | None = None) -> str | None:
    """Run job `job_id` if it is still queued. Returns the final status (None if skipped)."""
    s = ctx.settings
    with ctx.db.session() as ses:
        job = ses.get(Job, job_id)
        if job is None or job.status != "queued":
            return None
        if job.cancel_requested:
            finish(ses, job, "cancelled", {"error": "Cancelled", "message": "job was cancelled"})
            return "cancelled"
        plan = effective_plan(ses.get(Org, job.org_id)) if s.hosted else None
        if worker and worker.gpu_memory_bytes and (job.memory_bytes or 0) > 0.95 * worker.gpu_memory_bytes:
            finish(ses, job, "failed", {"error": "AdmissionRejected",
                                        "message": f"job needs about {job.memory_bytes / 2**30:.1f} GiB of GPU memory; "
                                                   f"this worker has {worker.gpu_memory_bytes / 2**30:.1f} GiB"})
            return "failed"
        job.status, job.started_at, job.stage, job.progress = "running", utcnow(), "starting", 0.01
        append_events(ses, job, [("status", {"status": "running"})])
        request = {"kind": job.kind, "simulation": job.spec, "method": job.method, "methods": job.methods,
                   "reference": job.reference, "params": job.params, "device": _device_for(job, worker)}

    s.work_dir.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix=f"job-{job_id[:12]}-", dir=s.work_dir))
    request["result_path"] = str(workdir / "result.json")
    sink = _Sink(ctx, job_id, shutdown)
    try:
        try:
            outcome = run_job(request, workdir, _limits(ctx, plan, worker), on_event=sink.on_event,
                              should_cancel=sink.should_cancel)
        except Exception as e:  # could not start the child at all
            log.exception("job %s: sandbox failure", job_id)
            outcome = Outcome("failed", 0.0, {"error": "WorkerCrashed", "message": f"could not run solver: {e}"})
        if outcome.status == "cancelled" and shutdown is not None and shutdown.is_set():
            outcome.status = "failed"
            outcome.error = {"error": "ServerShutdown", "message": "the server stopped while the job was running"}
        size = None
        if outcome.status == "succeeded":
            try:
                size = ctx.store.put(result_key(job_id), (workdir / "result.json").read_bytes())
            except Exception as e:
                log.exception("job %s: storing result failed", job_id)
                outcome.status, outcome.error = "failed", {"error": "StorageError", "message": str(e)[:1000]}
        sink.flush()
        return _record(ctx, job_id, outcome, size, worker)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _record(ctx: "Context", job_id: str, outcome: Outcome, size: int | None, worker: WorkerInfo | None) -> str:
    s = ctx.settings
    with ctx.db.session() as ses:
        job = ses.get(Job, job_id)
        if size is not None:
            job.result_key, job.result_bytes, job.result_kind = result_key(job_id), size, outcome.result_kind
        job.runtime_s = round(outcome.runtime_s, 3)
        job.device_used = outcome.device or None
        job.warnings = outcome.warnings or None
        on_gpu = str(outcome.device.get("device", "cpu")) != "cpu" if outcome.device else bool(worker)
        job.gpu_seconds = job.runtime_s if on_gpu else 0.0
        billed = s.hosted and not (outcome.error and outcome.error.get("error") in _NOT_BILLED)
        job.credits = credits_for(job.runtime_s, gpu=on_gpu, tier=s.tier(job.queue or ""), settings=s) if billed else None
        finish(ses, job, outcome.status, outcome.error)
        status, rec_id = job.status, None
        if billed and job.credits > 0:
            rec_id = record_usage(ses, job.org_id, user_id=job.user_id, job_id=job.id, runtime_s=job.runtime_s,
                                  gpu_seconds=job.gpu_seconds, credits=job.credits, queue=job.queue).id
    if rec_id and ctx.billing.enabled:  # outside the job transaction: this is a network call
        with ctx.db.session() as ses:
            rec = ses.get(UsageRecord, rec_id)
            ctx.billing.report_usage(ses, ses.get(Org, rec.org_id), rec)
    return status


def result_json(ctx: "Context", job: Job):
    return json.loads(ctx.store.get(job.result_key))
