from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import paths
from app.db import SessionLocal
from app.llm.client import request_embeddings
from app.models import Command, CommandEmbedding, EmbeddingJob, IndexStatus
from app.services.settings import get_provider_secret, read_provider_settings

MAX_EMBEDDING_BATCH_SIZE = 20


def _text_for_command(command: Command) -> str:
    return "\n".join(
        [
            command.canonical_name,
            command.feature or "",
            command.syntax_json,
            command.preconditions_json,
            command.constraints_json,
            command.document.text_content[:12_000],
        ]
    )


def start_embedding_worker(job_id: str) -> None:
    log_path = paths.logs / f"embedding-{job_id}.log"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--embedding-worker", "--job-id", job_id]
        worker_cwd = paths.data_root
    else:
        command = [sys.executable, "-m", "app.retrieval.worker", "--job-id", job_id]
        worker_cwd = Path(__file__).resolve().parents[2]
    with log_path.open("a", encoding="utf-8") as log_file:
        subprocess.Popen(  # noqa: S603 - fixed module invocation
            command,
            cwd=worker_cwd,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )


def create_embedding_job(manual_id: str) -> EmbeddingJob:
    with SessionLocal() as session:
        settings = read_provider_settings(session)
        if (
            not settings.embedding_base_url
            or not settings.embedding_model
            or not get_provider_secret("embedding")
        ):
            raise ValueError("请先在设置页保存 Embedding Base URL、模型和 API Key。")
        job = EmbeddingJob(manual_id=manual_id, model=settings.embedding_model, status=IndexStatus.queued)
        session.add(job)
        session.commit()
        session.refresh(job)
        return job


def run_embedding_job(job_id: str) -> None:
    with SessionLocal() as session:
        job = session.get(EmbeddingJob, job_id)
        if not job or job.status == IndexStatus.running:
            return
        settings = read_provider_settings(session)
        secret = get_provider_secret("embedding")
        if not settings.embedding_base_url or not settings.embedding_model or not secret:
            job.status = IndexStatus.failed
            job.detail = "Embedding 配置不完整。"
            job.finished_at = datetime.utcnow()
            session.commit()
            return
        job.status = IndexStatus.running
        job.worker_pid = os.getpid()
        job.started_at = datetime.utcnow()
        session.commit()
        try:
            commands = session.scalars(
                select(Command).where(Command.manual_id == job.manual_id).order_by(Command.id)
            ).all()
            job.progress_total = len(commands)
            session.commit()
            # Read once so a settings change cannot alter an already running task.
            batch_size = min(max(int(settings.embedding_batch_size), 1), MAX_EMBEDDING_BATCH_SIZE)
            # The configured OpenAI-compatible endpoint accepts at most 20 inputs
            # per request. The user-configurable batch remains within that limit.
            for start in range(0, len(commands), batch_size):
                batch = commands[start : start + batch_size]
                texts = [_text_for_command(command) for command in batch]
                vectors = asyncio.run(
                    request_embeddings(
                        base_url=settings.embedding_base_url,
                        api_key=secret,
                        model=job.model,
                        inputs=texts,
                        dimensions=settings.embedding_dimensions,
                    )
                )
                if len(vectors) != len(batch):
                    raise ValueError("Embedding 接口返回数量与输入不一致。")
                for command, text, vector in zip(batch, texts, vectors, strict=True):
                    array = np.asarray(vector, dtype=np.float32)
                    if array.ndim != 1 or array.size == 0:
                        raise ValueError("Embedding 接口返回空向量。")
                    source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    existing = session.scalar(
                        select(CommandEmbedding).where(
                            CommandEmbedding.command_id == command.id,
                            CommandEmbedding.model == job.model,
                        )
                    )
                    if existing:
                        existing.dimensions = int(array.size)
                        existing.source_hash = source_hash
                        existing.vector_blob = array.tobytes()
                    else:
                        session.add(
                            CommandEmbedding(
                                command_id=command.id,
                                manual_id=command.manual_id,
                                model=job.model,
                                dimensions=int(array.size),
                                source_hash=source_hash,
                                vector_blob=array.tobytes(),
                            )
                        )
                job.progress_current = min(start + len(batch), len(commands))
                job.detail = f"已生成 {job.progress_current}/{len(commands)} 条命令向量"
                session.commit()
            job.status = IndexStatus.completed
            job.detail = f"完成：{len(commands)} 条命令向量"
        except Exception as exc:
            session.rollback()
            job = session.get(EmbeddingJob, job_id)
            if job:
                job.status = IndexStatus.failed
                job.detail = str(exc)[:4000]
        if job:
            job.worker_pid = None
            job.finished_at = datetime.utcnow()
            session.commit()


