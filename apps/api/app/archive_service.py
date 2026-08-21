from __future__ import annotations

import base64
import io
import json
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, inspect, select
from sqlalchemy.orm import Session

from app.core.config import paths
from app.ingestion.pipeline import _refresh_fts
from app.models import (
    Command,
    CommandApplicability,
    CommandEmbedding,
    CompatibilityStatus,
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
    TaskStatus,
    Topology,
)
from app.planning.service import create_topology, get_topology_revision, update_topology
from app.schemas import TopologyDraft
from app.template_service import sanitize_template_snapshot, validate_template_snapshot


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(value: str | None) -> Any:
    return json.loads(value) if value else {}


def persist_export(content: bytes, filename: str) -> Path:
    """Persist an export locally so the UI can report an exact, durable path."""

    paths.exports.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r'[<>:"/\\|?*]', "_", Path(filename).name).strip(". ") or "export.bin"
    suffix = "".join(Path(safe_name).suffixes)
    stem = safe_name[: -len(suffix)] if suffix else safe_name
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = paths.exports / f"{stem}-{timestamp}{suffix}"
    sequence = 2
    while destination.exists():
        destination = paths.exports / f"{stem}-{timestamp}-{sequence}{suffix}"
        sequence += 1
    destination.write_bytes(content)
    return destination


def persist_export_to_destination(content: bytes, destination_path: str) -> Path:
    """Write an archive to the exact location selected in the desktop save dialog."""

    destination = Path(destination_path).expanduser()
    if not destination.is_absolute() or not destination.name:
        raise ValueError("导出位置必须是有效的本地文件路径")
    if not destination.parent.is_dir():
        raise ValueError("所选导出目录不存在")
    if destination.exists() and destination.is_dir():
        raise ValueError("所选导出位置是目录，不是文件")
    destination.write_bytes(content)
    return destination


def _manual_payload(session: Session, manual: Manual) -> dict[str, Any]:
    documents = session.scalars(
        select(KnowledgeDocument).where(KnowledgeDocument.manual_id == manual.id)
    ).all()
    commands = session.scalars(select(Command).where(Command.manual_id == manual.id)).all()
    document_ids = {item.id for item in documents}
    command_ids = {item.id for item in commands}
    models = session.scalars(select(DeviceModel).where(DeviceModel.source_manual_id == manual.id)).all()
    model_ids = {item.id for item in models}
    aliases = (
        session.scalars(select(ModelAlias).where(ModelAlias.model_id.in_(model_ids))).all()
        if model_ids
        else []
    )
    evidence = (
        session.scalars(select(ModelEvidence).where(ModelEvidence.model_id.in_(model_ids))).all()
        if model_ids
        else []
    )
    applicability = (
        session.scalars(
            select(CommandApplicability).where(CommandApplicability.command_id.in_(command_ids))
        ).all()
        if command_ids
        else []
    )
    embeddings = (
        session.scalars(select(CommandEmbedding).where(CommandEmbedding.command_id.in_(command_ids))).all()
        if command_ids
        else []
    )
    return {
        "manifest": {"kind": "network-automation-manual", "version": 1},
        "manual": {
            "original_filename": manual.original_filename,
            "source_sha256": manual.source_sha256,
            "file_format": manual.file_format,
            "brand": manual.brand,
            "release": manual.release,
            "cli_profile": manual.cli_profile,
            "status": manual.status.value,
            "extraction_path": manual.extraction_path,
            "page_count": manual.page_count,
            "command_count": manual.command_count,
            "model_count": manual.model_count,
            "issue_count": manual.issue_count,
        },
        "documents": [
            {
                "id": item.id,
                "source_path": item.source_path,
                "title": item.title,
                "toc_path_json": item.toc_path_json,
                "page_type": item.page_type,
                "encoding": item.encoding,
                "text_content": item.text_content,
                "metadata_json": item.metadata_json,
            }
            for item in documents
            if item.id in document_ids
        ],
        "commands": [
            {
                "id": item.id,
                "document_id": item.document_id,
                "canonical_name": item.canonical_name,
                "feature": item.feature,
                "syntax_json": item.syntax_json,
                "views_json": item.views_json,
                "parameters_json": item.parameters_json,
                "preconditions_json": item.preconditions_json,
                "constraints_json": item.constraints_json,
                "examples_json": item.examples_json,
                "evidence_json": item.evidence_json,
                "applicability_mode": item.applicability_mode,
                "extraction_confidence": item.extraction_confidence,
            }
            for item in commands
        ],
        "models": [
            {
                "id": item.id,
                "canonical_name": item.canonical_name,
                "level": item.level.value,
                "parent_id": item.parent_id,
                "review_status": item.review_status.value,
                "confidence": item.confidence,
            }
            for item in models
        ],
        "aliases": [
            {"model_id": item.model_id, "alias": item.alias, "source": item.source} for item in aliases
        ],
        "model_evidence": [
            {
                "model_id": item.model_id,
                "document_id": item.document_id,
                "evidence_text": item.evidence_text,
                "confidence": item.confidence,
                "source_kind": item.source_kind,
            }
            for item in evidence
        ],
        "applicability": [
            {
                "command_id": item.command_id,
                "model_id": item.model_id,
                "is_supported": item.is_supported,
                "evidence_text": item.evidence_text,
                "confidence": item.confidence,
            }
            for item in applicability
        ],
        "embeddings": [
            {
                "command_id": item.command_id,
                "model": item.model,
                "dimensions": item.dimensions,
                "source_hash": item.source_hash,
                "vector_b64": base64.b64encode(item.vector_blob).decode("ascii"),
            }
            for item in embeddings
        ],
    }


