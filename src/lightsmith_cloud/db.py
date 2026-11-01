"""Persistence: SQLAlchemy models for jobs, events, tenants and usage.

SQLite in local mode, Postgres in hosted mode (``postgresql+psycopg://...``).
The schema is managed by Alembic migrations (``lightsmith_cloud/migrations``);
change a model, then add a revision with ``lightsmith_cloud.migrate.autogenerate``.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint,
                        create_engine, event, func, select, update)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.orm.attributes import set_committed_value

JSONType = JSON().with_variant(JSONB(), "postgresql")

TERMINAL = ("succeeded", "failed", "cancelled")
ACTIVE = ("queued", "running")


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; every stored timestamp is UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


class Org(Base):
    """Plan, quota and billing state of a platform org. The id is the platform's org id
    (or ``user:<sub>`` for a personal org); profiles live in the platform, not here."""

    __tablename__ = "orgs"
    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    plan: Mapped[str] = mapped_column(String(32), default="free")
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), index=True, unique=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64))
    subscription_status: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(String(200), ForeignKey("orgs.id"), index=True)
    user_id: Mapped[str | None] = mapped_column(String(200))  # platform user id (JWT sub)
    name: Mapped[str] = mapped_column(String(200), default="")
    prefix: Mapped[str] = mapped_column(String(16), unique=True)
    hash: Mapped[str] = mapped_column(String(64))  # HMAC-SHA256(pepper, key); the key itself is never stored
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(String(200), ForeignKey("orgs.id"), index=True)
    user_id: Mapped[str | None] = mapped_column(String(200), index=True)  # platform user id (JWT sub)
    api_key_id: Mapped[str | None] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16))  # solve | compare | estimate (optimize reserved)
    problem_kind: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    method: Mapped[str | None] = mapped_column(String(64))
    methods: Mapped[list | None] = mapped_column(JSONType)
    reference: Mapped[str | None] = mapped_column(String(64))
    device: Mapped[str] = mapped_column(String(32), default="auto")
    label: Mapped[str | None] = mapped_column(String(200))
    spec: Mapped[dict] = mapped_column(JSONType)
    spec_bytes: Mapped[int] = mapped_column(Integer, default=0)
    estimate: Mapped[dict | None] = mapped_column(JSONType)
    memory_bytes: Mapped[int | None] = mapped_column(BigInteger)
    credits_estimate: Mapped[float | None] = mapped_column(Float)
    queue: Mapped[str | None] = mapped_column(String(64))
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    stage: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[dict | None] = mapped_column(JSONType)
    warnings: Mapped[list | None] = mapped_column(JSONType)
    result_key: Mapped[str | None] = mapped_column(String(200))
    result_kind: Mapped[str | None] = mapped_column(String(32))
    result_bytes: Mapped[int | None] = mapped_column(BigInteger)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    device_used: Mapped[dict | None] = mapped_column(JSONType)
    runtime_s: Mapped[float | None] = mapped_column(Float)
    gpu_seconds: Mapped[float | None] = mapped_column(Float)
    credits: Mapped[float | None] = mapped_column(Float)
    event_seq: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobEvent(Base):
    __tablename__ = "job_events"
    __table_args__ = (UniqueConstraint("job_id", "seq"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    type: Mapped[str] = mapped_column(String(16))  # status | progress | log
    data: Mapped[dict] = mapped_column(JSONType)


class UsageRecord(Base):
    __tablename__ = "usage_records"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(String(200), ForeignKey("orgs.id"), index=True)
    user_id: Mapped[str | None] = mapped_column(String(200))
    job_id: Mapped[str] = mapped_column(String(32), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    runtime_s: Mapped[float] = mapped_column(Float, default=0.0)
    gpu_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    credits: Mapped[float] = mapped_column(Float, default=0.0)
    queue: Mapped[str | None] = mapped_column(String(64))
    stripe_reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stripe_error: Mapped[str | None] = mapped_column(String(500))


class StripeEvent(Base):
    """Processed webhook event ids, so redeliveries are applied once."""

    __tablename__ = "stripe_events"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    type: Mapped[str] = mapped_column(String(100))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Database:
    def __init__(self, url: str):
        kw: dict[str, Any] = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kw["connect_args"] = {"check_same_thread": False, "timeout": 30}
        self.engine = create_engine(url, **kw)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def _pragmas(conn, _):  # concurrent readers while a worker writes
                cur = conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def prepare(self, auto_migrate: bool) -> None:
        """Bring the schema to the latest migration (local mode), or check it is there (hosted)."""
        from . import migrate

        if auto_migrate:
            migrate.upgrade(self.engine)
        else:
            migrate.check_current(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self._sessions()
        try:
            yield s
            s.commit()
        except BaseException:
            s.rollback()
            raise
        finally:
            s.close()

    def dispose(self) -> None:
        self.engine.dispose()


def append_events(s: Session, job: Job, events: list[tuple[str, dict]]) -> None:
    """Append events to a job's log; `job` must be loaded in session `s`.

    The sequence numbers are reserved with one atomic UPDATE ... RETURNING, which also locks the job
    row until commit: the API (cancel) and a worker may append to the same job concurrently, and a
    read-increment-write of `job.event_seq` would hand both the same numbers."""
    if not events:
        return
    last = s.execute(update(Job).where(Job.id == job.id).values(event_seq=Job.event_seq + len(events))
                     .returning(Job.event_seq).execution_options(synchronize_session=False)).scalar_one()
    set_committed_value(job, "event_seq", last)
    for seq, (typ, data) in enumerate(events, start=last - len(events) + 1):
        s.add(JobEvent(job_id=job.id, seq=seq, type=typ, data=data))


def month_start(now: datetime | None = None) -> datetime:
    now = now or utcnow()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def credits_used(s: Session, org_id: str, since: datetime) -> float:
    q = select(func.coalesce(func.sum(UsageRecord.credits), 0.0)).where(
        UsageRecord.org_id == org_id, UsageRecord.created_at >= since)
    return float(s.execute(q).scalar_one())
