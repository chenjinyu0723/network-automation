from __future__ import annotations

from pathlib import Path

from app.execution.readonly import _validate_read_only
from app.execution.service import _check_undo_commands, _check_write_commands
from app.ingestion.chm import parse_html_page, parse_toc
from app.model_resolution import resolve_series_for_model
from app.models import (
    CompatibilityStatus,
    DeviceModel,
    Manual,
    ModelAlias,
    ModelLevel,
    ReviewStatus,
)
from app.planning.service import _manual_selection_context
from app.ports import port_appears_in_output, port_identity


def test_read_only_probe_rejects_configuration_mode() -> None:
    try:
        _validate_read_only("system-view")
    except ValueError as exc:
        assert "白名单" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("configuration mode must be blocked")


def test_selected_completed_manual_authorizes_planning_without_model() -> None:
    manual = Manual(
        original_filename="huawei-reference.chm",
        brand="Huawei",
        release="V200R001C00",
        file_format="chm",
    )
    status, reason, series = _manual_selection_context(
        manual,
        detected_model="S5700-28C-HI",
        detected_release="V200R001C00",
    )
    assert status == CompatibilityStatus.manual_selected
    assert series is None
    assert "用户已选择已完成抽取的手册" in reason
    assert "S5700-28C-HI" in reason


def test_s5735_and_s5755_resolve_through_catalog_parent_tree(session) -> None:  # type: ignore[no-untyped-def]
    manual = Manual(brand="Huawei", release="V600R025C00", file_format="chm")
    series = DeviceModel(
        brand="Huawei",
        canonical_name="S5700",
        level=ModelLevel.series,
        review_status=ReviewStatus.published,
        source_manual_id=manual.id,
        confidence=100,
    )
    s5735 = DeviceModel(
        brand="Huawei",
        canonical_name="S5735",
        level=ModelLevel.family,
        parent=series,
        review_status=ReviewStatus.candidate,
    )
    s5755 = DeviceModel(
        brand="Huawei",
        canonical_name="S5755",
        level=ModelLevel.family,
        parent=series,
        review_status=ReviewStatus.candidate,
    )
    session.add_all([series, s5735, s5755])
    session.flush()
    session.add(ModelAlias(model_id=s5755.id, alias="S5755-S48T4X-A"))
    session.commit()

    for model_name in ("S5735", "s5755", "S5755-S48T4X-A"):
        resolution = resolve_series_for_model(
            session,
            model_name=model_name,
            brand="Huawei",
            covered_series={"S5700"},
        )
        assert resolution is not None
        assert resolution.series == "S5700"
        assert resolution.source == "model_catalog_tree"

def test_rejected_catalog_mapping_does_not_authorize_series(session) -> None:  # type: ignore[no-untyped-def]
    series = DeviceModel(
        brand="Huawei",
        canonical_name="S5700",
        level=ModelLevel.series,
        review_status=ReviewStatus.published,
    )
    rejected = DeviceModel(
        brand="Huawei",
        canonical_name="S5735",
        level=ModelLevel.family,
        parent=series,
        review_status=ReviewStatus.rejected,
    )
    session.add_all([series, rejected])
    session.commit()

    resolution = resolve_series_for_model(
        session,
        model_name="S5735",
        brand="Huawei",
        covered_series={"S5700"},
    )
    assert resolution is None


def test_rejected_exact_catalog_match_cannot_fall_back_to_huawei_prefix(session) -> None:  # type: ignore[no-untyped-def]
    series = DeviceModel(
        brand="Huawei",
        canonical_name="S5700",
        level=ModelLevel.series,
        review_status=ReviewStatus.published,
    )
    rejected_sku = DeviceModel(
        brand="Huawei",
        canonical_name="S5700-28C-HI",
        level=ModelLevel.sku,
        parent=series,
        review_status=ReviewStatus.rejected,
    )
    session.add_all([series, rejected_sku])
    session.commit()

    resolution = resolve_series_for_model(
        session,
        model_name="S5700-28C-HI",
        brand="Huawei",
        covered_series={"S5700"},
    )
    assert resolution is None


def test_huawei_s_series_subseries_fallback_requires_manual_coverage(session) -> None:  # type: ignore[no-untyped-def]
    assert (
        resolve_series_for_model(
            session,
            model_name="S6730-H48X6C",
            brand="Huawei",
            covered_series={"S6700"},
        ).series
        == "S6700"
    )
    assert (
        resolve_series_for_model(
            session,
            model_name="S7706",
            brand="Huawei",
            covered_series={"S5700"},
        )
        is None
    )


def test_port_aliases_compare_equally_without_rewriting_original_text() -> None:
    assert port_identity("GE0/0/0") == port_identity("GigabitEthernet0/0/0") == "GE0/0/0"
    assert port_appears_in_output("Interface GigabitEthernet0/0/0 current state : UP", "GE0/0/0")
    assert port_appears_in_output("Interface GE0/0/0 current state : UP", "GigabitEthernet0/0/0")
    assert port_appears_in_output("Interface TenGigabitEthernet0/0/1 current state : UP", "XGE0/0/1")
    assert not port_appears_in_output("Interface GE0/0/10 current state : UP", "GE0/0/1")


def test_execution_scope_rejects_non_pc_uplink_even_without_explicit_protection() -> None:
    errors = _check_write_commands(
        ["system-view", "interface GE0/0/2", "port link-type access"],
        protected_ports=set(),
        allowed_ports={"GE0/0/1"},
    )
    assert errors == ["命令尝试进入当前功能范围外端口 GE0/0/2"]


def test_undo_scope_rejects_protected_uplink_and_allows_only_pc_port() -> None:
    allowed = _check_undo_commands(
        [
            "system-view",
            "interface GE0/0/1",
            "undo port default vlan 10",
            "quit",
            "undo vlan batch 10",
            "return",
        ],
        protected_ports={"GE0/0/2"},
        allowed_ports={"GE0/0/1"},
    )
    protected = _check_undo_commands(
        [
            "system-view",
            "interface GigabitEthernet0/0/2",
            "undo port default vlan 10",
            "quit",
            "undo vlan batch 10",
            "return",
        ],
        protected_ports={"GE0/0/2"},
        allowed_ports={"GE0/0/1"},
    )
    assert allowed == []
    assert protected == ["Undo 尝试进入受保护端口 GE0/0/2"]


def test_chm_syntax_keeps_one_grammar_per_line() -> None:
    root = Path("data/manuals/extracted/b0c0b2545d224dabb154bfeaf5c5a342")
    if not root.exists():
        return
    toc, _ = parse_toc(root)
    page = parse_html_page(root / "v6r25c00/CLI/BATCHCREATEVLAN(VLANOM).html", root, toc)
    assert page.command
    assert page.command["syntax"] == [
        "vlan batch { vlan-id1 [ to vlan-id2 ] } &<1-10>",
        "undo vlan batch { vlan-id1 [ to vlan-id2 ] } &<1-10>",
    ]
