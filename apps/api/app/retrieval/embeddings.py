from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
import time
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
DEFAULT_EMBEDDING_DOCUMENT_CHARS = 4_000
FALLBACK_EMBEDDING_DOCUMENT_CHARS = 600
EMBEDDING_CAPACITY_COOLDOWN_SECONDS = 60
_capacity_backoff_until: dict[tuple[str, str], float] = {}


def is_embedding_capacity_error(exc: BaseException) -> bool:
    """Recognise provider-side OOM responses without binding to one vendor."""

    detail = str(exc).casefold()
    markers = (
        "out of memory",
        "outofmemory",
        "cuda out of memory",
        "cuda error",
        "gpu memory",
        "gpu oom",
        "oom",
        "显存",
        "内存不足",
        "memory exhausted",
        "resource exhausted",
    )
    return any(marker in detail for marker in markers)


def _provider_key(base_url: str, model: str) -> tuple[str, str]:
    return (base_url.strip().rstrip("/"), model.strip())


def _mark_capacity_unavailable(base_url: str, model: str) -> None:
    _capacity_backoff_until[_provider_key(base_url, model)] = (
        time.monotonic() + EMBEDDING_CAPACITY_COOLDOWN_SECONDS
    )


def _capacity_is_in_backoff(base_url: str, model: str) -> bool:
    key = _provider_key(base_url, model)
    until = _capacity_backoff_until.get(key, 0.0)
    if until <= time.monotonic():
        _capacity_backoff_until.pop(key, None)
        return False
    return True


def _text_for_command(
    command: Command,
    document_chars: int = DEFAULT_EMBEDDING_DOCUMENT_CHARS,
) -> str:
    return "\n".join(
        [
            command.canonical_name,
            command.feature or "",
            command.syntax_json,
            command.preconditions_json,
            command.constraints_json,
            command.document.text_content[:document_chars],
        ]
    )


def _store_vector(
    session: Session,
    *,
    command: Command,
    model: str,
    text: str,
    vector: list[float],
) -> None:
    array = np.asarray(vector, dtype=np.float32)
    if array.ndim != 1 or array.size == 0:
        raise ValueError("Embedding 接口返回空向量。")
    source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    existing = session.scalar(
        select(CommandEmbedding).where(
            CommandEmbedding.command_id == command.id,
            CommandEmbedding.model == model,
        )
    )
    if existing:
        existing.dimensions = int(array.size)
        existing.source_hash = source_hash
        existing.vector_blob = array.tobytes()
        return
    session.add(
        CommandEmbedding(
            command_id=command.id,
            manual_id=command.manual_id,
            model=model,
            dimensions=int(array.size),
            source_hash=source_hash,
            vector_blob=array.tobytes(),
        )
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


def create_embedding_job(manual_id: str) -> tuple[EmbeddingJob, bool]:
    """Create one index job, or return the active job for the same manual/model."""

    with SessionLocal() as session:
        settings = read_provider_settings(session)
        if (
            not settings.embedding_base_url
            or not settings.embedding_model
            or not get_provider_secret("embedding")
        ):
            raise ValueError("请先在设置页保存 Embedding Base URL、模型和 API Key。")
        existing = session.scalar(
            select(EmbeddingJob)
            .where(
                EmbeddingJob.manual_id == manual_id,
                EmbeddingJob.model == settings.embedding_model,
                EmbeddingJob.status.in_([IndexStatus.queued, IndexStatus.running]),
            )
            .order_by(EmbeddingJob.created_at.desc())
        )
        if existing:
            return existing, False
        job = EmbeddingJob(manual_id=manual_id, model=settings.embedding_model, status=IndexStatus.queued)
        session.add(job)
        session.commit()
        session.refresh(job)
        return job, True


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
            successful = 0
            skipped = 0
            capacity_exhausted = False

            def update_progress(processed: int) -> None:
                job.progress_current = processed
                if capacity_exhausted:
                    job.detail = (
                        f"Embedding 服务显存/内存不足；已写入 {successful} 条，"
                        f"跳过 {skipped} 条。后续检索将自动使用 FTS5/BM25。"
                    )
                else:
                    job.detail = f"已处理 {processed}/{len(commands)} 条，已生成 {successful} 条命令向量"
                session.commit()

            # The configured OpenAI-compatible endpoint accepts at most 20 inputs
            # per request. A capacity failure is not allowed to block handbook
            # use: retry its first command with a compact context, then retain
            # FTS5/BM25 for the remainder if the endpoint still cannot embed one.
            for start in range(0, len(commands), batch_size):
                batch = commands[start : start + batch_size]
                if capacity_exhausted:
                    skipped += len(batch)
                    update_progress(min(start + len(batch), len(commands)))
                    continue
                texts = [_text_for_command(command) for command in batch]
                try:
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
                        _store_vector(
                            session,
                            command=command,
                            model=job.model,
                            text=text,
                            vector=vector,
                        )
                        successful += 1
                except Exception as exc:
                    if not is_embedding_capacity_error(exc):
                        raise
                    # Batch one is the default. For a large command page, a
                    # smaller evidence window can still fit a constrained model.
                    command = batch[0]
                    compact_text = _text_for_command(command, FALLBACK_EMBEDDING_DOCUMENT_CHARS)
                    try:
                        vectors = asyncio.run(
                            request_embeddings(
                                base_url=settings.embedding_base_url,
                                api_key=secret,
                                model=job.model,
                                inputs=[compact_text],
                                dimensions=settings.embedding_dimensions,
                            )
                        )
                        if len(vectors) != 1:
                            raise ValueError("Embedding 接口返回数量与输入不一致。")
                        _store_vector(
                            session,
                            command=command,
                            model=job.model,
                            text=compact_text,
                            vector=vectors[0],
                        )
                        successful += 1
                        skipped += len(batch) - 1
                    except Exception as compact_exc:
                        if not is_embedding_capacity_error(compact_exc):
                            raise
                        capacity_exhausted = True
                        _mark_capacity_unavailable(settings.embedding_base_url, job.model)
                        skipped += len(batch)
                update_progress(min(start + len(batch), len(commands)))
            job.status = IndexStatus.completed
            if capacity_exhausted:
                job.detail = (
                    f"部分完成：已写入 {successful}/{len(commands)} 条命令向量；"
                    f"因 Embedding 服务显存/内存不足跳过 {skipped} 条，检索会自动使用 FTS5/BM25。"
                )
            else:
                job.detail = f"完成：{successful}/{len(commands)} 条命令向量"
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
    if _capacity_is_in_backoff(settings.embedding_base_url, settings.embedding_model):
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
        try:
            vectors.extend(
                await request_embeddings(
                    base_url=settings.embedding_base_url,
                    api_key=secret,
                    model=settings.embedding_model,
                    inputs=unique_queries[start : start + batch_size],
                    dimensions=settings.embedding_dimensions,
                )
            )
        except Exception as exc:
            if is_embedding_capacity_error(exc):
                _mark_capacity_unavailable(settings.embedding_base_url, settings.embedding_model)
                # Semantic ranking is optional. Returning empty results lets the
                # caller preserve exact-name and FTS5/BM25 evidence retrieval.
                return {query: [] for query in unique_queries}
            raise
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
