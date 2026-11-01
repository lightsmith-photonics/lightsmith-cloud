"""Job event sequence numbers under concurrent writers (the API's cancel and a worker)."""

from __future__ import annotations

import threading

from sqlalchemy import select

from lightsmith_cloud.auth import ensure_org
from lightsmith_cloud.db import Database, Job, JobEvent, append_events


def test_concurrent_appends_get_distinct_contiguous_seqs(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'events.db'}")
    db.prepare(auto_migrate=True)
    with db.session() as s:
        ensure_org(s, "org-1")
        job = Job(org_id="org-1", kind="solve", spec={})
        s.add(job)
    job_id, writers, per_writer = job.id, 8, 25
    start, errors = threading.Barrier(writers), []

    def write(n: int) -> None:
        try:
            start.wait()
            for i in range(per_writer):
                with db.session() as s:  # one transaction per append, like the API and the executor
                    append_events(s, s.get(Job, job_id), [("log", {"writer": n, "i": i})])
        except Exception as e:  # noqa: BLE001 - reported below
            errors.append(e)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors[0]
    with db.session() as s:
        seqs = s.scalars(select(JobEvent.seq).where(JobEvent.job_id == job_id).order_by(JobEvent.seq)).all()
        assert seqs == list(range(1, writers * per_writer + 1))
        assert s.get(Job, job_id).event_seq == writers * per_writer
    db.dispose()
