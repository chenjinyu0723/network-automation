from __future__ import annotations

import json

import app.archive_service as archive_service
import pytest
from app.archive_service import (
    export_manual,
    export_template,
    export_topology,
    import_manual,
    import_template,
    import_topology,
    persist_export,
    persist_export_to_destination,
)
from app.core.config import AppPaths
from app.models import (
    Command,
    CommandApplicability,
    CommandEmbedding,
    ConfigTask,
    ConfigurationTemplate,
    DeviceModel,
    DevicePlan,
    ImportStatus,
    KnowledgeDocument,
    Manual,
    ModelAlias,
    ModelEvidence,
    ModelLevel,
    ReviewStatus,
)
from app.planning.service import create_topology
from app.schemas import TopologyDraft


def _archive_paths(tmp_path):  # type: ignore[no-untyped-def]
    root = tmp_path / "archive-data"
    result = AppPaths(
        data_root=root,
        database=root / "network_automation.db",
        manuals_original=root / "manuals" / "original",
        manuals_extracted=root / "manuals" / "extracted",
        exports=root / "exports",
        logs=root / "logs",
    )
    result.ensure()
    return result


def _manual(session, source_path: str = "manual.html") -> Manual:  # type: ignore[no-untyped-def]
    manual = Manual(
        original_filename="huawei-vlan.html",
        stored_path=source_path,
        source_sha256="a" * 64,
        brand="Huawei",
        release="V600R025C00",
        file_format="html",
        status=ImportStatus.completed,
    )
    session.add(manual)
    session.flush()
    return manual


def _topology(session, name: str):  # type: ignore[no-untyped-def]
    return create_topology(
        session,
        TopologyDraft.model_validate(
            {
                "name": name,
                "nodes": [
                    {"id": "sw1", "kind": "switch", "name": "SW1", "x": 0, "y": 0},
                    {"id": "pc1", "kind": "pc", "name": "PC1", "x": 200, "y": 0},
                ],
                "links": [
                    {
                        "id": "link1",
                        "source": "sw1",
                        "source_port": "GE0/0/1",
                        "target": "pc1",
                        "target_port": "Ethernet0/0/1",
                    }
                ],
            }
        ),
    )


