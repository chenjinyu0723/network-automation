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
from app.retrieval.embeddings import semantic_command_scores, semantic_command_scores_many


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
    semantic_scores: list[tuple[str, float]] | None = None,
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
    # A generic contains search can match hundreds of unrelated pages such as
    # ``display ... interface``.  Fetch an exact canonical command name first;
    # applying LIMIT before this lookup previously allowed it to disappear from
    # the candidate set entirely.
    exact_name_candidates = session.scalars(
        base.where(Command.canonical_name.ilike(clean_query))
    ).all()
    # Parsed manuals often place the literal command in ``syntax_json`` while
    # the canonical title contains a view suffix, e.g. ``ip address（接口视图）``.
    # Preserve an exact literal syntax token for those command pages as well.
    exact_syntax_candidates = session.scalars(
        base.where(Command.syntax_json.ilike(f'%"{clean_query}"%'))
    ).all()
    partial_name_candidates = session.scalars(
        base.where(
            or_(Command.canonical_name.ilike(f"%{clean_query}%"), Command.feature.ilike(f"%{clean_query}%"))
        )
        .order_by(Command.canonical_name)
        .limit(broad_limit)
    ).all()
    exact_name_ids = {item.id for item in exact_name_candidates}
    exact_syntax_ids = {item.id for item in exact_syntax_candidates}
    name_candidates = [
        *exact_name_candidates,
        *[item for item in exact_syntax_candidates if item.id not in exact_name_ids],
        *[
            item
            for item in partial_name_candidates
            if item.id not in exact_name_ids and item.id not in exact_syntax_ids
        ],
    ]

    ranked: dict[str, dict[str, float]] = {}
    for index, command in enumerate(name_candidates):
        if _compact(command.canonical_name) == _compact(clean_query):
            kind, score = "exact_name", 1.0
        elif command.id in exact_syntax_ids:
            kind, score = "exact_syntax", 0.92
        else:
            kind, score = "name", 0.78 - index * 0.002
        ranked.setdefault(command.id, {})[kind] = score

    for index, (command_id, _bm25) in enumerate(_fts_candidates(clean_query, broad_limit), start=1):
        ranked.setdefault(command_id, {})["fts5"] = max(0.15, 0.68 - (index - 1) * 0.012)

    if semantic_scores is not None:
        semantic = semantic_scores
    elif use_semantic and manual_id:
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
    else:
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


def hybrid_command_search_many(
    session: Session,
    *,
    queries: list[str],
    manual_id: str | None = None,
    model_id: str | None = None,
    limit: int = 20,
    use_semantic: bool = True,
) -> dict[str, list[HybridCommandHit]]:
    """Run one retrieval round while batching the optional semantic branch."""

    unique_queries = list(dict.fromkeys(item.strip() for item in queries if item.strip()))
    semantic_by_query: dict[str, list[tuple[str, float]]] = {}
    if use_semantic and manual_id and unique_queries:
        try:
            semantic_by_query = _run_async(
                semantic_command_scores_many(
                    session,
                    manual_id=manual_id,
                    queries=unique_queries,
                    limit=max(limit * 5, 30),
                )
            )
        except Exception:
            semantic_by_query = {}
    return {
        query: hybrid_command_search(
            session,
            query=query,
            manual_id=manual_id,
            model_id=model_id,
            limit=limit,
            use_semantic=False,
            semantic_scores=semantic_by_query.get(query, []),
        )
        for query in unique_queries
    }
