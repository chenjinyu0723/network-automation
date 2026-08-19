"""LLM-guided manual retrieval with explicit, evidence-only search steps."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from threading import Event
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import engine
from app.llm.client import (
    json_format_repair_prompt,
    parse_json_response,
    request_text_result,
    should_enable_thinking,
)
from app.models import Command, KnowledgeDocument
from app.planning.runtime import PlanningCancelled
from app.retrieval.hybrid import hybrid_command_search_many
from app.schemas import LlmManualRetrievalDecision
from app.services.settings import get_provider_secret, read_provider_settings

# The explicit ReAct loop is deliberately short: two focused hybrid-search
# rounds are enough to find the important command pages without flooding a
# small model with duplicate context. Search execution stays in application
# code; the model only returns an auditable JSON decision.
MAX_ROUNDS = 2
DEFAULT_ROUNDS = 2
MAX_QUERIES_PER_ROUND = 5
MAX_CANDIDATES = 36
PER_QUERY_CANDIDATES = 3
LLM_CANDIDATE_PAGE_LIMIT = 24


def _run_async(coroutine):  # type: ignore[no-untyped-def]
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coroutine).result()


def _compact_query(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _command_phrase(value: str) -> str:
    """Keep the leading command-shaped keywords from an LLM follow-up query.

    A model often asks for a mixed-language phrase such as ``OSPF network
    配置命令``.  The imported catalog is most precise for its literal keyword
    stem (``OSPF network``), while unrelated descriptive tail words can dilute
    a hybrid result.  This is intentionally vendor-neutral and does not assume
    that the resulting phrase is a valid CLI by itself.
    """

    clean = _compact_query(value)
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_-]*", clean)
    return " ".join(tokens[:2]) if len(tokens) >= 2 else clean


def _manual_command_anchors(
    session: Session,
    *,
    manual_id: str,
    seed_queries: list[str],
) -> list[str]:
    """Extract exact command-shaped phrases from this manual's own catalog.

    LLM terms are often descriptive, for example ``OSPF configuration`` or
    ``network advertisement``. Searching those phrases semantically remains
    useful, but the command catalog can also tell us that ``ospf`` and
    ``network`` are literal, searchable handbook commands. This is dynamic per
    imported manual; it deliberately contains no vendor or feature allow-list.
    """

    phrases: list[str] = []
    seen_phrases: set[str] = set()
    for query in seed_queries:
        clean = _compact_query(query)
        if not clean:
            continue
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9_-]*", clean)
        candidates = [clean]
        candidates.extend(" ".join(tokens[index : index + 2]) for index in range(len(tokens) - 1))
        candidates.extend(tokens)
        for candidate in candidates:
            normalized = _compact_query(candidate)
            key = normalized.casefold()
            if normalized and key not in seen_phrases:
                seen_phrases.add(key)
                phrases.append(normalized)

    anchors: list[str] = []
    for phrase in phrases:
        catalog_match = Command.canonical_name.ilike(phrase)
        # Single-token syntax matches are excessively broad (for example,
        # ``configuration`` can occur inside descriptive syntax). A single
        # token must therefore be an actual page title; a multi-token literal
        # such as ``ip address`` may also be recovered from syntax.
        if len(phrase.split()) > 1:
            catalog_match = or_(catalog_match, Command.syntax_json.ilike(f'%"{phrase}"%'))
        elif len(phrase) >= 3:
            # A requirement commonly uses a protocol/family word (``VRRP``,
            # ``OSPF``, ``stack``) while the manual indexes individual
            # subcommands beneath it. A title-prefix match remains catalogue
            # grounded and avoids the broad syntax match used for multi-word
            # command phrases.
            catalog_match = or_(catalog_match, Command.canonical_name.ilike(f"{phrase}%"))
        try:
            found = session.scalar(
                select(Command.id).where(Command.manual_id == manual_id).where(catalog_match).limit(1)
            )
        except (AttributeError, SQLAlchemyError):
            # Lightweight fake sessions used by unit tests do not expose the
            # catalog query surface; their supplied seed terms still work.
            return []
        if found:
            anchors.append(phrase)

    # A requirement normally names a protocol or capability family (for
    # example ``VRRP``), whereas the actual command reference is indexed under
    # a more specific root such as ``vrrp vrid``.  Preserve the human term,
    # then add the shortest catalogue-native title below it as a second search
    # anchor.  This is directory navigation over the imported manual, not a
    # vendor-specific command map: a different handbook can expose a different
    # child title or none at all.
    family_roots: dict[str, str] = {}
    for phrase in anchors:
        if len(phrase.split()) != 1 or len(phrase) < 3:
            continue
        try:
            matches = session.scalars(
                select(Command.canonical_name)
                .where(Command.manual_id == manual_id)
                .where(Command.canonical_name.ilike(f"{phrase}%"))
                .order_by(func.length(Command.canonical_name), Command.canonical_name)
                .limit(24)
            ).all()
        except (AttributeError, SQLAlchemyError):
            return list(dict.fromkeys(anchors))
        normalized_matches = [
            _compact_query(str(value).split("（", 1)[0].split("(", 1)[0]) for value in matches
        ]
        candidates = [
            value
            for value in normalized_matches
            if len(value.split()) >= 2
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*(?:\s+[A-Za-z][A-Za-z0-9_-]*)*", value)
        ]
        if candidates:
            family_roots[phrase.casefold()] = min(
                candidates, key=lambda value: (len(value.split()), len(value), value)
            )
    # Interleave each family root with its broad family name. The active loop
    # has a finite search budget, so appending all discovered roots after all
    # generic nouns could still push a high-value root (for example ``vrrp
    # vrid``) beyond the final round.
    ordered: list[str] = []
    for phrase in anchors:
        ordered.append(phrase)
        root = family_roots.get(phrase.casefold())
        if root:
            ordered.append(root)
    return list(dict.fromkeys(ordered))


def _fts_query(query: str) -> str | None:
    terms = re.findall(r"[\w\u4e00-\u9fff-]+", query, re.UNICODE)
    return " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms[:12]) or None


def _document_fts_ids(query: str, manual_id: str, limit: int) -> list[tuple[str, float]]:
    expression = _fts_query(query)
    if not expression:
        return []
    try:
        with engine.connect() as connection:
            rows = connection.exec_driver_sql(
                """
                SELECT document_id, bm25(document_search) AS score
                FROM document_search
                WHERE document_search MATCH ? AND manual_id = ?
                ORDER BY score
                LIMIT ?
                """,
                (expression, manual_id, limit),
            ).fetchall()
    except SQLAlchemyError:
        return []
    return [(str(row[0]), float(row[1])) for row in rows]


def _excerpt(text: str, limit: int = 700) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    return normalized[:limit]


def search_manual_candidates(
    session: Session,
    *,
    manual_id: str,
    query: str,
    limit: int = MAX_CANDIDATES,
) -> list[dict[str, Any]]:
    """Return command-page semantic hits plus all-page lexical hits.

    Command embeddings already include the corresponding complete manual page.
    The document FTS branch adds non-command pages such as concepts, tables of
    contents and PDF pages without requiring a second full vector index.
    """

    return search_manual_candidates_many(
        session,
        manual_id=manual_id,
        queries=[query],
        limit=limit,
    ).get(query, [])


def search_manual_candidates_many(
    session: Session,
    *,
    manual_id: str,
    queries: list[str],
    limit: int = MAX_CANDIDATES,
) -> dict[str, list[dict[str, Any]]]:
    """Build independent candidate lists while sharing one semantic batch."""

    unique_queries = list(dict.fromkeys(item.strip() for item in queries if item.strip()))
    # Ask the local rank merger for a wider pool, then apply the active-search
    # evidence ranking below. This costs no additional embedding request; it
    # prevents broad FTS-only pages from pruning lower-scored but semantically
    # relevant command-family variants before the ReAct node can inspect them.
    hybrid_hits = hybrid_command_search_many(
        session,
        queries=unique_queries,
        manual_id=manual_id,
        limit=max(limit * 2, 32),
    )
    results: dict[str, list[dict[str, Any]]] = {}
    for query in unique_queries:
        candidates: dict[str, dict[str, Any]] = {}
        for hit in hybrid_hits.get(query, []):
            command = hit.command
            document = command.document
            candidates[f"command:{command.id}"] = {
                "kind": "command",
                "command_id": command.id,
                "document_id": document.id,
                "canonical_name": command.canonical_name,
                "syntax": json.loads(command.syntax_json),
                "views": json.loads(command.views_json),
                "parameters": json.loads(command.parameters_json),
                "preconditions": json.loads(command.preconditions_json),
                "constraints": json.loads(command.constraints_json),
                "examples": json.loads(command.examples_json),
                "source_path": document.source_path,
                "title": document.title or command.canonical_name,
                "excerpt": _excerpt(document.text_content),
                "score": round(hit.score, 4),
                "retrieval_sources": list(hit.sources),
            }

        document_scores = _document_fts_ids(query, manual_id, limit)
        if document_scores:
            documents = {
                item.id: item
                for item in session.scalars(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.id.in_([item[0] for item in document_scores])
                    )
                ).all()
            }
            for index, (document_id, _score) in enumerate(document_scores, start=1):
                document = documents.get(document_id)
                if not document:
                    continue
                linked_commands = list(document.commands)
                score = max(0.15, 0.68 - (index - 1) * 0.012)
                if len(linked_commands) == 1:
                    command = linked_commands[0]
                    key = f"command:{command.id}"
                    existing = candidates.get(key)
                    if existing:
                        existing["score"] = max(float(existing["score"]), score)
                        existing["retrieval_sources"] = sorted(
                            set(existing["retrieval_sources"]) | {"document_fts5"}
                        )
                        continue
                    candidates[key] = {
                        "kind": "command",
                        "command_id": command.id,
                        "document_id": document.id,
                        "canonical_name": command.canonical_name,
                        "syntax": json.loads(command.syntax_json),
                        "views": json.loads(command.views_json),
                        "parameters": json.loads(command.parameters_json),
                        "preconditions": json.loads(command.preconditions_json),
                        "constraints": json.loads(command.constraints_json),
                        "examples": json.loads(command.examples_json),
                        "source_path": document.source_path,
                        "title": document.title or command.canonical_name,
                        "excerpt": _excerpt(document.text_content),
                        "score": round(score, 4),
                        "retrieval_sources": ["document_fts5"],
                    }
                    continue
                candidates[f"document:{document.id}"] = {
                    "kind": "document",
                    "command_id": None,
                    "document_id": document.id,
                    "canonical_name": None,
                    "syntax": [],
                    "source_path": document.source_path,
                    "title": document.title or document.source_path,
                    "excerpt": _excerpt(document.text_content),
                    "score": round(score, 4),
                    "retrieval_sources": ["document_fts5"],
                }

        def candidate_rank(item: dict[str, Any]) -> tuple[int, int, float, str]:
            sources = set(item["retrieval_sources"])
            exact_rank = 0 if "exact_name" in sources else (1 if "exact_syntax" in sources else 2)
            # Long natural-language requirements often make FTS5 match many
            # incidental Chinese words. A configured embedding hit carries the
            # intent-level relation needed to surface a command family member
            # such as ``vrrp vrid priority``. Exact catalogue/syntax matches
            # remain stronger than both branches.
            semantic_rank = 0 if "embedding_cpu" in sources else 1
            return (
                exact_rank,
                semantic_rank,
                -float(item["score"]),
                str(item["canonical_name"] or item["title"]),
            )

        results[query] = sorted(candidates.values(), key=candidate_rank)[:limit]
    return results


def _decision_prompt(
    *,
    requirement: str,
    topology_context: dict[str, Any],
    confirmed_idea: str,
    known_actions: list[str],
    round_number: int,
    previously_selected: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> list[dict[str, str]]:
    def compact_page(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "command_id": item.get("command_id"),
            "name": item.get("canonical_name"),
            "syntax": item.get("syntax", [])[:6],
            "views": item.get("views", [])[:4],
            "parameters": item.get("parameters", [])[:6],
            "preconditions": item.get("preconditions", [])[:3],
            "constraints": item.get("constraints", [])[:3],
            "examples": item.get("examples", [])[:2],
            "source_path": item.get("source_path"),
            "title": item.get("title"),
            "excerpt": str(item.get("excerpt") or "")[:320],
            "sources": item.get("retrieval_sources", []),
        }

    compact_candidates = [compact_page(item) for item in candidates[:LLM_CANDIDATE_PAGE_LIMIT]]
    compact_selected = [compact_page(item) for item in previously_selected[:LLM_CANDIDATE_PAGE_LIMIT]]
    query_instruction = (
        "这是第一轮：next_queries 最多 5 条；优先选择能覆盖核心业务动作和必要前置步骤的命令词。"
        if round_number == 1
        else (
            "这是第二轮也是最后一轮：next_queries 最多 5 条；"
            "只列仍缺少的最重要精确查询词，避免重复已有页面。"
        )
    )
    return [
        {
            "role": "system",
            "content": (
                "你是网络设备手册的受限主动检索节点。你不能输出 CLI、命令参数、工具调用或手册外知识。"
                "你的工作是选择足以支持配置草案的手册页面，不是审阅或阻止用户配置。"
                "selected_command_ids 只能选择本轮候选中已有的 command_id；此前已选页面是累积证据，"
                "不能因发现新缺口而丢弃。对每个明确业务动作和必要前置步骤判断已覆盖、缺失或仅概念页。"
                "当已选页面足以让后续模型写出一份供用户审阅的命令草案时，就可以 verdict=sufficient；"
                "不要为了可选优化、边缘参数或完美覆盖而无休止检索。"
                "证据不足时，next_queries 应使用命令名、所在视图、参数关键词、功能别名、中英文同义词或"
                "前置条件，不能写自然语言长句，不能编造命令名。"
                + query_instruction
                + '只输出 JSON：{"action":"manual_retrieval","verdict":"sufficient|search_more|not_found",'
                '"selected_command_ids":["候选中已有 ID"],"next_queries":["检索词"],'
                '"reason_summary":"不超过300字"}。'
            ),
        },
        {
            "role": "user",
            "content": (
                f"原始用户需求：{requirement}\n完整拓扑：{json.dumps(topology_context, ensure_ascii=False)}\n"
                f"用户最终确认的配置思路：{confirmed_idea or '未提供'}\n"
                f"已知待覆盖动作/检索词：{known_actions}\n当前轮次：{round_number}\n"
                f"此前已选择的累积页面：{json.dumps(compact_selected, ensure_ascii=False)}\n"
                f"本轮候选手册页面（已按页面去重）：{json.dumps(compact_candidates, ensure_ascii=False)}\n"
                "请仅返回受限检索决策 JSON。"
            ),
        },
    ]


def _decide_with_llm(
    session: Session,
    *,
    requirement: str,
    topology_context: dict[str, Any],
    confirmed_idea: str,
    known_actions: list[str],
    round_number: int,
    previously_selected: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    on_progress: Callable[[str, str, str], None] | None = None,
    cancel_event: Event | None = None,
) -> tuple[LlmManualRetrievalDecision | None, dict[str, Any]]:
    settings = read_provider_settings(session)
    secret = get_provider_secret("llm")
    if not settings.llm_base_url or not settings.llm_model or not secret:
        return None, {"status": "disabled", "node": "retrieval_planning"}
    try:
        result = _run_async(
            request_text_result(
                base_url=settings.llm_base_url,
                api_key=secret,
                model=settings.llm_model,
                messages=_decision_prompt(
                    requirement=requirement,
                    topology_context=topology_context,
                    confirmed_idea=confirmed_idea,
                    known_actions=known_actions,
                    round_number=round_number,
                    previously_selected=previously_selected,
                    candidates=candidates,
                ),
                temperature=min(settings.llm_temperature, 0.2),
                thinking=should_enable_thinking(settings.llm_thinking_mode, "retrieval_planning"),
                stream=True,
                cancel_event=cancel_event,
                max_tokens=480,
            )
        )
        if result.cancelled:
            raise PlanningCancelled("用户已停止手册主动检索")
        try:
            decision = parse_json_response(result.content, LlmManualRetrievalDecision)
        except ValueError:
            if not result.content.strip():
                raise
            if on_progress:
                on_progress(
                    "手册检索·JSON 修复", "stage", "检索决策正式输出不是有效 JSON，正在进行一次格式修复。"
                )
            repaired = _run_async(
                request_text_result(
                    base_url=settings.llm_base_url,
                    api_key=secret,
                    model=settings.llm_model,
                    messages=json_format_repair_prompt(
                        schema_contract=(
                            '{"action":"manual_retrieval","verdict":"sufficient|search_more|not_found",'
                            '"selected_command_ids":["candidate id"],"next_queries":["query"],'
                            '"reason_summary":"string"}'
                        ),
                        answer=result.content,
                    ),
                    temperature=0.0,
                    thinking=False,
                    cancel_event=cancel_event,
                    max_tokens=1_024,
                )
            )
            decision = parse_json_response(repaired.content, LlmManualRetrievalDecision)
            result = repaired
    except PlanningCancelled:
        raise
    except Exception as exc:
        return None, {"status": "fallback", "node": "retrieval_planning", "reason": str(exc)[:240]}
    valid_ids = {str(item["command_id"]) for item in candidates if item.get("command_id")}
    invalid_ids = [item for item in decision.selected_command_ids if item not in valid_ids]
    if invalid_ids:
        return None, {
            "status": "rejected",
            "node": "retrieval_planning",
            "reason": "LLM 选择了候选集外的 command_id。",
        }
    return decision, {
        "status": "accepted",
        "node": "retrieval_planning",
        "model": settings.llm_model,
        "thinking_requested": result.thinking_requested,
        "thinking_used": result.thinking_used,
        "thinking_fallback": result.thinking_fallback,
        "thinking_fallback_reason": result.fallback_reason,
    }


def active_manual_search(
    session: Session,
    *,
    manual_id: str,
    requirement: str,
    seed_queries: list[str] | None = None,
    max_rounds: int = DEFAULT_ROUNDS,
    topology_context: dict[str, Any] | None = None,
    confirmed_idea: str = "",
    known_actions: list[str] | None = None,
    on_progress: Callable[[str, str, str], None] | None = None,
    cancel_event: Event | None = None,
) -> dict[str, Any]:
    """Run at most two explicit retrieval rounds controlled by LLM JSON.

    This is ReAct-style orchestration without native tool calling: the model
    returns a structured decision, then application code performs the actual
    handbook search and writes results back into the next prompt.
    """

    # The intent node may separate one user request into several independent
    # handbook search terms (for example, interface conversion, addressing,
    # and routing).  A high-scoring first command page must not let a small LLM
    # declare the whole request complete before those supplied leads are even
    # searched.  This is generic retrieval coverage, not a feature allow-list.
    topology_context = topology_context or {}
    known_actions = list(
        dict.fromkeys(str(item).strip() for item in known_actions or [] if str(item).strip())
    )
    raw_seed_queries = list(
        dict.fromkeys(clean for item in seed_queries or [] if (clean := _compact_query(item)))
    )
    catalog_anchors = _manual_command_anchors(
        session,
        manual_id=manual_id,
        seed_queries=raw_seed_queries,
    )
    raw_tokens = [
        token.casefold() for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]*", " ".join(raw_seed_queries))
    ]
    recurring_topics = [
        anchor for anchor in catalog_anchors if " " not in anchor and raw_tokens.count(anchor.casefold()) >= 2
    ]
    contextual_anchors = [
        f"{topic} {anchor}"
        for topic in recurring_topics
        for anchor in reversed(catalog_anchors)
        if anchor.casefold() != topic.casefold()
    ]
    mandatory_seed_queries = list(dict.fromkeys([*catalog_anchors, *contextual_anchors, *raw_seed_queries]))
    pending = [_compact_query(requirement), *mandatory_seed_queries]
    seen_queries: set[str] = set()
    all_candidates: dict[str, dict[str, Any]] = {}
    candidate_keys_by_query: dict[str, list[str]] = {}
    rounds: list[dict[str, Any]] = []
    selected_ids: list[str] = []
    followup_queries: list[str] = []
    capped_rounds = min(max(int(max_rounds), 1), MAX_ROUNDS)
    llm_status: dict[str, Any] = {"status": "not_run", "node": "retrieval_planning"}

    def collect_candidates(query: str, candidates: list[dict[str, Any]]) -> None:
        query_keys: list[str] = []
        for candidate in candidates:
            # A manual page is the LLM context unit. A command reference can
            # expose the same page through exact, FTS and embedding hits (or
            # even through multiple extracted command rows); retaining it once
            # prevents duplicate context from overwhelming a small model.
            key = f"document:{candidate['document_id']}"
            if key not in query_keys:
                query_keys.append(key)
            previous = all_candidates.get(key)
            if previous:
                previous["score"] = max(float(previous["score"]), float(candidate["score"]))
                previous["retrieval_sources"] = sorted(
                    set(previous["retrieval_sources"]) | set(candidate["retrieval_sources"])
                )
                previous_ids = list(previous.get("page_command_ids", []))
                candidate_id = str(candidate.get("command_id") or "")
                if candidate_id and candidate_id not in previous_ids:
                    previous["page_command_ids"] = [*previous_ids, candidate_id]
            else:
                all_candidates[key] = {
                    **candidate,
                    "page_command_ids": [str(candidate["command_id"])] if candidate.get("command_id") else [],
                }
        candidate_keys_by_query[query.casefold()] = query_keys

    def prioritized_candidates(preferred_queries: list[str]) -> list[dict[str, Any]]:
        """Keep exact hits from each query visible beside globally strong hits."""

        result: list[dict[str, Any]] = []
        seen_candidate_keys: set[str] = set()
        query_order = [
            *preferred_queries,
            *mandatory_seed_queries,
            *reversed(candidate_keys_by_query),
        ]
        for query in query_order:
            for candidate_key in candidate_keys_by_query.get(query.casefold(), [])[:PER_QUERY_CANDIDATES]:
                if candidate_key in seen_candidate_keys or candidate_key not in all_candidates:
                    continue
                seen_candidate_keys.add(candidate_key)
                result.append(all_candidates[candidate_key])
                if len(result) >= MAX_CANDIDATES:
                    return result
        for candidate_key, candidate in sorted(
            all_candidates.items(),
            key=lambda item: (-float(item[1]["score"]), str(item[1]["canonical_name"] or item[1]["title"])),
        ):
            if candidate_key in seen_candidate_keys:
                continue
            result.append(candidate)
            if len(result) >= MAX_CANDIDATES:
                break
        return result

    def annotated_candidates(preferred_queries: list[str]) -> list[dict[str, Any]]:
        """Expose ReAct query priority to the next, separate planning node."""

        return [
            {**candidate, "active_retrieval_priority": index}
            for index, candidate in enumerate(prioritized_candidates(preferred_queries))
        ]

    def selected_pages() -> list[dict[str, Any]]:
        selected = set(selected_ids)
        return [
            candidate
            for candidate in all_candidates.values()
            if str(candidate.get("command_id") or "") in selected
            or selected.intersection(str(item) for item in candidate.get("page_command_ids", []))
        ]

    for round_number in range(1, capped_rounds + 1):
        if cancel_event and cancel_event.is_set():
            raise PlanningCancelled("用户已停止手册主动检索")
        queries = []
        query_limit = MAX_QUERIES_PER_ROUND
        for item in pending:
            clean = _compact_query(item)
            key = clean.casefold()
            if clean and key not in seen_queries:
                seen_queries.add(key)
                queries.append(clean)
            if query_limit is not None and len(queries) >= query_limit:
                break
        if not queries:
            break
        candidates_by_query = search_manual_candidates_many(
            session,
            manual_id=manual_id,
            queries=queries,
        )
        for query in queries:
            collect_candidates(query, candidates_by_query.get(query, []))
        candidates = prioritized_candidates(queries)
        if on_progress:
            progress_text = (
                f"执行 {len(queries)} 条查询，获得 {len(candidates)} 个候选页面；"
                f"累计去重页面 {len(all_candidates)} 个。"
            )
            on_progress(
                f"手册检索·第 {round_number} 轮",
                "stage",
                _compact_query(progress_text),
            )
            on_progress(
                f"手册检索·第 {round_number} 轮",
                "output",
                json.dumps(
                    {"queries": queries, "deduplicated_page_count": len(all_candidates)}, ensure_ascii=False
                ),
            )
            on_progress(
                f"手册检索·第 {round_number} 轮",
                "stage",
                "正在让模型从候选页面中筛选可用于命令草案的内容，并判断是否需要下一轮查询。",
            )
        decision, llm_status = _decide_with_llm(
            session,
            requirement=requirement,
            topology_context=topology_context,
            confirmed_idea=confirmed_idea,
            known_actions=known_actions,
            round_number=round_number,
            previously_selected=selected_pages(),
            candidates=candidates,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )
        round_audit: dict[str, Any] = {
            "round": round_number,
            "queries": queries,
            "candidate_count": len(candidates),
            "llm": llm_status,
        }
        if decision:
            # The response Schema deliberately accepts a tolerant list so a
            # weak provider is not rejected merely for returning six items.
            # The workflow itself, however, never carries more than five into
            # the next/last hybrid-search round or its audit context.
            decision_queries = list(
                dict.fromkeys(
                    clean for item in decision.next_queries if (clean := _compact_query(item))
                )
            )[:MAX_QUERIES_PER_ROUND]
            round_audit["decision"] = {
                **decision.model_dump(mode="json"),
                "next_queries": decision_queries,
            }
            # A later ``search_more`` decision means these pages are useful but
            # incomplete, not irrelevant.  Retain them for the command-planning
            # node and prioritise them over generic neighbouring hits.
            selected_ids = list(dict.fromkeys([*selected_ids, *decision.selected_command_ids]))
            remaining_seed_queries = [
                item for item in mandatory_seed_queries if item.casefold() not in seen_queries
            ]
            if on_progress:
                on_progress(
                    f"手册检索·第 {round_number} 轮",
                    "output",
                    json.dumps(
                        {
                            "verdict": decision.verdict,
                            "selected_page_count": len(selected_pages()),
                            "next_queries": decision_queries,
                        },
                        ensure_ascii=False,
                    ),
                )
            if decision.verdict == "sufficient" and decision.selected_command_ids:
                rounds.append(round_audit)
                if on_progress:
                    on_progress(
                        f"手册检索·第 {round_number} 轮",
                        "stage",
                        "模型认为手册证据已足够，提前结束检索。",
                    )
                return {
                    "status": "found",
                    "selected_command_ids": selected_ids,
                    "catalog_anchors": catalog_anchors,
                    "candidates": annotated_candidates([*followup_queries, *mandatory_seed_queries]),
                    "rounds": rounds,
                }
            if decision.verdict == "not_found" and not remaining_seed_queries:
                rounds.append(round_audit)
                break
            if decision.verdict == "search_more" and round_number == capped_rounds:
                # This is intentionally only an audit record. The product has
                # a hard two-round retrieval budget, so no hidden third lookup
                # occurs after the final decision.
                round_audit["unresolved_queries"] = decision_queries
            else:
                followup_queries.extend(decision_queries)
            # Preserve one deterministic handbook anchor per round (which may
            # be a required CLI precondition), then let the ReAct decision
            # drive the next search.  Appending all model follow-ups after
            # every seed can exhaust the bounded loop before it ever searches
            # the precise missing action the model identified.
            # The first decision defines the final, focused lookup set.
            pending = (
                decision_queries
                if round_number == 1
                else [*remaining_seed_queries[:1], *decision_queries, *remaining_seed_queries[1:]]
            )
        else:
            rounds.append(round_audit)
            # A weak or overloaded model can fail to return its tiny retrieval
            # JSON while the handbook search itself remains fully usable.  Do
            # not throw away unsearched, intent-derived command leads in that
            # case: continue the bounded evidence-only loop without inventing
            # new queries.  This keeps the deterministic coverage guarantees
            # (for example an address precondition) independent of a model
            # formatting failure.
            remaining_seed_queries = [
                item for item in mandatory_seed_queries if item.casefold() not in seen_queries
            ]
            if not remaining_seed_queries:
                break
            pending = remaining_seed_queries
            continue
        rounds.append(round_audit)

    return {
        "status": "incomplete" if all_candidates else "not_found",
        "selected_command_ids": selected_ids,
        "catalog_anchors": catalog_anchors,
        # The model's final missing-action terms are the most valuable result
        # of a bounded ReAct loop.  Preserve their handbook hits ahead of
        # generic neighbouring pages so they still reach the command planner.
        "candidates": annotated_candidates(followup_queries),
        "rounds": rounds,
    }
