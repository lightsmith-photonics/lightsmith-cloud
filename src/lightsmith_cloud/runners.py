"""Job dispatch.

Local mode: an in-process pool of threads, each supervising one solver
subprocess. Hosted mode: RQ queues in Redis, one queue per GPU tier, served
by ``lightsmith-worker`` processes (one per GPU).

Why RQ: each job is one blocking subprocess on one GPU, so a worker that
handles exactly one job at a time is the natural fit. RQ does that by default
with plain synchronous code, keeps per-queue routing simple (tier = queue),
and is small enough to read. Celery's prefetching and pools fight the
one-job-per-GPU model and add a lot of moving parts; arq's asyncio buys
nothing when the work is a child process. The database, not Redis, is the
source of truth for job state, so the queue can be swapped later.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import TYPE_CHECKING

from sqlalchemy import select

from .db import Job, append_events, utcnow
from .executor import execute_job

if TYPE_CHECKING:
    from .context import Context

log = logging.getLogger("lightsmith_cloud")

RQ_FUNC = "lightsmith_cloud.worker.run_job"


class LocalRunner:
    def __init__(self, ctx: "Context", workers: int = 1):
        self.ctx, self.n = ctx, workers
        self.q: queue.Queue[str | None] = queue.Queue()
        self.shutdown = threading.Event()
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        # Jobs that were running when the server last stopped cannot be resumed.
        with self.ctx.db.session() as s:
            for job in s.execute(select(Job).where(Job.status == "running")).scalars():
                job.status, job.finished_at = "failed", utcnow()
                job.error = {"error": "ServerShutdown", "message": "the server stopped while the job was running"}
                append_events(s, job, [("status", {"status": "failed", "error": job.error})])
            queued = [j.id for j in s.execute(select(Job).where(Job.status == "queued")
                                              .order_by(Job.created_at)).scalars()]
        for i in range(self.n):
            t = threading.Thread(target=self._loop, name=f"osc-worker-{i}", daemon=True)
            t.start()
            self.threads.append(t)
        for jid in queued:
            self.q.put(jid)

    def _loop(self) -> None:
        while not self.shutdown.is_set():
            jid = self.q.get()
            if jid is None:
                return
            try:
                execute_job(self.ctx, jid, shutdown=self.shutdown)
            except Exception:  # keep the worker alive
                log.exception("job %s failed in the runner", jid)

    def submit(self, job: Job, timeout_s: float | None = None) -> None:
        self.q.put(job.id)

    def cancel(self, job_id: str) -> None:
        pass  # queued jobs are skipped by execute_job; running ones see cancel_requested

    def stop(self, timeout: float = 15.0) -> None:
        self.shutdown.set()
        for _ in self.threads:
            self.q.put(None)
        for t in self.threads:
            t.join(timeout)


class RqRunner:
    def __init__(self, ctx: "Context", redis):
        self.ctx, self.redis = ctx, redis

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def submit(self, job: Job, timeout_s: float | None = None) -> None:
        from rq import Queue

        timeout = int(timeout_s or 3600) + 300  # the sandbox enforces the real cap; this is RQ's backstop
        Queue(job.queue, connection=self.redis).enqueue(
            RQ_FUNC, job.id, job_id=job.id, job_timeout=timeout, result_ttl=0, failure_ttl=7 * 86400)

    def cancel(self, job_id: str) -> None:
        from rq.exceptions import NoSuchJobError
        from rq.job import Job as RqJob

        try:
            RqJob.fetch(job_id, connection=self.redis).cancel()
        except (NoSuchJobError, Exception):  # already started or gone; the worker checks the database flag
            pass