def export_manual(session: Session, manual_id: str) -> bytes:
    manual = session.get(Manual, manual_id)
    if not manual:
        raise ValueError("手册不存在")
    payload = _manual_payload(session, manual)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", _dump(payload))
        source = Path(manual.stored_path)
        if source.exists() and source.is_file():
            bundle.writestr(f"source/{manual.original_filename}", source.read_bytes())
    return archive.getvalue()


def _clear_manual_contents(session: Session, manual: Manual) -> None:
    command_ids = list(session.scalars(select(Command.id).where(Command.manual_id == manual.id)).all())
    document_ids = list(
        session.scalars(select(KnowledgeDocument.id).where(KnowledgeDocument.manual_id == manual.id)).all()
    )
    model_ids = list(
        session.scalars(select(DeviceModel.id).where(DeviceModel.source_manual_id == manual.id)).all()
    )
    if command_ids:
        session.execute(delete(CommandEmbedding).where(CommandEmbedding.command_id.in_(command_ids)))
        session.execute(delete(CommandApplicability).where(CommandApplicability.command_id.in_(command_ids)))
        session.execute(delete(Command).where(Command.id.in_(command_ids)))
    if model_ids:
        # Break the self-referential model tree before deleting its rows.
        session.query(DeviceModel).filter(DeviceModel.parent_id.in_(model_ids)).update(
            {DeviceModel.parent_id: None}, synchronize_session=False
        )
        session.execute(delete(ModelAlias).where(ModelAlias.model_id.in_(model_ids)))
        session.execute(delete(ModelEvidence).where(ModelEvidence.model_id.in_(model_ids)))
        session.execute(delete(CommandApplicability).where(CommandApplicability.model_id.in_(model_ids)))
        session.execute(delete(DeviceModel).where(DeviceModel.id.in_(model_ids)))
    if document_ids:
        session.execute(delete(KnowledgeDocument).where(KnowledgeDocument.id.in_(document_ids)))
    session.flush()


def delete_manual_with_contents(session: Session, manual: Manual) -> None:
    """Delete one manual and only artifacts that reside in the app data directory."""

    artifact_paths = [manual.stored_path, manual.extraction_path]
    _clear_manual_contents(session, manual)
    session.delete(manual)
    session.commit()
    data_root = paths.data_root.resolve()
    for raw_path in artifact_paths:
        if not raw_path:
            continue
        path = Path(raw_path).resolve()
        try:
            path.relative_to(data_root)
        except ValueError:
            continue
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()


