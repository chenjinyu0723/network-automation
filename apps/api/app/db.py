from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import paths


class Base(DeclarativeBase):
    pass


def _enable_sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    # A local import worker and the Web API can briefly overlap.  Waiting is safer
    # than returning an arbitrary "database is locked" error to the user.
    cursor.execute("PRAGMA busy_timeout=15000")
    cursor.close()


paths.ensure()
engine = create_engine(
    f"sqlite:///{paths.database.as_posix()}",
    connect_args={"check_same_thread": False},
    future=True,
)
event.listen(engine, "connect", _enable_sqlite_pragmas)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


def init_database() -> None:
    # Imported here so model declarations are registered before metadata creation.
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        # This project is intentionally a local single-user application.  Keep the
        # small additive migrations here until Alembic is introduced, so existing
        # data/ databases are not silently discarded during development.
        import_job_columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(import_jobs)").fetchall()
        }
        if "worker_pid" not in import_job_columns:
            connection.exec_driver_sql("ALTER TABLE import_jobs ADD COLUMN worker_pid INTEGER")
        if "heartbeat_at" not in import_job_columns:
            connection.exec_driver_sql("ALTER TABLE import_jobs ADD COLUMN heartbeat_at DATETIME")
        device_plan_columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(device_plans)").fetchall()
        }
        if "rollback_json" not in device_plan_columns:
            connection.exec_driver_sql(
                "ALTER TABLE device_plans ADD COLUMN rollback_json TEXT NOT NULL DEFAULT '{}'"
            )
        connection.exec_driver_sql(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS command_search USING fts5(
                command_id UNINDEXED,
                manual_id UNINDEXED,
                content,
                tokenize='unicode61'
            )
            """
        )


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
