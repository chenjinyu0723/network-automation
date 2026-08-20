from __future__ import annotations

import asyncio
import json

import app.retrieval.embeddings as embeddings
from app.db import Base
from app.models import (
    Command,
    CommandEmbedding,
    EmbeddingJob,
    ImportStatus,
    IndexStatus,
    KnowledgeDocument,
    Manual,
)
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


def test_embedding_job_completes_with_fts_fallback_after_provider_oom(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(embeddings, "SessionLocal", maker)
    embeddings._capacity_backoff_until.clear()

    class Settings:
        embedding_base_url = "http://embedding.example/v1/"
        embedding_model = "embedding-test"
        embedding_dimensions = None
        embedding_batch_size = 1

    async def oom_request(**_kwargs: object) -> list[list[float]]:
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(embeddings, "read_provider_settings", lambda _session: Settings())
    monkeypatch.setattr(embeddings, "get_provider_secret", lambda _provider: "sk-test")
    monkeypatch.setattr(embeddings, "request_embeddings", oom_request)

    with maker() as session:
        manual = Manual(
            original_filename="manual.chm",
            stored_path="manual.chm",
            source_sha256="b" * 64,
            file_format="chm",
            status=ImportStatus.completed,
        )
        session.add(manual)
        session.flush()
        document = KnowledgeDocument(
            manual_id=manual.id,
            source_path="vlan.html",
            text_content="x" * 8_000,
        )
        session.add(document)
        session.flush()
        session.add(
            Command(
                manual_id=manual.id,
                document_id=document.id,
                canonical_name="vlan batch",
                syntax_json=json.dumps(["vlan batch 10"]),
            )
        )
        job = EmbeddingJob(manual_id=manual.id, model="embedding-test", status=IndexStatus.queued)
        session.add(job)
        session.commit()
        job_id = job.id

    embeddings.run_embedding_job(job_id)

    with maker() as session:
        job = session.get(EmbeddingJob, job_id)
        assert job is not None
        assert job.status == IndexStatus.completed
        assert job.progress_current == 1
        assert job.progress_total == 1
        assert "FTS5/BM25" in (job.detail or "")
        assert session.query(CommandEmbedding).count() == 0


def test_semantic_query_oom_returns_empty_scores(monkeypatch, session) -> None:  # type: ignore[no-untyped-def]
    embeddings._capacity_backoff_until.clear()

    class Settings:
        embedding_base_url = "http://embedding.example/v1/"
        embedding_model = "embedding-test"
        embedding_dimensions = None
        embedding_batch_size = 1

    async def oom_request(**_kwargs: object) -> list[list[float]]:
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(embeddings, "read_provider_settings", lambda _session: Settings())
    monkeypatch.setattr(embeddings, "get_provider_secret", lambda _provider: "sk-test")
    monkeypatch.setattr(embeddings, "request_embeddings", oom_request)

    manual = Manual(
        original_filename="manual.chm",
        stored_path="manual.chm",
        source_sha256="c" * 64,
        file_format="chm",
        status=ImportStatus.completed,
    )
    session.add(manual)
    session.flush()
    document = KnowledgeDocument(manual_id=manual.id, source_path="vlan.html", text_content="vlan")
    session.add(document)
    session.flush()
    command = Command(manual_id=manual.id, document_id=document.id, canonical_name="vlan batch")
    session.add(command)
    session.flush()
    session.add(
        CommandEmbedding(
            command_id=command.id,
            manual_id=manual.id,
            model="embedding-test",
            dimensions=2,
            source_hash="d" * 64,
            vector_blob=b"\x00\x00\x80?\x00\x00\x00@",
        )
    )
    session.commit()

    scores = asyncio.run(
        embeddings.semantic_command_scores_many(
            session,
            manual_id=manual.id,
            queries=["vlan batch"],
            limit=5,
        )
    )

    assert scores == {"vlan batch": []}