def import_manual(session: Session, content: bytes, *, overwrite: bool = False) -> Manual:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as bundle:
            payload = json.loads(bundle.read("manifest.json").decode("utf-8"))
            source_names = [name for name in bundle.namelist() if name.startswith("source/")]
            source_content = bundle.read(source_names[0]) if source_names else None
    except (KeyError, ValueError, zipfile.BadZipFile) as exc:
        raise ValueError("手册归档文件无效或缺少 manifest.json") from exc
    if payload.get("manifest", {}).get("kind") != "network-automation-manual":
        raise ValueError("不是 Network Automation 手册归档")
    info = payload.get("manual") or {}
    filename = str(info.get("original_filename") or "imported-manual")
    existing = session.scalar(select(Manual).where(Manual.original_filename == filename))
    by_hash = (
        session.scalar(select(Manual).where(Manual.source_sha256 == info.get("source_sha256")))
        if info.get("source_sha256")
        else None
    )
    existing = existing or by_hash
    if existing and not overwrite:
        raise FileExistsError(f"同名手册已存在：{existing.original_filename}（{existing.id}）")
    if existing:
        manual = existing
        _clear_manual_contents(session, manual)
    else:
        manual = Manual(
            original_filename=filename,
            stored_path="",
            source_sha256=str(info.get("source_sha256") or ""),
            file_format=str(info.get("file_format") or "archive"),
            status=ImportStatus.completed,
        )
        session.add(manual)
        session.flush()
    source_path = paths.manuals_original / f"{manual.id}_{Path(filename).name}"
    if source_content is not None:
        source_path.write_bytes(source_content)
    manual.stored_path = str(source_path)
    manual.original_filename = filename
    manual.source_sha256 = str(info.get("source_sha256") or manual.source_sha256)
    manual.file_format = str(info.get("file_format") or manual.file_format)
    manual.brand = info.get("brand")
    manual.release = info.get("release")
    manual.cli_profile = str(info.get("cli_profile") or "auto")
    manual.status = ImportStatus.completed
    manual.extraction_path = None
    manual.page_count = int(info.get("page_count") or len(payload.get("documents") or []))
    manual.command_count = int(info.get("command_count") or len(payload.get("commands") or []))
    manual.model_count = int(info.get("model_count") or len(payload.get("models") or []))
    manual.issue_count = int(info.get("issue_count") or 0)
    document_map: dict[str, str] = {}
    for item in payload.get("documents") or []:
        document = KnowledgeDocument(
            manual_id=manual.id,
            source_path=str(item.get("source_path") or ""),
            title=item.get("title"),
            toc_path_json=str(item.get("toc_path_json") or "[]"),
            page_type=str(item.get("page_type") or "topic"),
            encoding=item.get("encoding"),
            text_content=str(item.get("text_content") or ""),
            metadata_json=str(item.get("metadata_json") or "{}"),
        )
        session.add(document)
        session.flush()
        document_map[str(item.get("id"))] = document.id
    command_map: dict[str, str] = {}
    for item in payload.get("commands") or []:
        document_id = document_map.get(str(item.get("document_id")))
        if not document_id:
            continue
        command = Command(
            manual_id=manual.id,
            document_id=document_id,
            canonical_name=str(item.get("canonical_name") or ""),
            feature=item.get("feature"),
            syntax_json=str(item.get("syntax_json") or "[]"),
            views_json=str(item.get("views_json") or "[]"),
            parameters_json=str(item.get("parameters_json") or "[]"),
            preconditions_json=str(item.get("preconditions_json") or "[]"),
            constraints_json=str(item.get("constraints_json") or "[]"),
            examples_json=str(item.get("examples_json") or "[]"),
            evidence_json=str(item.get("evidence_json") or "{}"),
            applicability_mode=str(item.get("applicability_mode") or "inherit_with_exceptions"),
            extraction_confidence=int(item.get("extraction_confidence") or 70),
        )
        session.add(command)
        session.flush()
        command_map[str(item.get("id"))] = command.id
    model_map: dict[str, str] = {}
    for item in payload.get("models") or []:
        model = DeviceModel(
            brand=str(info.get("brand") or "Unknown"),
            canonical_name=str(item.get("canonical_name") or ""),
            level=ModelLevel(str(item.get("level") or ModelLevel.series.value)),
            review_status=ReviewStatus(str(item.get("review_status") or ReviewStatus.candidate.value)),
            confidence=int(item.get("confidence") or 50),
            source_manual_id=manual.id,
        )
        session.add(model)
        session.flush()
        model_map[str(item.get("id"))] = model.id
    for item in payload.get("models") or []:
        model_id = model_map.get(str(item.get("id")))
        parent_id = model_map.get(str(item.get("parent_id"))) if item.get("parent_id") else None
        if model_id and parent_id:
            session.get(DeviceModel, model_id).parent_id = parent_id
    for item in payload.get("aliases") or []:
        if model_map.get(str(item.get("model_id"))):
            session.add(
                ModelAlias(
                    model_id=model_map[str(item["model_id"])],
                    alias=str(item.get("alias") or ""),
                    source=str(item.get("source") or "archive"),
                )
            )
    for item in payload.get("model_evidence") or []:
        if model_map.get(str(item.get("model_id"))):
            session.add(
                ModelEvidence(
                    model_id=model_map[str(item["model_id"])],
                    document_id=document_map.get(str(item.get("document_id"))),
                    evidence_text=str(item.get("evidence_text") or ""),
                    confidence=int(item.get("confidence") or 50),
                    source_kind=str(item.get("source_kind") or "archive"),
                )
            )
    for item in payload.get("applicability") or []:
        if command_map.get(str(item.get("command_id"))) and model_map.get(str(item.get("model_id"))):
            session.add(
                CommandApplicability(
                    command_id=command_map[str(item["command_id"])],
                    model_id=model_map[str(item["model_id"])],
                    is_supported=bool(item.get("is_supported", True)),
                    evidence_text=str(item.get("evidence_text") or ""),
                    confidence=int(item.get("confidence") or 60),
                )
            )
    for item in payload.get("embeddings") or []:
        if command_map.get(str(item.get("command_id"))):
            session.add(
                CommandEmbedding(
                    command_id=command_map[str(item["command_id"])],
                    manual_id=manual.id,
                    model=str(item.get("model") or ""),
                    dimensions=int(item.get("dimensions") or 0),
                    source_hash=str(item.get("source_hash") or ""),
                    vector_blob=base64.b64decode(str(item.get("vector_b64") or "")),
                )
            )
    session.commit()
    _rebuild_manual_search(session, manual)
    session.refresh(manual)
    return manual


