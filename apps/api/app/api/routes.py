from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.execution.pc_ping import run_pc_ping
from app.execution.readonly import run_huawei_read_only_probe
from app.execution.service import execute_huawei_device_plan
from app.ingestion.pipeline import create_manual_from_upload, recover_interrupted_imports, start_import_worker
from app.models import (
    DeviceModel,
    EmbeddingJob,
    ExecutionRun,
    ImportJob,
    Manual,
    ModelAlias,
    PcPingRun,
    ReviewStatus,
)
from app.planning.service import approve_device_plan, create_config_task, create_topology
from app.retrieval.embeddings import create_embedding_job, start_embedding_worker
from app.retrieval.hybrid import hybrid_command_search
from app.schemas import (
    CommandSearchHit,
    CommandSearchResponse,
    ConfigTaskCreate,
    ConfigTaskResponse,
    DeviceApprovalRequest,
    DeviceExecutionRequest,
    DevicePlanResponse,
    EmbeddingJobResponse,
    ExecutionRunResponse,
    ImportJobResponse,
    ManualDetail,
    ManualSummary,
    ModelCorrectionRequest,
    ModelResponse,
    PcPingRequest,
    PcPingResponse,
    ProviderSettingsInput,
    ProviderSettingsResponse,
    ReadOnlyProbeRequest,
    ReadOnlyProbeResponse,
    TopologyDraft,
    TopologyResponse,
)
from app.services.settings import read_provider_settings, save_provider_settings

router = APIRouter(prefix="/api")


def manual_summary(manual: Manual) -> ManualSummary:
    return ManualSummary(
        id=manual.id,
        original_filename=manual.original_filename,
        file_format=manual.file_format,
        brand=manual.brand,
        release=manual.release,
        status=manual.status.value,
        page_count=manual.page_count,
        command_count=manual.command_count,
        model_count=manual.model_count,
        issue_count=manual.issue_count,
        created_at=manual.created_at,
        updated_at=manual.updated_at,
    )


