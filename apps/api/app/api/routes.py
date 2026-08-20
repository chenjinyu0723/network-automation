from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.archive_service import (
    delete_manual_with_contents,
    export_manual,
    export_template,
    export_topology,
    import_manual,
    import_template,
    import_topology,
    persist_export,
    persist_export_to_destination,
)
from app.db import SessionLocal, get_session
from app.execution.readonly import run_huawei_read_only_probe
from app.execution.service import (
    execute_huawei_device_plan,
    execute_huawei_undo_plan,
    queue_huawei_device_plan,
    queue_huawei_undo_plan,
)
from app.ingestion.pipeline import (
    ImportFailure,
    create_manual_from_upload,
    recover_interrupted_imports,
    start_import_worker,
)
from app.llm.client import request_embeddings, request_text_result, should_enable_thinking
from app.models import (
    ConfigTask,
    ConfigurationTemplate,
    DeviceModel,
    EmbeddingJob,
    ExecutionCommand,
    ExecutionRun,
    ExecutionStatus,
    ImportJob,
    ImportStatus,
    Manual,
    ModelAlias,
    PlanningEvent,
    ReviewStatus,
    TaskStatus,
    Topology,
    TopologyRevision,
)
from app.planning.runtime import (
    PlanningCancelled,
    append_event,
    finish_run,
    get_run,
    make_event_sink,
    request_cancel,
    start_run,
)
from app.planning.service import (
    approve_device_plan,
    cancel_config_task,
    create_config_task_record,
    create_topology,
    generate_config_commands,
    generate_planning_idea,
    get_topology_revision,
    update_planning_idea,
    update_topology,
)
from app.retrieval.active import active_manual_search
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
    EmbeddingConnectionTestResponse,
    EmbeddingJobResponse,
    ExecutionRunResponse,
    ExportSaveRequest,
    ExportSaveResponse,
    ImportJobResponse,
    LlmConnectionTestResponse,
    ManualActiveSearchRequest,
    ManualActiveSearchResponse,
    ManualDetail,
    ManualSummary,
    ManualUpdateRequest,
    ModelCorrectionRequest,
    ModelResponse,
    PlanningEventResponse,
    PlanningIdeaUpdateRequest,
    ProviderSettingsInput,
    ProviderSettingsResponse,
    ReadOnlyProbeRequest,
    ReadOnlyProbeResponse,
    TemplateCreateFromTaskRequest,
    TemplateDetail,
    TemplateSummary,
    TemplateUpdateRequest,
    TopologyDraft,
    TopologyResponse,
    TopologySummary,
)
from app.services.settings import get_provider_secret, read_provider_settings, save_provider_settings
from app.template_service import (
    create_template_from_task,
    delete_template,
    sanitize_template_snapshot,
    update_template,
)

router = APIRouter(prefix="/api")