def _rebuild_manual_search(session: Session, manual: Manual) -> None:
    """Refresh FTS rows when the destination database exposes the local indexes."""

    bind = session.get_bind()
    if not inspect(bind).has_table("command_search") or not inspect(bind).has_table("document_search"):
        return
    with bind.begin() as connection:
        connection.exec_driver_sql("DELETE FROM command_search WHERE manual_id = ?", (manual.id,))
        commands = session.scalars(select(Command).where(Command.manual_id == manual.id)).all()
        for command in commands:
            _refresh_fts(connection, command, command.document)
        connection.exec_driver_sql("DELETE FROM document_search WHERE manual_id = ?", (manual.id,))
        connection.exec_driver_sql(
            """
            INSERT INTO document_search(document_id, manual_id, content)
            SELECT id, manual_id,
                   trim(
                       coalesce(title, '') || char(10) || coalesce(toc_path_json, '') ||
                       char(10) || coalesce(text_content, '')
                   )
            FROM knowledge_documents
            WHERE manual_id = ?
            """,
            (manual.id,),
        )


def export_topology(session: Session, topology_id: str) -> dict[str, Any]:
    revision = get_topology_revision(session, topology_id)
    if not revision:
        raise ValueError("拓扑不存在")
    task = session.scalar(
        select(ConfigTask)
        .where(ConfigTask.topology_revision_id == revision.id)
        .order_by(ConfigTask.updated_at.desc())
    )
    config = None
    if task:
        manual = session.get(Manual, task.manual_id)
        config = {
            "manual_id": task.manual_id,
            "manual": {
                "original_filename": manual.original_filename if manual else None,
                "source_sha256": manual.source_sha256 if manual else None,
            },
            "requirement_text": task.requirement_text,
            "status": task.status.value,
            "intent": _load(task.intent_json),
            "planning_idea": task.planning_idea,
            "device_plans": [
                {
                    "display_name": plan.display_name,
                    "device_node_id": plan.device_node_id,
                    "detected_model": plan.detected_model,
                    "detected_release": plan.detected_release,
                    "mapped_series": plan.mapped_series,
                    "compatibility_status": plan.compatibility_status.value,
                    "compatibility_reason": plan.compatibility_reason,
                    "intent": _load(plan.intent_json),
                    "evidence": _load(plan.evidence_json),
                    "commands": _load(plan.commands_json),
                    "validation": _load(plan.validation_json),
                    "rollback": _load(plan.rollback_json),
                    "approval_revision": plan.approval_revision,
                }
                for plan in task.device_plans
            ],
        }
    return {
        "manifest": {"kind": "network-automation-topology", "version": 1},
        "topology": json.loads(revision.graph_json),
        "config": config,
    }


def import_topology(session: Session, payload: dict[str, Any], *, overwrite: bool = False):  # type: ignore[no-untyped-def]
    if payload.get("manifest", {}).get("kind") != "network-automation-topology":
        raise ValueError("不是 Network Automation 拓扑归档")
    draft = TopologyDraft.model_validate(payload.get("topology") or {})
    existing = session.scalar(select(Topology).where(Topology.name == draft.name))
    if existing and not overwrite:
        raise FileExistsError(f"同名拓扑已存在：{draft.name}（{existing.id}）")
    revision = update_topology(session, existing.id, draft) if existing else create_topology(session, draft)
    _restore_topology_configuration(session, revision.id, payload.get("config"))
    return revision


