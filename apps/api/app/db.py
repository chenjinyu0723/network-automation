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
        manual_columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(manuals)").fetchall()
        }
        if "cli_profile" not in manual_columns:
            connection.exec_driver_sql(
                "ALTER TABLE manuals ADD COLUMN cli_profile VARCHAR(40) NOT NULL DEFAULT 'auto'"
            )
        device_plan_columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(device_plans)").fetchall()
        }
        if "rollback_json" not in device_plan_columns:
            connection.exec_driver_sql(
                "ALTER TABLE device_plans ADD COLUMN rollback_json TEXT NOT NULL DEFAULT '{}'"
            )
        execution_run_columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(execution_runs)").fetchall()
        }
        if "operation" not in execution_run_columns:
            connection.exec_driver_sql(
                "ALTER TABLE execution_runs ADD COLUMN operation VARCHAR(16) NOT NULL DEFAULT 'apply'"
            )
        config_task_columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(config_tasks)").fetchall()
        }
        if "planning_idea" not in config_task_columns:
            connection.exec_driver_sql(
                "ALTER TABLE config_tasks ADD COLUMN planning_idea TEXT NOT NULL DEFAULT ''"
            )
        if "planning_idea_revision" not in config_task_columns:
            connection.exec_driver_sql(
                "ALTER TABLE config_tasks ADD COLUMN planning_idea_revision INTEGER NOT NULL DEFAULT 0"
            )
        if "planning_idea_confirmed_at" not in config_task_columns:
            connection.exec_driver_sql(
                "ALTER TABLE config_tasks ADD COLUMN planning_idea_confirmed_at DATETIME"
            )
        if "cancel_requested" not in config_task_columns:
            connection.exec_driver_sql(
                "ALTER TABLE config_tasks ADD COLUMN cancel_requested BOOLEAN NOT NULL DEFAULT 0"
            )
        if "cancel_reason" not in config_task_columns:
            connection.exec_driver_sql("ALTER TABLE config_tasks ADD COLUMN cancel_reason TEXT")
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
        connection.exec_driver_sql(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS document_search USING fts5(
                document_id UNINDEXED,
                manual_id UNINDEXED,
                content,
                tokenize='unicode61'
            )
            """
        )
        # Existing manuals predate the page-level index.  Compare counts first:
        # a correlated lookup against an FTS virtual table becomes very slow for
        # a multi-thousand-page manual during every desktop startup.
        indexed_count = connection.exec_driver_sql("SELECT count(*) FROM document_search").scalar_one()
        document_count = connection.exec_driver_sql("SELECT count(*) FROM knowledge_documents").scalar_one()
        if indexed_count != document_count:
            connection.exec_driver_sql("DELETE FROM document_search")
            connection.exec_driver_sql(
                """
                INSERT INTO document_search(document_id, manual_id, content)
                SELECT id, manual_id,
                       trim(
                           coalesce(title, '') || char(10) || coalesce(toc_path_json, '') ||
                           char(10) || coalesce(text_content, '')
                       )
                FROM knowledge_documents
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