async def semantic_command_scores(
    session: Session,
    *,
    manual_id: str,
    query: str,
    limit: int,
) -> list[tuple[str, float]]:
    """Return CPU cosine matches only when a compatible local index exists."""

    return (await semantic_command_scores_many(
        session,
        manual_id=manual_id,
        queries=[query],
        limit=limit,
    )).get(query, [])


async def semantic_command_scores_many(
    session: Session,
    *,
    manual_id: str,
    queries: list[str],
    limit: int,
) -> dict[str, list[tuple[str, float]]]:
    """Score a retrieval round with one Embedding request and one CPU scan.

    Active retrieval asks up to three independent handbook questions per round.
    Sending those questions to an OpenAI-compatible embedding endpoint one at a
    time dominated planning latency.  The endpoint already accepts an input
    array, so batch them while keeping each query's ranked result independent.
    """

    settings = read_provider_settings(session)
    secret = get_provider_secret("embedding")
    unique_queries = list(dict.fromkeys(item.strip() for item in queries if item.strip()))
    if not unique_queries or not settings.embedding_base_url or not settings.embedding_model or not secret:
        return {query: [] for query in unique_queries}
    rows = session.scalars(
        select(CommandEmbedding).where(
            CommandEmbedding.manual_id == manual_id,
            CommandEmbedding.model == settings.embedding_model,
        )
    ).all()
    if not rows:
        return {query: [] for query in unique_queries}
    # Reuse the user-configured provider batch size for retrieval queries as
    # well as handbook indexing. This is sequential batching, not concurrent
    # fan-out: a constrained local embedding endpoint can stay at batch 1 or
    # 2, while providers that accept larger arrays can finish the whole active
    # retrieval round in one request.
    batch_size = min(max(int(settings.embedding_batch_size), 1), MAX_EMBEDDING_BATCH_SIZE)
    vectors: list[list[float]] = []
    for start in range(0, len(unique_queries), batch_size):
        vectors.extend(
            await request_embeddings(
                base_url=settings.embedding_base_url,
                api_key=secret,
                model=settings.embedding_model,
                inputs=unique_queries[start : start + batch_size],
                dimensions=settings.embedding_dimensions,
            )
        )
    if len(vectors) != len(unique_queries):
        raise ValueError("Embedding 接口返回数量与主动检索查询数量不一致。")
    query_vectors = [np.asarray(vector, dtype=np.float32) for vector in vectors]
    matching_rows: list[CommandEmbedding] = []
    indexed_vectors: list[np.ndarray] = []
    for row in rows:
        vector = np.frombuffer(row.vector_blob, dtype=np.float32)
        if vector.size and all(vector.size == query_vector.size for query_vector in query_vectors):
            matching_rows.append(row)
            indexed_vectors.append(vector)
    if not indexed_vectors:
        return {query: [] for query in unique_queries}
    matrix = np.stack(indexed_vectors)
    vector_norms = np.linalg.norm(matrix, axis=1)
    results: dict[str, list[tuple[str, float]]] = {}
    for query, query_vector in zip(unique_queries, query_vectors, strict=True):
        query_norm = float(np.linalg.norm(query_vector))
        if not query_norm:
            results[query] = []
            continue
        denominators = vector_norms * query_norm
        scores = np.divide(
            matrix @ query_vector,
            denominators,
            out=np.zeros_like(denominators, dtype=np.float32),
            where=denominators != 0,
        )
        ranked = sorted(
            (
                (row.command_id, float(score))
                for row, score in zip(matching_rows, scores, strict=True)
                if np.isfinite(score)
            ),
            key=lambda item: item[1],
            reverse=True,
        )[:limit]
        results[query] = ranked
    return results