def _restore_topology_configuration(session: Session, topology_revision_id: str, config: object) -> None:
    if not isinstance(config, dict):
        return
    manual = session.get(Manual, str(config.get("manual_id") or ""))
    manual_reference = config.get("manual")
    if not manual and isinstance(manual_reference, dict):
        source_sha256 = manual_reference.get("source_sha256")
        filename = manual_reference.get("original_filename")
        if source_sha256:
            manual = session.scalar(select(Manual).where(Manual.source_sha256 == str(source_sha256)))
        if not manual and filename:
            manual = session.scalar(select(Manual).where(Manual.original_filename == str(filename)))
    if not manual:
        return
    task = ConfigTask(
        topology_revision_id=topology_revision_id,
        manual_id=manual.id,
        requirement_text=str(config.get("requirement_text") or "从拓扑归档导入的配置要求"),
        status=TaskStatus.needs_review if config.get("device_plans") else TaskStatus.idea_ready,
        intent_json=_dump(config.get("intent") or {}),
        planning_idea=str(config.get("planning_idea") or ""),
    )
    session.add(task)
    session.flush()
    for item in config.get("device_plans") or []:
        if not isinstance(item, dict):
            continue
        try:
            compatibility = CompatibilityStatus(
                str(item.get("compatibility_status") or CompatibilityStatus.manual_selected.value)
            )
        except ValueError:
            compatibility = CompatibilityStatus.manual_selected
        plan = DevicePlan(
            task_id=task.id,
            device_node_id=str(item.get("device_node_id") or "unknown-device"),
            display_name=str(item.get("display_name") or "未命名设备"),
            detected_model=item.get("detected_model"),
            detected_release=item.get("detected_release"),
            mapped_series=item.get("mapped_series"),
            compatibility_status=compatibility,
            compatibility_reason=item.get("compatibility_reason"),
            intent_json=_dump(item.get("intent") or {}),
            evidence_json=_dump(item.get("evidence") or []),
            commands_json=_dump(item.get("commands") or []),
            validation_json=_dump(item.get("validation") or {}),
            rollback_json=_dump(item.get("rollback") or {}),
            approval_revision=int(item.get("approval_revision") or 0),
        )
        session.add(plan)
    session.commit()


def export_template(session: Session, template_id: str) -> bytes:
    template = session.get(ConfigurationTemplate, template_id)
    if not template:
        raise ValueError("配置模板不存在")
    payload = {
        "manifest": {"kind": "network-automation-template", "version": 1},
        "title": template.title,
        "description": template.description,
        "source_task_id": template.source_task_id,
        "manual_name": template.manual_name,
        "snapshot": sanitize_template_snapshot(_load(template.snapshot_json)),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def import_template(
    session: Session, payload: dict[str, Any], *, overwrite: bool = False
) -> ConfigurationTemplate:
    if payload.get("manifest", {}).get("kind") != "network-automation-template":
        raise ValueError("不是 Network Automation 模板归档")
    title = str(payload.get("title") or "").strip()
    if not title:
        raise ValueError("模板标题不能为空")
    template = session.scalar(select(ConfigurationTemplate).where(ConfigurationTemplate.title == title))
    if template and not overwrite:
        raise FileExistsError(f"同名模板已存在：{title}（{template.id}）")
    if not template:
        template = ConfigurationTemplate(
            title=title,
            description=str(payload.get("description") or ""),
            manual_name=payload.get("manual_name"),
        )
        session.add(template)
    template.description = str(payload.get("description") or "")
    template.manual_name = payload.get("manual_name")
    template.source_task_id = payload.get("source_task_id")
    imported_snapshot = payload.get("snapshot") or {}
    if not isinstance(imported_snapshot, dict):
        raise ValueError("模板快照格式无效")
    # Legacy exports created before editable templates may only contain a
    # planning note. Preserve them for viewing; newly created complete
    # snapshots are validated strictly before their command blocks are stored.
    normalized_snapshot = (
        validate_template_snapshot(imported_snapshot)
        if isinstance(imported_snapshot.get("topology"), dict)
        else sanitize_template_snapshot(imported_snapshot)
    )
    template.snapshot_json = _dump(normalized_snapshot)
    session.commit()
    session.refresh(template)
    return template