def test_manual_archive_round_trip_keeps_knowledge_and_vectors(session, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "huawei-vlan.html"
    source.write_text("manual source", encoding="utf-8")
    monkeypatch.setattr(archive_service, "paths", _archive_paths(tmp_path))
    manual = _manual(session, str(source))
    document = KnowledgeDocument(
        manual_id=manual.id,
        source_path="vlan.html",
        title="vlan batch",
        text_content="配置 VLAN。",
    )
    session.add(document)
    session.flush()
    command = Command(
        manual_id=manual.id,
        document_id=document.id,
        canonical_name="vlan batch",
        syntax_json=json.dumps(["vlan batch { vlan-id }"], ensure_ascii=False),
        views_json="[]",
        preconditions_json="[]",
        constraints_json="[]",
    )
    series = DeviceModel(
        brand="Huawei",
        canonical_name="S5700",
        level=ModelLevel.series,
        review_status=ReviewStatus.published,
        source_manual_id=manual.id,
    )
    session.add_all([command, series])
    session.flush()
    sku = DeviceModel(
        brand="Huawei",
        canonical_name="S5735",
        level=ModelLevel.sku,
        parent_id=series.id,
        review_status=ReviewStatus.published,
        source_manual_id=manual.id,
    )
    session.add(sku)
    session.flush()
    session.add_all(
        [
            ModelAlias(model_id=sku.id, alias="S5735", source="manual"),
            ModelEvidence(
                model_id=sku.id, document_id=document.id, evidence_text="S5735 属于 S5700", confidence=90
            ),
            CommandApplicability(command_id=command.id, model_id=series.id, is_supported=True),
            CommandEmbedding(
                command_id=command.id,
                manual_id=manual.id,
                model="embedding-test",
                dimensions=2,
                source_hash="b" * 64,
                vector_blob=b"\x00\x00\x80?\x00\x00\x00@",
            ),
        ]
    )
    session.commit()

    exported = export_manual(session, manual.id)
    restored = import_manual(session, exported, overwrite=True)

    assert restored.id == manual.id
    assert restored.extraction_path is None
    assert session.query(Command).filter_by(manual_id=manual.id).count() == 1
    assert session.query(CommandEmbedding).filter_by(manual_id=manual.id).one().model == "embedding-test"
    models = session.query(DeviceModel).filter_by(source_manual_id=manual.id).all()
    assert {item.canonical_name for item in models} == {"S5700", "S5735"}
    assert session.query(ModelAlias).count() == 1
    assert session.query(ModelEvidence).count() == 1


def test_topology_archive_restores_current_configuration(session) -> None:  # type: ignore[no-untyped-def]
    manual = _manual(session)
    revision = _topology(session, "园区 VLAN")
    task = ConfigTask(
        topology_revision_id=revision.id,
        manual_id=manual.id,
        requirement_text="PC1 接入 VLAN 10",
        planning_idea="创建 VLAN 并将 GE0/0/1 设置为 access。",
        intent_json=json.dumps({"vlan_ids": [10]}),
    )
    session.add(task)
    session.flush()
    session.add(
        DevicePlan(
            task_id=task.id,
            device_node_id="sw1",
            display_name="SW1",
            intent_json=json.dumps({"vlan_ids": [10]}),
            evidence_json="[]",
            commands_json=json.dumps(["vlan batch 10", "interface GE0/0/1"]),
            validation_json="{}",
            rollback_json="{}",
        )
    )
    session.commit()

    exported = export_topology(session, revision.topology_id)
    with pytest.raises(FileExistsError):
        import_topology(session, exported)
    restored_revision = import_topology(session, exported, overwrite=True)
    restored_task = session.query(ConfigTask).filter_by(topology_revision_id=restored_revision.id).one()
    restored_plan = session.query(DevicePlan).filter_by(task_id=restored_task.id).one()

    assert restored_task.requirement_text == "PC1 接入 VLAN 10"
    assert json.loads(restored_plan.commands_json) == ["vlan batch 10", "interface GE0/0/1"]


def test_template_archive_round_trip_and_overwrite(session) -> None:  # type: ignore[no-untyped-def]
    template = ConfigurationTemplate(
        title="双 VLAN 模板",
        description="园区接入参考",
        manual_name="huawei-vlan.html",
        snapshot_json=json.dumps({"planning_idea": "先建 VLAN，再配置端口"}, ensure_ascii=False),
    )
    session.add(template)
    session.commit()

    exported = export_template(session, template.id)
    payload = json.loads(exported)
    assert payload["snapshot"]["planning_idea"] == "先建 VLAN，再配置端口"
    template.description = "已修改"
    session.commit()

    with pytest.raises(FileExistsError):
        import_template(session, payload)
    restored = import_template(session, payload, overwrite=True)
    assert restored.description == "园区接入参考"
    assert json.loads(restored.snapshot_json) == {"planning_idea": "先建 VLAN，再配置端口"}


def test_export_copy_is_saved_to_the_local_exports_directory(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    configured_paths = _archive_paths(tmp_path)
    monkeypatch.setattr(archive_service, "paths", configured_paths)

    destination = persist_export(b"archive-content", "园区/拓扑?.topology.json")

    assert destination.parent == configured_paths.exports
    assert destination.read_bytes() == b"archive-content"
    assert "/" not in destination.name
    assert "?" not in destination.name


def test_export_copy_can_use_the_exact_user_selected_destination(tmp_path) -> None:  # type: ignore[no-untyped-def]
    selected = tmp_path / "用户选择的目录" / "topology.topology.json"
    selected.parent.mkdir()

    destination = persist_export_to_destination(b"chosen-location", str(selected))

    assert destination == selected
    assert selected.read_bytes() == b"chosen-location"
