"""Shared services for the API and workers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .auth import ensure_local_tenant
from .billing import StripeBilling
from .config import Settings
from .db import Database
from .ratelimit import MemoryRateLimiter, RedisRateLimiter
from .sandbox import Estimator
from .storage import ResultStore, make_store


@dataclass
class Context:
    settings: Settings
    db: Database
    store: ResultStore
    billing: StripeBilling
    estimator: Estimator | None = None
    runner: Any = None
    limiter: Any = None
    redis: Any = None


def build_context(settings: Settings, *, redis=None, store: ResultStore | None = None,
                  billing: StripeBilling | None = None, with_estimator: bool = True) -> Context:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_url)
    db.prepare(auto_migrate=not settings.hosted or settings.auto_migrate)
    with db.session() as s:
        ensure_local_tenant(s)
    if settings.hosted and redis is None and settings.redis_url:
        import redis as redis_lib

        redis = redis_lib.Redis.from_url(settings.redis_url)
    ctx = Context(settings=settings, db=db, store=store or make_store(settings),
                  billing=billing or StripeBilling(settings), redis=redis)
    if with_estimator:
        ctx.estimator = Estimator(settings.work_dir / "estimator", timeout_s=settings.estimate_timeout_s,
                                  memory_bytes=settings.estimate_memory_bytes,
                                  startup_timeout_s=settings.estimate_startup_timeout_s)
    ctx.limiter = RedisRateLimiter(redis) if redis is not None else MemoryRateLimiter()
    return ctx
