from __future__ import annotations

import app.retrieval.embeddings as embeddings
from app.db import Base
from app.models import EmbeddingJob, ImportStatus, IndexStatus, Manual
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def test_create_embedding_job_reuses_an_active_job(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(embeddings, "SessionLocal", maker)

    class Settings:
        embedding_base_url = "http://embedding.example/v1/"
        embedding_model = "embedding-test"

    monkeypatch.setattr(embeddings, "read_provider_settings", lambda _session: Settings())
    monkeypatch.setattr(embeddings, "get_provider_secret", lambda _provider: "sk-test")

    with maker() as session:
        manual = Manual(
            original_filename="manual.html",
            stored_path="manual.html",
            source_sha256="a" * 64,
            file_format="html",
            status=ImportStatus.completed,
        )
        session.add(manual)
        session.commit()
        manual_id = manual.id

    first, first_created = embeddings.create_embedding_job(manual_id)
    second, second_created = embeddings.create_embedding_job(manual_id)

    assert first.id == second.id
    assert first_created is True
    assert second_created is False
    assert second.status == IndexStatus.queued
    with maker() as session:
        assert session.query(EmbeddingJob).count() == 1
