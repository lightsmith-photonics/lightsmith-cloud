"""Database migrations (Alembic), shipped inside the package.

    lightsmith-server migrate      # upgrade the database to the latest revision

Local mode upgrades its SQLite file automatically at start-up. Hosted mode
(Postgres) expects migrations to run as a separate step, e.g. the compose
`migrate` service, and refuses to start on an out-of-date schema.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine

MIGRATIONS = Path(__file__).parent / "migrations"


def _config(engine: Engine) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.attributes["engine"] = engine
    return cfg


def head_revision() -> str:
    return ScriptDirectory(str(MIGRATIONS)).get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def upgrade(engine: Engine, revision: str = "head") -> None:
    command.upgrade(_config(engine), revision)


def check_current(engine: Engine) -> None:
    cur, head = current_revision(engine), head_revision()
    if cur != head:
        raise RuntimeError(f"database schema is at revision {cur!r}, expected {head!r}; "
                           "run `lightsmith-server migrate` first")


def autogenerate(engine: Engine, message: str, rev_id: str | None = None) -> None:
    """Developer helper: write a new revision from model changes (review it before committing)."""
    command.revision(_config(engine), message=message, autogenerate=True, rev_id=rev_id)
