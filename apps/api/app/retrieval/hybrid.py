"""SQLite FTS5 + optional Embedding CPU hybrid command retrieval."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.db import engine
from app.models import Command
from app.retrieval.embeddings import semantic_command_scores


@dataclass(frozen=True)
class HybridCommandHit:
    command: Command
    score: float
    sources: tuple[str, ...]


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _run_async(coroutine):  # type: ignore[no-untyped-def]
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coroutine).result()


def _fts_query(query: str) -> str | None:
    # Keep only plain terms so user input cannot become an FTS expression.
    terms = re.findall(r"[\w\u4e00-\u9fff-]+", query, re.UNICODE)
    return " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms[:12]) or None


def _fts_candidates(query: str, limit: int) -> list[tuple[str, float]]:
    expression = _fts_query(query)
    if not expression:
        return []
    try:
        with engine.connect() as connection:
            rows = connection.exec_driver_sql(
                """
                SELECT command_id, bm25(command_search) AS score
                FROM command_search
                WHERE command_search MATCH ?
                ORDER BY score
                LIMIT ?
                """,
                (expression, limit),
            ).fetchall()
    except SQLAlchemyError:
        # In-memory unit tests do not initialise the app-owned FTS virtual table.
        return []
    return [(str(row[0]), float(row[1])) for row in rows]


def hybrid_command_search(
    session: Session,
    *,
    query: str,
    manual_id: str | None = None,
    model_id: str | None = None,
    limit: int = 20,
    use_semantic: bool = True,
) -> list[HybridCommandHit]:
    """Merge exact-name, FTS5 and optional CPU cosine results deterministically.

    Manual and explicit negative-applicability filters are hard filters.  An
    unavailable Embedding endpoint/index simply removes the semantic branch;
    exact-name and FTS5 retrieval remain usable.
    """

    clean_query = query.strip()
    if not clean_query:
        return []
    broad_limit = max(limit * 5, 30)
    base = select(Command).options(selectinload(Command.applicability))
    if manual_id:
        base = base.where(Command.manual_id == manual_id)
    name_candidates = session.scalars(
        base.where(
            or_(Command.canonical_name.ilike(f"%{clean_query}%"), Command.feature.ilike(f"%{clean_query}%"))
        ).limit(broad_limit)
    ).all()

    ranked: dict[str, dict[str, float]] = {}
    for index, command in enumerate(name_candidates):
        kind = "exact_name" if _compact(command.canonical_name) == _compact(clean_query) else "name"
        ranked.setdefault(command.id, {})[kind] = 1.0 if kind == "exact_name" else 0.78 - index * 0.002

    for index, (command_id, _bm25) in enumerate(_fts_candidates(clean_query, broad_limit), start=1):
        ranked.setdefault(command_id, {})["fts5"] = max(0.15, 0.68 - (index - 1) * 0.012)

    if use_semantic and manual_id:
        try:
            semantic = _run_async(
                semantic_command_scores(
                    session,
                    manual_id=manual_id,
                    query=clean_query,
                    limit=broad_limit,
                )
            )
        except Exception:
            semantic = []
        for index, (command_id, cosine) in enumerate(semantic, start=1):
            # Cosine uses the complete local manual index; rank decay prevents a
            # weak tail of approximate matches from outranking exact command names.
            scaled = max(0.0, min(1.0, (cosine + 1.0) / 2.0))
            ranked.setdefault(command_id, {})["embedding_cpu"] = max(
                0.10, 0.62 * scaled - (index - 1) * 0.008
            )

    if not ranked:
        return []
    commands = {
        item.id: item
        for item in session.scalars(
            select(Command).options(selectinload(Command.applicability)).where(Command.id.in_(ranked))
        ).all()
    }
    hits: list[HybridCommandHit] = []
    for command_id, source_scores in ranked.items():
        command = commands.get(command_id)
        if not command or (manual_id and command.manual_id != manual_id):
            continue
        if model_id:
            explicit = next((entry for entry in command.applicability if entry.model_id == model_id), None)
            if explicit and not explicit.is_supported:
                continue
        # Prefer agreement across retrievers without allowing it to exceed an
        # exact command-name result.
        score = min(1.0, max(source_scores.values()) + 0.08 * (len(source_scores) - 1))
        hits.append(
            HybridCommandHit(
                command=command,
                score=score,
                sources=tuple(sorted(source_scores)),
            )
        )
    return sorted(hits, key=lambda hit: (-hit.score, hit.command.canonical_name))[:limit]