def job_response(job: ImportJob) -> ImportJobResponse:
    return ImportJobResponse(
        id=job.id,
        manual_id=job.manual_id,
        status=job.status.value,
        stage=job.stage,
        progress_current=job.progress_current,
        progress_total=job.progress_total,
        detail=job.detail,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def embedding_job_response(job: EmbeddingJob) -> EmbeddingJobResponse:
    return EmbeddingJobResponse(
        id=job.id,
        manual_id=job.manual_id,
        model=job.model,
        status=job.status.value,
        progress_current=job.progress_current,
        progress_total=job.progress_total,
        detail=job.detail,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def model_response(model: DeviceModel) -> ModelResponse:
    return ModelResponse(
        id=model.id,
        brand=model.brand,
        canonical_name=model.canonical_name,
        level=model.level.value,
        parent_id=model.parent_id,
        review_status=model.review_status.value,
        confidence=model.confidence,
        source_manual_id=model.source_manual_id,
        aliases=sorted({item.alias for item in model.aliases}),
        evidence_count=len(model.evidence),
    )


def device_plan_response(plan) -> DevicePlanResponse:  # type: ignore[no-untyped-def]
    validation = json.loads(plan.validation_json)
    return DevicePlanResponse(
        id=plan.id,
        device_node_id=plan.device_node_id,
        display_name=plan.display_name,
        detected_model=plan.detected_model,
        detected_release=plan.detected_release,
        mapped_series=plan.mapped_series,
        compatibility_status=plan.compatibility_status.value,
        compatibility_reason=plan.compatibility_reason,
        intent=json.loads(plan.intent_json),
        command_plan=validation.get("command_plan", {}),
        evidence=json.loads(plan.evidence_json),
        commands=json.loads(plan.commands_json),
        validation=json.loads(plan.validation_json),
        rollback=json.loads(plan.rollback_json),
        approval_revision=plan.approval_revision,
        approved_at=plan.approved_at,
    )


def execution_response(execution) -> ExecutionRunResponse:  # type: ignore[no-untyped-def]
    return ExecutionRunResponse(
        id=execution.id,
        task_id=execution.task_id,
        device_plan_id=execution.device_plan_id,
        status=execution.status.value,
        target_host=execution.target_host,
        target_port=execution.target_port,
        execution_revision=execution.execution_revision,
        preflight=json.loads(execution.preflight_json),
        validation=json.loads(execution.validation_json),
        save=json.loads(execution.save_json),
        error_message=execution.error_message,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
        commands=[
            {
                "sequence": entry.sequence,
                "phase": entry.phase,
                "command": entry.command,
                "output": entry.output,
                "success": entry.success,
            }
            for entry in sorted(execution.commands, key=lambda item: item.sequence)
        ],
    )


def config_task_response(task) -> ConfigTaskResponse:  # type: ignore[no-untyped-def]
    return ConfigTaskResponse(
        id=task.id,
        topology_revision_id=task.topology_revision_id,
        manual_id=task.manual_id,
        requirement_text=task.requirement_text,
        status=task.status.value,
        intent=json.loads(task.intent_json),
        blocking_reason=task.blocking_reason,
        device_plans=[device_plan_response(item) for item in task.device_plans],
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/settings/providers", response_model=ProviderSettingsResponse)
def get_provider_settings(session: Session = Depends(get_session)) -> ProviderSettingsResponse:
    return read_provider_settings(session)


@router.put("/settings/providers", response_model=ProviderSettingsResponse)
def put_provider_settings(
    payload: ProviderSettingsInput,
    session: Session = Depends(get_session),
) -> ProviderSettingsResponse:
    return save_provider_settings(session, payload)


@router.get("/manuals", response_model=list[ManualSummary])
def list_manuals(session: Session = Depends(get_session)) -> list[ManualSummary]:
    manuals = session.scalars(select(Manual).order_by(Manual.created_at.desc())).all()
    return [manual_summary(item) for item in manuals]


@router.get("/manuals/{manual_id}", response_model=ManualDetail)
def get_manual(manual_id: str, session: Session = Depends(get_session)) -> ManualDetail:
    manual = session.get(Manual, manual_id)
    if not manual:
        raise HTTPException(status_code=404, detail="手册不存在")
    base = manual_summary(manual).model_dump()
    return ManualDetail(extraction_path=manual.extraction_path, error_message=manual.error_message, **base)


@router.post("/manuals/{manual_id}/embedding-index", response_model=EmbeddingJobResponse)
def build_manual_embedding_index(
    manual_id: str, session: Session = Depends(get_session)
) -> EmbeddingJobResponse:
    if not session.get(Manual, manual_id):
        raise HTTPException(status_code=404, detail="手册不存在")
    try:
        job = create_embedding_job(manual_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    start_embedding_worker(job.id)
    return embedding_job_response(job)


@router.get("/embedding-jobs/{job_id}", response_model=EmbeddingJobResponse)
def get_embedding_job(job_id: str, session: Session = Depends(get_session)) -> EmbeddingJobResponse:
    job = session.get(EmbeddingJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Embedding 任务不存在")
    return embedding_job_response(job)


@router.post("/manuals/upload", response_model=ImportJobResponse)
async def upload_manual(
    file: UploadFile = File(...),
    brand: str | None = None,
    release: str | None = None,
    session: Session = Depends(get_session),
) -> ImportJobResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少文件名")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(content) > 1024 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="单个手册超过 1 GiB 限制")
    manual, job, duplicate = create_manual_from_upload(session, file.filename, content, brand, release)
    session.commit()
    if not duplicate:
        start_import_worker(job.id)
    return job_response(job)


@router.get("/manual-imports/{job_id}", response_model=ImportJobResponse)
def get_import_job(job_id: str, session: Session = Depends(get_session)) -> ImportJobResponse:
    job = session.get(ImportJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="导入任务不存在")
    return job_response(job)


@router.post("/manual-imports/{job_id}/retry", response_model=ImportJobResponse)
def retry_import(job_id: str, session: Session = Depends(get_session)) -> ImportJobResponse:
    recover_interrupted_imports(session)
    job = session.get(ImportJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="导入任务不存在")
    if job.status.value == "running":
        raise HTTPException(status_code=409, detail="导入任务正在运行")
    start_import_worker(job.id)
    return job_response(job)


@router.get("/models", response_model=list[ModelResponse])
def list_models(
    published_only: bool = Query(default=False),
    manual_id: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> list[ModelResponse]:
    query = select(DeviceModel).order_by(DeviceModel.brand, DeviceModel.level, DeviceModel.canonical_name)
    if published_only:
        query = query.where(DeviceModel.review_status == ReviewStatus.published)
    if manual_id:
        query = query.where(DeviceModel.source_manual_id == manual_id)
    return [model_response(item) for item in session.scalars(query).all()]


@router.patch("/models/{model_id}", response_model=ModelResponse)
def patch_model(
    model_id: str,
    payload: ModelCorrectionRequest,
    session: Session = Depends(get_session),
) -> ModelResponse:
    model = session.get(DeviceModel, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="型号不存在")
    if payload.parent_id is not None:
        if payload.parent_id == model.id:
            raise HTTPException(status_code=400, detail="型号不能以自身为父级")
        parent = session.get(DeviceModel, payload.parent_id)
        if not parent:
            raise HTTPException(status_code=404, detail="父级型号不存在")
        model.parent_id = parent.id
    if payload.canonical_name:
        model.canonical_name = payload.canonical_name.upper().strip()
    for alias in payload.aliases_to_add:
        normalized = alias.upper().strip()
        if not normalized:
            continue
        exists = session.scalar(
            select(ModelAlias).where(ModelAlias.model_id == model.id, ModelAlias.alias == normalized)
        )
        if not exists:
            session.add(ModelAlias(model_id=model.id, alias=normalized, source="manual_overlay"))
    if payload.review_status:
        try:
            model.review_status = ReviewStatus(payload.review_status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="无效的发布状态") from exc
    session.commit()
    return model_response(model)


@router.get("/commands/search", response_model=CommandSearchResponse)
def search_commands(
    q: str = Query(min_length=1, max_length=300),
    manual_id: str | None = Query(default=None),
    model_id: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
) -> CommandSearchResponse:
    hits: list[CommandSearchHit] = []
    for hit in hybrid_command_search(
        session,
        query=q,
        manual_id=manual_id,
        model_id=model_id,
        limit=limit,
    ):
        command = hit.command
        evidence = json.loads(command.evidence_json)
        hits.append(
            CommandSearchHit(
                id=command.id,
                canonical_name=command.canonical_name,
                manual_id=command.manual_id,
                document_id=command.document_id,
                feature=command.feature,
                syntax=json.loads(command.syntax_json),
                views=json.loads(command.views_json),
                preconditions=json.loads(command.preconditions_json),
                constraints=json.loads(command.constraints_json),
                applicability_mode=command.applicability_mode,
                source_path=evidence.get("source_path", ""),
                score=hit.score,
                retrieval_sources=list(hit.sources),
            )
        )
    return CommandSearchResponse(query=q, hits=hits)


@router.post("/devices/huawei/read-only-probe", response_model=ReadOnlyProbeResponse)
def huawei_read_only_probe(payload: ReadOnlyProbeRequest) -> ReadOnlyProbeResponse:
    try:
        result = run_huawei_read_only_probe(
            host=payload.host,
            port=payload.port,
            username=payload.username,
            password=payload.password,
            command=payload.command,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"只读 SSH 探测失败：{exc}") from exc
    return ReadOnlyProbeResponse(
        command=result.command,
        output=result.output,
        detected_model=result.detected_model,
        detected_release=result.detected_release,
        warnings=result.warnings,
    )


@router.post("/topologies", response_model=TopologyResponse)
def post_topology(payload: TopologyDraft, session: Session = Depends(get_session)) -> TopologyResponse:
    try:
        revision = create_topology(session, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return TopologyResponse(
        id=revision.topology_id,
        name=revision.topology.name,
        revision_id=revision.id,
        revision=revision.revision,
        graph=TopologyDraft.model_validate(json.loads(revision.graph_json)),
    )


@router.post("/config-tasks", response_model=ConfigTaskResponse)
def post_config_task(
    payload: ConfigTaskCreate,
    session: Session = Depends(get_session),
) -> ConfigTaskResponse:
    try:
        task = create_config_task(session, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return config_task_response(task)


@router.get("/config-tasks/{task_id}", response_model=ConfigTaskResponse)
def get_config_task(task_id: str, session: Session = Depends(get_session)) -> ConfigTaskResponse:
    from app.models import ConfigTask

    task = session.get(ConfigTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="配置任务不存在")
    return config_task_response(task)


@router.post("/config-tasks/{task_id}/devices/{plan_id}/approve", response_model=DevicePlanResponse)
def approve_plan(
    task_id: str,
    plan_id: str,
    payload: DeviceApprovalRequest,
    session: Session = Depends(get_session),
) -> DevicePlanResponse:
    from app.models import DevicePlan

    plan = session.get(DevicePlan, plan_id)
    if not plan or plan.task_id != task_id:
        raise HTTPException(status_code=404, detail="设备计划不存在")
    try:
        updated = approve_device_plan(
            session,
            plan_id,
            payload.approval_revision,
            payload.command_overrides,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return device_plan_response(updated)


@router.post(
    "/config-tasks/{task_id}/devices/{plan_id}/execute-huawei",
    response_model=ExecutionRunResponse,
)
def execute_huawei_plan(
    task_id: str,
    plan_id: str,
    payload: DeviceExecutionRequest,
    session: Session = Depends(get_session),
) -> ExecutionRunResponse:
    try:
        execution = execute_huawei_device_plan(
            session,
            task_id=task_id,
            plan_id=plan_id,
            host=payload.host,
            port=payload.port,
            username=payload.username,
            password=payload.password,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return execution_response(execution)


@router.get(
    "/config-tasks/{task_id}/devices/{plan_id}/executions",
    response_model=list[ExecutionRunResponse],
)
def list_plan_executions(
    task_id: str,
    plan_id: str,
    session: Session = Depends(get_session),
) -> list[ExecutionRunResponse]:
    from app.models import DevicePlan

    plan = session.get(DevicePlan, plan_id)
    if not plan or plan.task_id != task_id:
        raise HTTPException(status_code=404, detail="设备计划不存在")
    return [execution_response(item) for item in sorted(plan.executions, key=lambda item: item.created_at)]


@router.get("/config-tasks/{task_id}/devices/{plan_id}/export")
def export_device_plan(
    task_id: str,
    plan_id: str,
    session: Session = Depends(get_session),
) -> Response:
    from app.models import DevicePlan

    plan = session.get(DevicePlan, plan_id)
    if not plan or plan.task_id != task_id:
        raise HTTPException(status_code=404, detail="设备计划不存在")
    task = plan.task
    lines = [
        "# AI Agent 工业交换机自动配置导出（不含凭据）",
        f"# task_id: {task.id}",
        f"# device: {plan.display_name}",
        f"# detected_model: {plan.detected_model or 'unresolved'}",
        f"# detected_release: {plan.detected_release or 'unresolved'}",
        f"# compatibility: {plan.compatibility_status.value}",
        f"# generated_at_utc: {datetime.utcnow().isoformat()}Z",
        "",
        "# 正向命令",
        *json.loads(plan.commands_json),
        "",
        "# 验证命令",
        *json.loads(plan.validation_json).get("validation_commands", []),
        "",
        "# 条件回滚草案（必须结合执行前快照人工审核）",
        *json.loads(plan.rollback_json).get("commands", []),
        "",
        "# 证据页",
        *[
            f"# {item.get('canonical_name')} -> {item.get('source_path')}"
            for item in json.loads(plan.evidence_json)
        ],
    ]
    return Response(
        content="\n".join(lines) + "\n",
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="device-plan-{plan.id[:8]}.txt"'},
    )


@router.post("/executions/{execution_id}/pc-ping", response_model=PcPingResponse)
def execute_pc_ping(
    execution_id: str,
    payload: PcPingRequest,
    session: Session = Depends(get_session),
) -> PcPingResponse:
    execution = session.get(ExecutionRun, execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="设备执行记录不存在")
    if execution.status.value != "completed":
        raise HTTPException(status_code=409, detail="设备未完成验证和 save，不能开始 PC 验收")
    try:
        result = run_pc_ping(
            host=payload.host,
            port=payload.port,
            username=payload.username,
            password=payload.password,
            os_family=payload.os_family,
            target_ip=payload.target_ip,
        )
        record = PcPingRun(
            execution_id=execution.id,
            source_host=payload.host,
            target_ip=payload.target_ip,
            command=result.command,
            output=result.output[-20_000:],
            success=result.success,
        )
    except Exception as exc:
        record = PcPingRun(
            execution_id=execution.id,
            source_host=payload.host,
            target_ip=payload.target_ip,
            command="",
            output="",
            success=False,
            error_message=str(exc)[:4000],
        )
    session.add(record)
    session.commit()
    return PcPingResponse(
        id=record.id,
        command=record.command,
        output=record.output,
        success=record.success,
        error_message=record.error_message,
    )
