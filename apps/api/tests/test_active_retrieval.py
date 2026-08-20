from __future__ import annotations

from threading import Event

import pytest
from app.planning.runtime import PlanningCancelled
from app.retrieval import active
from app.schemas import LlmManualRetrievalDecision


def _candidate(command_id: str, name: str, score: float = 0.7) -> dict[str, object]:
    return {
        "kind": "command",
        "command_id": command_id,
        "document_id": f"doc-{command_id}",
        "canonical_name": name,
        "syntax": [f"{name} enable"],
        "source_path": f"{name}.html",
        "title": name,
        "excerpt": f"{name} reference",
        "score": score,
        "retrieval_sources": ["embedding_cpu"],
    }


def _batch_search(fake_search):  # type: ignore[no-untyped-def]
    def run(session, *, manual_id: str, queries: list[str], limit: int = 16):  # type: ignore[no-untyped-def]
        return {
            query: fake_search(session, manual_id=manual_id, query=query, limit=limit)
            for query in queries
        }

    return run


def test_active_retrieval_uses_explicit_llm_queries_then_selects_existing_candidate(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []

    def fake_search(_session, *, manual_id: str, query: str, limit: int = 16):  # type: ignore[no-untyped-def]
        assert manual_id == "manual"
        calls.append(query)
        return [_candidate("rare-command", "rare-protection")] if query == "rare protection" else []

    decisions = iter(
        [
            LlmManualRetrievalDecision(
                action="manual_retrieval",
                verdict="search_more",
                next_queries=["rare protection"],
            ),
            LlmManualRetrievalDecision(
                action="manual_retrieval",
                verdict="sufficient",
                selected_command_ids=["rare-command"],
            ),
        ]
    )

    def fake_decide(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return next(decisions), {"status": "accepted", "node": "retrieval_planning"}

    monkeypatch.setattr(active, "search_manual_candidates_many", _batch_search(fake_search))
    monkeypatch.setattr(active, "_decide_with_llm", fake_decide)

    result = active.active_manual_search(
        object(),  # type: ignore[arg-type]
        manual_id="manual",
        requirement="启用罕见保护功能",
        max_rounds=3,
    )

    assert result["status"] == "found"
    assert result["selected_command_ids"] == ["rare-command"]
    assert calls == ["启用罕见保护功能", "rare protection"]
    assert len(result["rounds"]) == 2


def test_active_retrieval_candidates_are_sorted_by_score_after_multiple_rounds(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_search(_session, *, manual_id: str, query: str, limit: int = 16):  # type: ignore[no-untyped-def]
        assert manual_id == "manual"
        if query == "需求":
            return [_candidate("weak", "weak-page", 0.31)]
        if query == "strong command":
            return [_candidate("strong", "strong-page", 0.94)]
        return []

    decisions = iter(
        [
            LlmManualRetrievalDecision(
                action="manual_retrieval", verdict="search_more", next_queries=["strong command"]
            ),
            LlmManualRetrievalDecision(action="manual_retrieval", verdict="not_found"),
        ]
    )
    monkeypatch.setattr(active, "search_manual_candidates_many", _batch_search(fake_search))
    monkeypatch.setattr(
        active,
        "_decide_with_llm",
        lambda *_args, **_kwargs: (next(decisions), {"status": "accepted", "node": "retrieval_planning"}),
    )

    result = active.active_manual_search(
        object(),  # type: ignore[arg-type]
        manual_id="manual",
        requirement="需求",
        max_rounds=2,
    )

    assert [item["command_id"] for item in result["candidates"]] == ["strong", "weak"]


def test_active_retrieval_stops_after_two_rounds(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_search(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return []

    next_queries = iter(["next query 1", "next query 2", "next query 3", "next query 4"])

    def fake_decide(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return (
            LlmManualRetrievalDecision(
                action="manual_retrieval",
                verdict="search_more",
                next_queries=[next(next_queries)],
            ),
            {"status": "accepted", "node": "retrieval_planning"},
        )

    monkeypatch.setattr(active, "search_manual_candidates_many", _batch_search(fake_search))
    monkeypatch.setattr(active, "_decide_with_llm", fake_decide)

    result = active.active_manual_search(
        object(),  # type: ignore[arg-type]
        manual_id="manual",
        requirement="功能",
        max_rounds=99,
    )

    assert result["status"] == "not_found"
    assert len(result["rounds"]) == 2


def test_active_retrieval_uses_at_most_five_queries_for_each_of_two_rounds(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    batches: list[list[str]] = []

    def fake_search_many(_session, *, manual_id: str, queries: list[str], limit: int = 16):  # type: ignore[no-untyped-def]
        assert manual_id == "manual"
        batches.append(list(queries))
        return {query: [] for query in queries}

    decisions = iter(
        [
            LlmManualRetrievalDecision(
                action="manual_retrieval", verdict="search_more", next_queries=["round-two-extra"]
            ),
            LlmManualRetrievalDecision(
                action="manual_retrieval",
                verdict="search_more",
                next_queries=[f"free-query-{index}" for index in range(1, 8)],
            ),
            LlmManualRetrievalDecision(action="manual_retrieval", verdict="not_found"),
        ]
    )

    monkeypatch.setattr(active, "search_manual_candidates_many", fake_search_many)
    monkeypatch.setattr(
        active,
        "_decide_with_llm",
        lambda *_args, **_kwargs: (next(decisions), {"status": "accepted", "node": "retrieval_planning"}),
    )

    active.active_manual_search(
        object(),  # type: ignore[arg-type]
        manual_id="manual",
        requirement="配置复杂业务",
        seed_queries=[f"seed-{index}" for index in range(1, 11)],
        max_rounds=3,
    )

    assert len(batches) == 2
    assert len(batches[0]) == 5
    assert len(batches[1]) <= 5
    assert "round-two-extra" in batches[1]


def test_retrieval_decision_prompt_carries_requirement_topology_idea_and_selected_pages() -> None:
    messages = active._decision_prompt(
        requirement="为园区互联启用 OSPF",
        topology_context={"devices": [{"name": "SW1", "ip": "10.0.0.1"}], "links": []},
        confirmed_idea="SW1 与 SW2 建立 Area 0 邻接。",
        known_actions=["ospf", "area", "network"],
        round_number=2,
        previously_selected=[_candidate("interface", "interface")],
        candidates=[_candidate("ospf", "ospf")],
    )

    content = messages[1]["content"]
    assert "为园区互联启用 OSPF" in content
    assert "SW1" in content
    assert "Area 0" in content
    assert "interface" in content
    assert "ospf" in content
    assert "最多 5 条" in messages[0]["content"]


def test_active_retrieval_retains_selected_pages_when_more_search_is_needed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_search(_session, *, manual_id: str, query: str, limit: int = 16):  # type: ignore[no-untyped-def]
        assert manual_id == "manual"
        return [_candidate("first", "first-command")] if query == "功能" else []

    decisions = iter(
        [
            LlmManualRetrievalDecision(
                action="manual_retrieval",
                verdict="search_more",
                selected_command_ids=["first"],
                next_queries=["missing-command"],
            ),
            LlmManualRetrievalDecision(
                action="manual_retrieval",
                verdict="search_more",
                next_queries=["still-missing"],
            ),
        ]
    )

    def fake_decide(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return next(decisions), {"status": "accepted", "node": "retrieval_planning"}

    monkeypatch.setattr(active, "search_manual_candidates_many", _batch_search(fake_search))
    monkeypatch.setattr(active, "_decide_with_llm", fake_decide)

    result = active.active_manual_search(
        object(),  # type: ignore[arg-type]
        manual_id="manual",
        requirement="功能",
        max_rounds=2,
    )

    assert result["status"] == "incomplete"
    assert result["selected_command_ids"] == ["first"]
    assert result["rounds"][-1]["unresolved_queries"] == ["still-missing"]


def test_active_retrieval_searches_pending_intent_terms_before_accepting_sufficient(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []

    def fake_search(_session, *, manual_id: str, query: str, limit: int = 16):  # type: ignore[no-untyped-def]
        assert manual_id == "manual"
        calls.append(query)
        return [_candidate(f"command-{query}", query)]

    decisions = iter(
        [
            LlmManualRetrievalDecision(
                action="manual_retrieval",
                verdict="sufficient",
                selected_command_ids=["command-interface"],
            ),
            LlmManualRetrievalDecision(
                action="manual_retrieval",
                verdict="sufficient",
                selected_command_ids=["command-address"],
            ),
        ]
    )

    def fake_decide(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return next(decisions), {"status": "accepted", "node": "retrieval_planning"}

    monkeypatch.setattr(active, "search_manual_candidates_many", _batch_search(fake_search))
    monkeypatch.setattr(active, "_decide_with_llm", fake_decide)

    result = active.active_manual_search(
        object(),  # type: ignore[arg-type]
        manual_id="manual",
        requirement="配置站点互联",
        seed_queries=["route", "interface", "address"],
        max_rounds=3,
    )

    assert result["status"] == "found"
    assert calls == ["配置站点互联", "route", "interface", "address"]
    # The wider first packet evaluates all intent-derived seeds before the
    # model is allowed to accept the evidence as sufficient.
    assert len(result["rounds"]) == 1
    assert "coverage_continuation" not in result["rounds"][0]


def test_active_retrieval_keeps_searching_intent_terms_after_llm_format_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []

    def fake_search(_session, *, manual_id: str, query: str, limit: int = 16):  # type: ignore[no-untyped-def]
        assert manual_id == "manual"
        calls.append(query)
        return [_candidate(f"command-{query}", query)]

    def fake_decide(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None, {"status": "fallback", "node": "retrieval_planning"}

    monkeypatch.setattr(active, "search_manual_candidates_many", _batch_search(fake_search))
    monkeypatch.setattr(active, "_decide_with_llm", fake_decide)

    result = active.active_manual_search(
        object(),  # type: ignore[arg-type]
        manual_id="manual",
        requirement="配置站点互联",
        seed_queries=["interface", "ip address", "portswitch", "ospf", "area", "network"],
        max_rounds=3,
    )

    assert result["status"] == "incomplete"
    assert calls == [
        "配置站点互联",
        "interface",
        "ip address",
        "portswitch",
        "ospf",
        "area",
        "network",
    ]
    assert len(result["rounds"]) == 2


def test_active_retrieval_prioritizes_final_followup_evidence(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_search(_session, *, manual_id: str, query: str, limit: int = 16):  # type: ignore[no-untyped-def]
        assert manual_id == "manual"
        return [_candidate(f"command-{query}", query)]

    def fake_decide(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return (
            LlmManualRetrievalDecision(
                action="manual_retrieval",
                verdict="search_more",
                next_queries=["network"],
            ),
            {"status": "accepted", "node": "retrieval_planning"},
        )

    monkeypatch.setattr(active, "search_manual_candidates_many", _batch_search(fake_search))
    monkeypatch.setattr(active, "_decide_with_llm", fake_decide)

    result = active.active_manual_search(
        object(),  # type: ignore[arg-type]
        manual_id="manual",
        requirement="发布路由",
        seed_queries=["interface", "ip address", "portswitch", "ospf", "area", "router-id"],
        max_rounds=1,
    )

    assert result["rounds"][0]["unresolved_queries"] == ["network"]
    assert len(result["rounds"]) == 1


def test_active_retrieval_honours_cancellation_before_an_llm_request() -> None:
    cancelled = Event()
    cancelled.set()

    with pytest.raises(PlanningCancelled, match="停止手册主动检索"):
        active.active_manual_search(
            object(),  # type: ignore[arg-type]
            manual_id="manual",
            requirement="OSPF",
            cancel_event=cancelled,
        )
