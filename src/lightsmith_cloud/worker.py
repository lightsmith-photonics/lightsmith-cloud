"""Hosted GPU worker: ``lightsmith-worker``.

Run one worker per GPU (e.g. one container with one GPU, or
``CUDA_VISIBLE_DEVICES=1 lightsmith-worker``). It serves the RQ queue(s) of
its tier and runs one job at a time, each in a sandboxed subprocess. Before a
job starts it checks the job's estimated memory against this GPU (admission)
and caps the child's CUDA allocator to the GPU's memory.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import subprocess
import sys
from functools import lru_cache

from .config import Settings
from .context import Context, build_context
from .executor import WorkerInfo, execute_job

log = logging.getLogger("lightsmith_cloud.worker")


def detect_gpu_memory() -> int:
    """Total memory of CUDA device 0, probed in a child so this process never initialises CUDA."""
    code = "import torch;print(torch.cuda.get_device_properties(0).total_memory if torch.cuda.is_available() else 0)"
    try:
        out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120)
        return int(out.stdout.strip().splitlines()[-1])
    except Exception:
        return 0


_override: tuple[Context, WorkerInfo] | None = None


def configure(ctx: Context, info: WorkerInfo) -> None:
    """Use this context instead of one built from the environment (tests, embedding)."""
    global _override
    _override = (ctx, info)


def _ctx() -> Context:
    return _override[0] if _override else _env_ctx()


@lru_cache(maxsize=1)
def _env_ctx() -> Context:
    return build_context(Settings(), with_estimator=False)


def _worker_info() -> WorkerInfo:
    return _override[1] if _override else _env_worker_info()


@lru_cache(maxsize=1)
def _env_worker_info() -> WorkerInfo:
    s = _ctx().settings
    mem = s.worker_gpu_memory_bytes if s.worker_gpu_memory_bytes is not None else detect_gpu_memory()
    return WorkerInfo(gpu_memory_bytes=mem, queues=_queues(s, mem))


def _queues(s: Settings, gpu_mem: int) -> list[str]:
    if s.worker_queues:
        return [q.strip() for q in s.worker_queues.split(",") if q.strip()]
    # By default serve the largest tier this GPU can hold, and every smaller one after it.
    fits = [t.queue for t in s.tiers if t.memory_bytes <= gpu_mem * 1.02]
    return list(reversed(fits)) or [s.tiers[0].queue]


def run_job(job_id: str) -> str | None:
    """RQ entry point."""
    return execute_job(_ctx(), job_id, worker=_worker_info())


def make_worker(queues: list[str], redis):
    """An RQ SimpleWorker: runs jobs in this process (no fork). Isolation comes from the solver
    subprocess, and not forking keeps database connections and CUDA state sane."""
    import signal

    from rq import SimpleWorker
    from rq.timeouts import TimerDeathPenalty

    w = SimpleWorker(queues, connection=redis, name=f"{socket.gethostname()}.{os.getpid()}")
    if not hasattr(signal, "SIGALRM"):  # Windows
        w.death_penalty_class = TimerDeathPenalty
    return w


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lightsmith-worker", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--burst", action="store_true", help="exit when the queues are empty")
    p.add_argument("--queues", help="comma-separated queue names (default: from the GPU size)")
    p.add_argument("command", nargs="?", choices=["work", "healthcheck"], default="work",
                   help="work (default), or healthcheck: exit 0 if Redis answers and this host's worker is registered")
    a = p.parse_args(argv)
    if a.command == "healthcheck":
        return healthcheck()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    ctx = _ctx()
    if not ctx.settings.hosted or ctx.redis is None:
        print("lightsmith-worker needs LIGHTSMITH_MODE=hosted and REDIS_URL", file=sys.stderr)
        return 2
    info = _worker_info()
    if a.queues:
        info.queues = [q.strip() for q in a.queues.split(",") if q.strip()]
    log.info("GPU memory %.1f GiB, queues %s", info.gpu_memory_bytes / 2**30, info.queues)
    make_worker(info.queues, ctx.redis).work(burst=a.burst, with_scheduler=False)
    return 0


def healthcheck() -> int:
    """For the container HEALTHCHECK: Redis reachable and a live RQ worker registered from this host."""
    import redis as redis_lib
    from rq import Worker

    url = os.environ.get("REDIS_URL") or os.environ.get("LIGHTSMITH_REDIS_URL")
    try:
        conn = redis_lib.Redis.from_url(url, socket_timeout=5)
        conn.ping()
        host = socket.gethostname()
        alive = [w for w in Worker.all(connection=conn) if w.name.startswith(host + ".")]
    except Exception as e:
        print(f"unhealthy: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if not alive:
        print("unhealthy: no registered worker from this host", file=sys.stderr)
        return 1
    print(f"ok: {[w.name for w in alive]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
