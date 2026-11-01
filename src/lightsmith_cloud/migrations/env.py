"""Alembic environment: uses the engine passed in by lightsmith_cloud.migrate."""

from alembic import context

from lightsmith_cloud.db import Base

target_metadata = Base.metadata


def run() -> None:
    engine = context.config.attributes["engine"]
    with engine.begin() as conn:
        context.configure(connection=conn, target_metadata=target_metadata, compare_type=True,
                          render_as_batch=conn.dialect.name == "sqlite")
        with context.begin_transaction():
            context.run_migrations()


run()