def manual_summary(manual: Manual) -> ManualSummary:
    return ManualSummary(
        id=manual.id,
        original_filename=manual.original_filename,
        file_format=manual.file_format,
        brand=manual.brand,
        release=manual.release,
        cli_profile=manual.cli_profile,
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
    graph = json.loads(plan.task.topology_revision.graph_json)
    node = next((item for item in graph.get("nodes", []) if item.get("id") == plan.device_node_id), {})
    connection_hint = {
        "host": node.get("ssh_host"),
        "port": node.get("ssh_port") or 22,
        "username": node.get("ssh_username"),
    }
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
        connection_hint=connection_hint,
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
        operation=execution.operation,
        target_host=execution.target_host,
        target_port=execution.target_port,
        execution_revision=execution.execution_revision,
        preflight=json.loads(execution.preflight_json),
        validation=json.loads(execution.validation_json),
        save=json.loads(execution.save_json),
        error_message=execution.error_message,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
        created_at=execution.created_at,
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


def _run_huawei_execution_worker(
    *,
    operation: str,
    execution_id: str,
    task_id: str,
    plan_id: str,
    host: str,
    port: int,
    username: str,
    password: str,
) -> None:
    """Run one local SSH operation after its queued record is visible to SSE."""

    with SessionLocal() as worker_session:
        runner = execute_huawei_undo_plan if operation == "undo" else execute_huawei_device_plan
        runner(
            worker_session,
            task_id=task_id,
            plan_id=plan_id,
            host=host,
            port=port,
            username=username,
            password=password,
            execution_id=execution_id,
        )


def _start_huawei_execution_worker(
    *,
    operation: str,
    execution_id: str,
    task_id: str,
    plan_id: str,
    payload: DeviceExecutionRequest,
) -> None:
    threading.Thread(
        target=_run_huawei_execution_worker,
        kwargs={
            "operation": operation,
            "execution_id": execution_id,
            "task_id": task_id,
            "plan_id": plan_id,
            "host": payload.host,
            "port": payload.port,
            "username": payload.username,
            "password": payload.password,
        },
        name=f"network-automation-{operation}-{execution_id[:8]}",
        daemon=True,
    ).start()


def _mark_config_task_failed(session: Session, task_id: str, detail: str) -> None:
    """Persist a terminal result when an asynchronous planner exits unexpectedly."""

    session.rollback()
    task = session.get(ConfigTask, task_id)
    if not task or task.status == TaskStatus.cancelled:
        return
    task.status = TaskStatus.failed
    task.blocking_reason = detail
    task.cancel_requested = False
    task.cancel_reason = None
    session.commit()
    append_event(session, task_id, "错误", "error", detail)


def _run_config_idea_worker(task_id: str) -> None:
    """Generate the idea outside the POST so the UI can stop/restart it."""

    run = get_run(task_id)
    if not run:
        return
    with SessionLocal() as worker_session:
        event_sink = make_event_sink(worker_session, task_id, run.cancel_event.is_set)
        try:
            if run.cancel_event.is_set():
                raise PlanningCancelled("用户已停止配置规划")
            generate_planning_idea(
                worker_session,
                task_id,
                event_sink=event_sink,
                cancel_event=run.cancel_event,
            )
        except PlanningCancelled:
            worker_session.rollback()
            # A replacement run may already own the same task id.  The old
            # provider request is still allowed to unwind, but must not mark
            # the new run cancelled or append stale UI events.
            if get_run(task_id) is not run:
                return
            task = worker_session.get(ConfigTask, task_id)
            if task and task.status != TaskStatus.cancelled:
                task.status = TaskStatus.cancelled
                task.cancel_requested = True
                task.cancel_reason = "用户停止了配置规划"
                task.blocking_reason = task.cancel_reason
                worker_session.commit()
                append_event(worker_session, task_id, "已停止", "cancelled", "配置思路生成已停止。")
        except ValueError as exc:
            if get_run(task_id) is not run:
                return
            _mark_config_task_failed(worker_session, task_id, f"配置思路生成失败：{str(exc)[:240]}")
        except Exception as exc:
            if get_run(task_id) is not run:
                return
            _mark_config_task_failed(worker_session, task_id, f"配置思路生成失败：{str(exc)[:240]}")
        finally:
            finish_run(task_id, run)


def _start_config_idea_worker(task_id: str) -> None:
    threading.Thread(
        target=_run_config_idea_worker,
        args=(task_id,),
        name=f"network-automation-idea-{task_id[:8]}",
        daemon=True,
    ).start()


def _run_config_command_worker(task_id: str) -> None:
    """Generate CLI outside the request lifetime so slow LLM calls cannot time out HTTP."""

    run = get_run(task_id)
    if not run:
        return
    with SessionLocal() as worker_session:
        event_sink = make_event_sink(worker_session, task_id, run.cancel_event.is_set)
        try:
            # A stop request can arrive after the route queues the worker but before
            # this thread gets CPU time.  Never revive a task that is already stopped.
            if run.cancel_event.is_set():
                raise PlanningCancelled("用户已停止配置规划")
            generate_config_commands(
                worker_session,
                task_id,
                event_sink=event_sink,
                cancel_event=run.cancel_event,
            )
        except PlanningCancelled:
            worker_session.rollback()
            if get_run(task_id) is not run:
                return
            task = worker_session.get(ConfigTask, task_id)
            if task and task.status != TaskStatus.cancelled:
                task.status = TaskStatus.cancelled
                task.cancel_requested = True
                task.cancel_reason = "用户停止了配置规划"
                task.blocking_reason = task.cancel_reason
                worker_session.commit()
                append_event(worker_session, task_id, "已停止", "cancelled", "命令生成已停止。")
        except ValueError as exc:
            if get_run(task_id) is not run:
                return
            _mark_config_task_failed(worker_session, task_id, f"命令生成失败：{str(exc)[:240]}")
        except Exception as exc:
            if get_run(task_id) is not run:
                return
            _mark_config_task_failed(worker_session, task_id, f"命令生成失败：{str(exc)[:240]}")
        finally:
            finish_run(task_id, run)


def _start_config_command_worker(task_id: str) -> None:
    threading.Thread(
        target=_run_config_command_worker,
        args=(task_id,),
        name=f"network-automation-command-plan-{task_id[:8]}",
        daemon=True,
    ).start()


def config_task_response(task) -> ConfigTaskResponse:  # type: ignore[no-untyped-def]
    return ConfigTaskResponse(
        id=task.id,
        topology_revision_id=task.topology_revision_id,
        manual_id=task.manual_id,
        requirement_text=task.requirement_text,
        status=task.status.value,
        intent=json.loads(task.intent_json),
        planning_idea=task.planning_idea,
        planning_idea_revision=task.planning_idea_revision,
        planning_idea_confirmed_at=task.planning_idea_confirmed_at,
        blocking_reason=task.blocking_reason,
        cancel_requested=task.cancel_requested,
        cancel_reason=task.cancel_reason,
        device_plans=[device_plan_response(item) for item in task.device_plans],
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def planning_event_response(event: PlanningEvent) -> PlanningEventResponse:
    return PlanningEventResponse(
        id=event.id,
        task_id=event.task_id,
        sequence=event.sequence,
        stage=event.stage,
        event_type=event.event_type,
        content=event.content,
        created_at=event.created_at,
    )


def template_summary_response(template: ConfigurationTemplate) -> TemplateSummary:
    snapshot = json.loads(template.snapshot_json or "{}")
    return TemplateSummary(
        id=template.id,
        title=template.title,
        description=template.description,
        source_task_id=template.source_task_id,
        manual_name=template.manual_name,
        device_plan_count=len(snapshot.get("device_plans") or []),
        created_at=template.created_at,
        updated_at=template.updated_at,
    )


def template_detail_response(template: ConfigurationTemplate) -> TemplateDetail:
    snapshot = sanitize_template_snapshot(json.loads(template.snapshot_json or "{}"))
    try:
        topology = TopologyDraft.model_validate(snapshot.get("topology") or {})
    except Exception as exc:
        raise HTTPException(status_code=500, detail="配置模板快照中的拓扑数据无效") from exc
    return TemplateDetail(
        **template_summary_response(template).model_dump(),
        topology=topology,
        requirement_text=str(snapshot.get("requirement_text") or ""),
        planning_idea=str(snapshot.get("planning_idea") or ""),
        device_plans=list(snapshot.get("device_plans") or []),
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


@router.post("/settings/providers/test-llm", response_model=LlmConnectionTestResponse)
def test_llm_provider(session: Session = Depends(get_session)) -> LlmConnectionTestResponse:
    settings = read_provider_settings(session)
    secret = get_provider_secret("llm")
    if not settings.llm_base_url or not settings.llm_model or not secret:
        raise HTTPException(status_code=409, detail="请先保存 LLM Base URL、模型名和 API Key。")
    try:
        result = asyncio.run(
            request_text_result(
                base_url=settings.llm_base_url,
                api_key=secret,
                model=settings.llm_model,
                messages=[
                    {"role": "system", "content": "只回复 OK。"},
                    {"role": "user", "content": "连通性检查"},
                ],
                temperature=0,
                thinking=should_enable_thinking(settings.llm_thinking_mode, "intent_refinement"),
            )
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LLM 调用失败：{str(exc)[:240]}") from exc
    return LlmConnectionTestResponse(
        status="ok",
        model=settings.llm_model,
        thinking_requested=result.thinking_requested,
        thinking_used=result.thinking_used,
        thinking_fallback=result.thinking_fallback,
        detail=result.fallback_reason,
    )


@router.post("/settings/providers/test-embedding", response_model=EmbeddingConnectionTestResponse)
def test_embedding_provider(session: Session = Depends(get_session)) -> EmbeddingConnectionTestResponse:
    settings = read_provider_settings(session)
    secret = get_provider_secret("embedding")
    if not settings.embedding_base_url or not settings.embedding_model or not secret:
        raise HTTPException(status_code=409, detail="请先保存 Embedding Base URL、模型名和 API Key。")
    try:
        vectors = asyncio.run(
            request_embeddings(
                base_url=settings.embedding_base_url,
                api_key=secret,
                model=settings.embedding_model,
                inputs=["network automation embedding connectivity check"],
                dimensions=settings.embedding_dimensions,
            )
        )
        if len(vectors) != 1 or not vectors[0]:
            raise ValueError("Embedding 接口未返回有效向量。")
        dimensions = len(vectors[0])
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Embedding 调用失败：{str(exc)[:240]}") from exc
    return EmbeddingConnectionTestResponse(
        status="ok",
        model=settings.embedding_model,
        dimensions=dimensions,
        requested_dimensions=settings.embedding_dimensions,
    )


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


@router.patch("/manuals/{manual_id}", response_model=ManualSummary)
def patch_manual(
    manual_id: str,
    payload: ManualUpdateRequest,
    session: Session = Depends(get_session),
) -> ManualSummary:
    manual = session.get(Manual, manual_id)
    if not manual:
        raise HTTPException(status_code=404, detail="手册不存在")
    filename = payload.original_filename.strip()
    if not filename:
        raise HTTPException(status_code=400, detail="手册名称不能为空")
    manual.original_filename = filename
    manual.brand = payload.brand.strip() or None if payload.brand else None
    manual.release = payload.release.strip() or None if payload.release else None
    manual.cli_profile = payload.cli_profile
    session.commit()
    session.refresh(manual)
    return manual_summary(manual)


@router.delete("/manuals/{manual_id}", status_code=204)
def delete_manual(manual_id: str, session: Session = Depends(get_session)) -> Response:
    manual = session.get(Manual, manual_id)
    if not manual:
        raise HTTPException(status_code=404, detail="手册不存在")
    try:
        if session.scalar(select(ConfigTask.id).where(ConfigTask.manual_id == manual.id)):
            raise HTTPException(status_code=409, detail="手册已被配置任务引用，不能删除；请先处理相关任务。")
        delete_manual_with_contents(session, manual)
    except HTTPException:
        raise
    except Exception as exc:
        session.rollback()
        raise HTTPException(
            status_code=409, detail="手册已被配置任务引用，不能删除；请先处理相关任务。"
        ) from exc
    return Response(status_code=204)


@router.get("/manuals/{manual_id}/export")
def export_manual_archive(manual_id: str, session: Session = Depends(get_session)) -> Response:
    try:
        content = export_manual(session, manual_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    manual = session.get(Manual, manual_id)
    filename = f"manual-{manual.id[:8]}.manual.zip"
    export_path = persist_export(content, filename)
    return Response(
        content=content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Network-Automation-Export-Path": quote(str(export_path), safe=""),
        },
    )


@router.post("/manuals/{manual_id}/export", response_model=ExportSaveResponse)
def save_manual_archive(
    manual_id: str,
    payload: ExportSaveRequest,
    session: Session = Depends(get_session),
) -> ExportSaveResponse:
    try:
        saved_path = persist_export_to_destination(
            export_manual(session, manual_id), payload.destination_path
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ExportSaveResponse(saved_path=str(saved_path))


@router.post("/manuals/import")
async def import_manual_archive(
    file: UploadFile = File(...),
    overwrite: bool = Query(default=False),
    session: Session = Depends(get_session),
) -> ManualSummary:
    content = await file.read()
    try:
        manual = import_manual(session, content, overwrite=overwrite)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return manual_summary(manual)


@router.post("/manuals/{manual_id}/active-search", response_model=ManualActiveSearchResponse)
def active_search_manual(
    manual_id: str,
    payload: ManualActiveSearchRequest,
    session: Session = Depends(get_session),
) -> ManualActiveSearchResponse:
    manual = session.get(Manual, manual_id)
    if not manual:
        raise HTTPException(status_code=404, detail="手册不存在")
    if manual.status not in {ImportStatus.completed, ImportStatus.completed_with_issues}:
        raise HTTPException(status_code=400, detail="手册尚未完成抽取，不能执行主动检索")
    return ManualActiveSearchResponse.model_validate(
        active_manual_search(session, manual_id=manual.id, requirement=payload.requirement_text)
    )


@router.post("/manuals/{manual_id}/embedding-index", response_model=EmbeddingJobResponse)
def build_manual_embedding_index(
    manual_id: str, session: Session = Depends(get_session)
) -> EmbeddingJobResponse:
    if not session.get(Manual, manual_id):
        raise HTTPException(status_code=404, detail="手册不存在")
    try:
        job, created = create_embedding_job(manual_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if created:
        start_embedding_worker(job.id)
    return embedding_job_response(job)


@router.get("/embedding-jobs", response_model=list[EmbeddingJobResponse])
def list_embedding_jobs(
    manual_id: str | None = None, session: Session = Depends(get_session)
) -> list[EmbeddingJobResponse]:
    statement = select(EmbeddingJob)
    if manual_id:
        statement = statement.where(EmbeddingJob.manual_id == manual_id)
    jobs = session.scalars(statement.order_by(EmbeddingJob.created_at.desc())).all()
    return [embedding_job_response(job) for job in jobs]


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
    try:
        manual, job, duplicate = create_manual_from_upload(session, file.filename, content, brand, release)
    except ImportFailure as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    if not duplicate:
        try:
            start_import_worker(job.id)
        except OSError as exc:
            job.status = ImportStatus.failed
            job.stage = "worker_start_failed"
            job.detail = f"无法启动本地导入进程：{exc}"
            job.finished_at = datetime.utcnow()
            manual.status = ImportStatus.failed
            manual.error_message = job.detail
            session.commit()
    return job_response(job)


@router.get("/manual-imports", response_model=list[ImportJobResponse])
def list_import_jobs(
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
) -> list[ImportJobResponse]:
    jobs = session.scalars(select(ImportJob).order_by(ImportJob.created_at.desc()).limit(limit)).all()
    return [job_response(job) for job in jobs]


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
    try:
        start_import_worker(job.id)
    except OSError as exc:
        job.status = ImportStatus.failed
        job.stage = "worker_start_failed"
        job.detail = f"无法启动本地导入进程：{exc}"
        job.finished_at = datetime.utcnow()
        manual = session.get(Manual, job.manual_id)
        if manual:
            manual.status = ImportStatus.failed
            manual.error_message = job.detail
        session.commit()
    return job_response(job)


@router.get("/models", response_model=list[ModelResponse])
def list_models(
    published_only: bool = Query(default=False),
    manual_id: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> list[ModelResponse]:
    query = select(DeviceModel).order_by(
        DeviceModel.confidence.desc(),
        DeviceModel.brand,
        DeviceModel.level,
        DeviceModel.canonical_name,
    )
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


def topology_response(revision) -> TopologyResponse:  # type: ignore[no-untyped-def]
    return TopologyResponse(
        id=revision.topology_id,
        name=revision.topology.name,
        revision_id=revision.id,
        revision=revision.revision,
        graph=TopologyDraft.model_validate(json.loads(revision.graph_json)),
    )


@router.get("/topologies", response_model=list[TopologySummary])
def list_topologies(session: Session = Depends(get_session)) -> list[TopologySummary]:
    results: list[TopologySummary] = []
    for topology in session.scalars(select(Topology).order_by(Topology.updated_at.desc())).all():
        revision = get_topology_revision(session, topology.id)
        if revision:
            results.append(
                TopologySummary(
                    id=topology.id,
                    name=topology.name,
                    revision_id=revision.id,
                    revision=revision.revision,
                    updated_at=topology.updated_at,
                )
            )
    return results


@router.get("/topologies/{topology_id}", response_model=TopologyResponse)
def get_topology(topology_id: str, session: Session = Depends(get_session)) -> TopologyResponse:
    revision = get_topology_revision(session, topology_id)
    if not revision:
        raise HTTPException(status_code=404, detail="拓扑不存在")
    return topology_response(revision)


@router.post("/topologies", response_model=TopologyResponse)
def post_topology(payload: TopologyDraft, session: Session = Depends(get_session)) -> TopologyResponse:
    try:
        revision = create_topology(session, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return topology_response(revision)


@router.put("/topologies/{topology_id}", response_model=TopologyResponse)
def put_topology(
    topology_id: str,
    payload: TopologyDraft,
    session: Session = Depends(get_session),
) -> TopologyResponse:
    try:
        revision = update_topology(session, topology_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=404 if "不存在" in str(exc) else 400, detail=str(exc)) from exc
    return topology_response(revision)


@router.delete("/topologies/{topology_id}", status_code=204)
def delete_topology(topology_id: str, session: Session = Depends(get_session)) -> Response:
    topology = session.get(Topology, topology_id)
    if not topology:
        raise HTTPException(status_code=404, detail="拓扑不存在")
    if session.scalar(
        select(ConfigTask.id)
        .join(TopologyRevision, ConfigTask.topology_revision_id == TopologyRevision.id)
        .where(TopologyRevision.topology_id == topology_id)
    ):
        raise HTTPException(status_code=409, detail="拓扑已有配置任务引用，不能删除；请先删除相关任务。")
    session.delete(topology)
    session.commit()
    return Response(status_code=204)


@router.get("/topologies/{topology_id}/export")
def export_topology_archive(topology_id: str, session: Session = Depends(get_session)) -> Response:
    try:
        payload = export_topology(session, topology_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    name = str(payload["topology"].get("name") or topology_id[:8])
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    export_path = persist_export(content, f"{name}.topology.json")
    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="topology-{topology_id[:8]}.topology.json"',
            "X-Network-Automation-Export-Path": quote(str(export_path), safe=""),
        },
    )


@router.post("/topologies/{topology_id}/export", response_model=ExportSaveResponse)
def save_topology_archive(
    topology_id: str,
    payload: ExportSaveRequest,
    session: Session = Depends(get_session),
) -> ExportSaveResponse:
    try:
        content = json.dumps(export_topology(session, topology_id), ensure_ascii=False, indent=2).encode(
            "utf-8"
        )
        saved_path = persist_export_to_destination(content, payload.destination_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ExportSaveResponse(saved_path=str(saved_path))


@router.post("/topologies/import", response_model=TopologyResponse)
async def import_topology_archive(
    file: UploadFile = File(...),
    overwrite: bool = Query(default=False),
    session: Session = Depends(get_session),
) -> TopologyResponse:
    try:
        payload = json.loads((await file.read()).decode("utf-8"))
        revision = import_topology(session, payload, overwrite=overwrite)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return topology_response(revision)


@router.post("/config-tasks", response_model=ConfigTaskResponse)
def post_config_task(
    payload: ConfigTaskCreate,
    session: Session = Depends(get_session),
) -> ConfigTaskResponse:
    from app.models import new_id

    task_id = payload.task_id or new_id()
    if session.get(ConfigTask, task_id):
        raise HTTPException(status_code=409, detail="规划任务 ID 已存在，请重新开始任务")
    payload.task_id = task_id
    start_run(task_id)
    run_started = True
    try:
        task = create_config_task_record(session, payload)
        append_event(session, task_id, "任务创建", "stage", "配置思路任务已提交，正在后台初始化。")
        _start_config_idea_worker(task_id)
        run_started = False  # The background worker owns registry cleanup.
    except ValueError as exc:
        if run_started:
            finish_run(task_id)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        if run_started:
            finish_run(task_id)
        raise
    return config_task_response(task)


@router.post("/config-tasks/{task_id}/cancel", response_model=ConfigTaskResponse)
def post_cancel_config_task(
    task_id: str,
    session: Session = Depends(get_session),
) -> ConfigTaskResponse:
    try:
        task = cancel_config_task(session, task_id)
    except ValueError as exc:
        raise HTTPException(status_code=409 if "没有正在运行" in str(exc) else 404, detail=str(exc)) from exc
    request_cancel(task_id)
    append_event(session, task_id, "已停止", "cancelled", "用户请求停止配置规划。")
    return config_task_response(task)


@router.get("/config-tasks/{task_id}/events")
async def stream_config_task_events(
    task_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
) -> StreamingResponse:
    async def event_stream():  # type: ignore[no-untyped-def]
        sequence = after
        terminal = {
            TaskStatus.idea_ready,
            TaskStatus.needs_review,
            TaskStatus.blocked,
            TaskStatus.cancelled,
            TaskStatus.failed,
        }
        while True:
            if await request.is_disconnected():
                return
            with SessionLocal() as event_session:
                events = event_session.scalars(
                    select(PlanningEvent)
                    .where(PlanningEvent.task_id == task_id, PlanningEvent.sequence > sequence)
                    .order_by(PlanningEvent.sequence)
                ).all()
                task = event_session.get(ConfigTask, task_id)
                payloads = [planning_event_response(item).model_dump(mode="json") for item in events]
                task_status = task.status if task else None
            for payload in payloads:
                sequence = int(payload["sequence"])
                yield f"event: planning\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
            if task_status in terminal and not payloads:
                complete_payload = {"task_id": task_id, "status": task_status.value}
                yield f"event: complete\ndata: {json.dumps(complete_payload, ensure_ascii=False)}\n\n"
                return
            if not payloads:
                yield ": keepalive\n\n"
            await asyncio.sleep(0.35)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/templates", response_model=list[TemplateSummary])
def list_templates(session: Session = Depends(get_session)) -> list[TemplateSummary]:
    templates = session.scalars(
        select(ConfigurationTemplate).order_by(ConfigurationTemplate.updated_at.desc())
    ).all()
    return [template_summary_response(item) for item in templates]


@router.get("/templates/{template_id}", response_model=TemplateDetail)
def get_template(template_id: str, session: Session = Depends(get_session)) -> TemplateDetail:
    template = session.get(ConfigurationTemplate, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="配置模板不存在")
    return template_detail_response(template)


@router.get("/templates/{template_id}/export")
def export_template_archive(template_id: str, session: Session = Depends(get_session)) -> Response:
    try:
        content = export_template(session, template_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    template = session.get(ConfigurationTemplate, template_id)
    filename = (template.title if template else template_id[:8]).strip() or template_id[:8]
    export_path = persist_export(content, f"{filename}.template.json")
    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="template-{template_id[:8]}.template.json"',
            "X-Network-Automation-Export-Path": quote(str(export_path), safe=""),
        },
    )


@router.post("/templates/{template_id}/export", response_model=ExportSaveResponse)
def save_template_archive(
    template_id: str,
    payload: ExportSaveRequest,
    session: Session = Depends(get_session),
) -> ExportSaveResponse:
    try:
        saved_path = persist_export_to_destination(
            export_template(session, template_id), payload.destination_path
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ExportSaveResponse(saved_path=str(saved_path))


@router.post("/templates/import", response_model=TemplateSummary)
async def import_template_archive(
    file: UploadFile = File(...),
    overwrite: bool = Query(default=False),
    session: Session = Depends(get_session),
) -> TemplateSummary:
    try:
        payload = json.loads((await file.read()).decode("utf-8"))
        template = import_template(session, payload, overwrite=overwrite)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return template_summary_response(template)


@router.put("/templates/{template_id}", response_model=TemplateSummary)
def put_template(
    template_id: str,
    payload: TemplateUpdateRequest,
    session: Session = Depends(get_session),
) -> TemplateSummary:
    try:
        template = update_template(
            session,
            template_id=template_id,
            title=payload.title,
            description=payload.description,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return template_summary_response(template)


@router.delete("/templates/{template_id}", status_code=204)
def remove_template(template_id: str, session: Session = Depends(get_session)) -> Response:
    try:
        delete_template(session, template_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=204)


@router.post("/config-tasks/{task_id}/templates", response_model=TemplateDetail)
def post_template_from_config_task(
    task_id: str,
    payload: TemplateCreateFromTaskRequest,
    session: Session = Depends(get_session),
) -> TemplateDetail:
    try:
        template = create_template_from_task(
            session,
            task_id=task_id,
            title=payload.title,
            description=payload.description,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400 if "不能保存" in str(exc) else 404, detail=str(exc)) from exc
    return template_detail_response(template)


@router.put("/config-tasks/{task_id}/planning-idea", response_model=ConfigTaskResponse)
def put_planning_idea(
    task_id: str,
    payload: PlanningIdeaUpdateRequest,
    session: Session = Depends(get_session),
) -> ConfigTaskResponse:
    try:
        task = update_planning_idea(session, task_id, payload.planning_idea)
    except ValueError as exc:
        raise HTTPException(status_code=409 if "已执行" in str(exc) else 404, detail=str(exc)) from exc
    return config_task_response(task)


@router.post("/config-tasks/{task_id}/generate-commands", response_model=ConfigTaskResponse)
def post_generate_config_commands(
    task_id: str,
    payload: PlanningIdeaUpdateRequest,
    session: Session = Depends(get_session),
) -> ConfigTaskResponse:
    run_started = False
    try:
        active_run = get_run(task_id)
        if active_run:
            existing = session.get(ConfigTask, task_id)
            if existing and existing.status == TaskStatus.planning:
                raise HTTPException(status_code=409, detail="当前任务正在生成；已继续订阅右侧进度")
            # A worker can finish between its final database commit and
            # registry cleanup. Replacing its run token below is atomic under
            # the runtime lock; do not create a transient "no owner" window
            # in which the old worker could mark the new task cancelled.
        # Register the cancellation token before changing task state so a stop
        # request cannot land in the small queue-to-worker window.
        start_run(task_id)
        run_started = True
        task = update_planning_idea(session, task_id, payload.planning_idea)
        if not task.planning_idea:
            raise ValueError("配置思路为空；请先生成或填写配置思路，再生成命令")
        # Make the queued state visible before the worker starts.  This enables
        # immediate cancellation and gives SSE a first progress event even when
        # the provider has a long delay before its first streamed token.
        task.status = TaskStatus.planning
        task.cancel_requested = False
        task.cancel_reason = None
        task.planning_idea_confirmed_at = datetime.utcnow()
        task.blocking_reason = None
        session.commit()
        session.refresh(task)
        # A restart is a fresh UI run: discard the old progress timeline so
        # SSE replay cannot mix a cancelled/previous attempt into the new one.
        session.execute(delete(PlanningEvent).where(PlanningEvent.task_id == task_id))
        session.commit()
        append_event(session, task_id, "任务准备", "stage", "命令生成任务已提交，正在后台初始化。")
        _start_config_command_worker(task_id)
        run_started = False  # The worker owns cleanup after it has been started.
    except ValueError as exc:
        if run_started:
            finish_run(task_id)
        raise HTTPException(status_code=409 if "已执行" in str(exc) else 400, detail=str(exc)) from exc
    except Exception:
        if run_started:
            finish_run(task_id)
        raise
    return config_task_response(task)


@router.get("/config-tasks", response_model=list[ConfigTaskResponse])
def list_config_tasks(
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
) -> list[ConfigTaskResponse]:
    tasks = session.scalars(select(ConfigTask).order_by(ConfigTask.updated_at.desc()).limit(limit)).all()
    return [config_task_response(task) for task in tasks]


@router.get("/config-tasks/{task_id}", response_model=ConfigTaskResponse)
def get_config_task(task_id: str, session: Session = Depends(get_session)) -> ConfigTaskResponse:
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
        execution = queue_huawei_device_plan(
            session,
            task_id=task_id,
            plan_id=plan_id,
            host=payload.host,
            port=payload.port,
            execution_id=payload.execution_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _start_huawei_execution_worker(
        operation="apply",
        execution_id=execution.id,
        task_id=task_id,
        plan_id=plan_id,
        payload=payload,
    )
    return execution_response(execution)


@router.post(
    "/config-tasks/{task_id}/devices/{plan_id}/undo-huawei",
    response_model=ExecutionRunResponse,
)
def undo_huawei_plan(
    task_id: str,
    plan_id: str,
    payload: DeviceExecutionRequest,
    session: Session = Depends(get_session),
) -> ExecutionRunResponse:
    try:
        execution = queue_huawei_undo_plan(
            session,
            task_id=task_id,
            plan_id=plan_id,
            host=payload.host,
            port=payload.port,
            execution_id=payload.execution_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _start_huawei_execution_worker(
        operation="undo",
        execution_id=execution.id,
        task_id=task_id,
        plan_id=plan_id,
        payload=payload,
    )
    return execution_response(execution)


@router.get("/executions/{execution_id}/events")
async def stream_execution_events(
    execution_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
) -> StreamingResponse:
    async def event_stream():  # type: ignore[no-untyped-def]
        sequence = after
        terminal = {
            ExecutionStatus.preflight_blocked,
            ExecutionStatus.validation_failed,
            ExecutionStatus.command_failed,
            ExecutionStatus.completed,
            ExecutionStatus.failed,
        }
        while True:
            if await request.is_disconnected():
                return
            with SessionLocal() as event_session:
                execution = event_session.get(ExecutionRun, execution_id)
                entries = []
                status = None
                complete_payload = None
                if execution:
                    entries = event_session.scalars(
                        select(ExecutionCommand)
                        .where(
                            ExecutionCommand.execution_id == execution_id,
                            ExecutionCommand.sequence > sequence,
                        )
                        .order_by(ExecutionCommand.sequence)
                    ).all()
                    status = execution.status
                    if status in terminal:
                        complete_payload = execution_response(execution).model_dump(mode="json")
                payloads = [
                    {
                        "sequence": entry.sequence,
                        "phase": entry.phase,
                        "command": entry.command,
                        "output": entry.output,
                        "success": entry.success,
                    }
                    for entry in entries
                ]
            for payload in payloads:
                sequence = int(payload["sequence"])
                yield f"event: execution\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
            if status in terminal and not payloads and complete_payload:
                serialized = json.dumps(complete_payload, ensure_ascii=False, default=str)
                yield f"event: complete\ndata: {serialized}\n\n"
                return
            if not payloads:
                yield ": keepalive\n\n"
            await asyncio.sleep(0.35)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
