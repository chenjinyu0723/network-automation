from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import paths
from app.db import SessionLocal, engine
from app.ingestion.chm import (
    MODEL_TOKEN_RE,
    ParsedPage,
    classify_model_token,
    infer_family_name,
    infer_series,
    iter_html_pages,
    json_dump,
    parse_html_page,
    parse_toc,
    read_text_with_fallback,
)
from app.models import (
    Command,
    CommandApplicability,
    DeviceModel,
    ImportJob,
    ImportStatus,
    KnowledgeDocument,
    Manual,
    ModelAlias,
    ModelEvidence,
    ModelLevel,
    ReviewStatus,
)


class ImportFailure(RuntimeError):
    pass


def sha256_file(source: Path) -> str:
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def detect_format(filename: str) -> str:
    extension = Path(filename).suffix.lower()
    return {
        ".chm": "chm",
        ".html": "html",
        ".htm": "html",
        ".txt": "text",
        ".md": "text",
        ".pdf": "pdf",
    }.get(extension, "unknown")


def _find_7z() -> str:
    candidates = [
        shutil.which("7z"),
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise ImportFailure("找不到 7-Zip。导入 CHM 前请安装 7-Zip 并确保 7z.exe 可用。")


def _set_job(
    session: Session,
    job: ImportJob,
    *,
    status: ImportStatus | None = None,
    stage: str | None = None,
    current: int | None = None,
    total: int | None = None,
    detail: str | None = None,
) -> None:
    if status is not None:
        job.status = status
    if stage is not None:
        job.stage = stage
    if current is not None:
        job.progress_current = current
    if total is not None:
        job.progress_total = total
    if detail is not None:
        job.detail = detail
    if job.status == ImportStatus.running:
        job.heartbeat_at = datetime.utcnow()
    session.commit()


def start_import_worker(job_id: str) -> None:
    """Start a durable local child process for an import job.

    FastAPI reloads and browser disconnects must not terminate a ten-minute CHM
    parse.  The job state and page batches live in SQLite; the child only needs an
    id and can therefore be retried or resumed safely.
    """

    log_path = paths.logs / f"manual-import-{job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "app.ingestion.worker", "--job-id", job_id]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with log_path.open("a", encoding="utf-8") as log_file:
        subprocess.Popen(  # noqa: S603 - fixed interpreter/module; job id is UUID-valued database data.
            command,
            cwd=paths.data_root.parent / "apps" / "api",
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )


def _process_is_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        # ``os.kill(pid, 0)`` is available on Windows and does not send a signal.
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def recover_interrupted_imports(session: Session) -> int:
    """Make jobs abandoned by a terminated local worker retryable.

    Documents are retained as committed batches.  A retry starts from those pages
    instead of deleting the manual, so it is safe to call during application boot
    and just before a user requests retry.
    """

    jobs = session.scalars(select(ImportJob).where(ImportJob.status == ImportStatus.running)).all()
    recovered = 0
    for job in jobs:
        if _process_is_alive(job.worker_pid):
            continue
        job.status = ImportStatus.failed
        job.stage = "interrupted"
        job.detail = "本地导入进程已中断；可重试并从已持久化页面继续。"
        job.finished_at = datetime.utcnow()
        job.worker_pid = None
        manual = session.get(Manual, job.manual_id)
        if manual:
            manual.status = ImportStatus.failed
            manual.error_message = job.detail
        recovered += 1
    if recovered:
        session.commit()
    return recovered


def _extract_chm(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    command = [_find_7z(), "x", str(source), f"-o{destination}", "-y"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
    if result.returncode != 0:
        raise ImportFailure(f"7-Zip 解包失败（code={result.returncode}）：{result.stderr[-1000:]}")
    if not any(destination.rglob("*.html")):
        raise ImportFailure("CHM 解包后未发现 HTML 页面；拒绝发布空知识库。")


def _upsert_model(
    session: Session,
    *,
    brand: str,
    canonical_name: str,
    level: ModelLevel,
    parent: DeviceModel | None,
    manual_id: str,
    confidence: int,
    publish: bool,
) -> DeviceModel:
    item = session.scalar(
        select(DeviceModel).where(
            DeviceModel.brand == brand,
            DeviceModel.canonical_name == canonical_name,
            DeviceModel.level == level,
        )
    )
    if item is None:
        item = DeviceModel(
            brand=brand,
            canonical_name=canonical_name,
            level=level,
            parent_id=parent.id if parent else None,
            review_status=ReviewStatus.published if publish else ReviewStatus.candidate,
            confidence=confidence,
            source_manual_id=manual_id,
        )
        session.add(item)
        session.flush()
    elif parent and item.parent_id is None:
        item.parent_id = parent.id
    if publish and item.review_status == ReviewStatus.candidate:
        item.review_status = ReviewStatus.published
    item.confidence = max(item.confidence, confidence)
    alias = session.scalar(
        select(ModelAlias).where(ModelAlias.model_id == item.id, ModelAlias.alias == canonical_name)
    )
    if alias is None:
        session.add(ModelAlias(model_id=item.id, alias=canonical_name, source="extractor"))
    return item


def _record_model_evidence(
    session: Session, model: DeviceModel, document_id: str, text: str, confidence: int
) -> None:
    existing = session.scalar(
        select(ModelEvidence).where(
            ModelEvidence.model_id == model.id,
            ModelEvidence.document_id == document_id,
            ModelEvidence.evidence_text == text,
        )
    )
    if existing is None:
        session.add(
            ModelEvidence(
                model_id=model.id,
                document_id=document_id,
                evidence_text=text[:2000],
                confidence=confidence,
            )
        )


def _model_for_token(
    session: Session,
    manual_id: str,
    document_id: str,
    brand: str,
    token: str,
) -> DeviceModel | None:
    series_name = infer_series(token)
    if not series_name:
        return None
    series = _upsert_model(
        session,
        brand=brand,
        canonical_name=series_name,
        level=ModelLevel.series,
        parent=None,
        manual_id=manual_id,
        confidence=100,
        publish=True,
    )
    if token.upper() == series_name:
        return series
    kind = classify_model_token(token)
    if kind == "family":
        model = _upsert_model(
            session,
            brand=brand,
            canonical_name=token,
            level=ModelLevel.family,
            parent=series,
            manual_id=manual_id,
            confidence=75,
            publish=False,
        )
        _record_model_evidence(session, model, document_id, token, 75)
        return model
    inferred_family = infer_family_name(token)
    family: DeviceModel | None = None
    if inferred_family:
        family = _upsert_model(
            session,
            brand=brand,
            canonical_name=inferred_family,
            level=ModelLevel.family,
            parent=series,
            manual_id=manual_id,
            confidence=40,
            publish=False,
        )
        _record_model_evidence(
            session,
            family,
            document_id,
            f"由 SKU {token} 自动推导产品族 {inferred_family}",
            40,
        )
    model = _upsert_model(
        session,
        brand=brand,
        canonical_name=token,
        level=ModelLevel.sku,
        parent=family or series,
        manual_id=manual_id,
        confidence=65,
        publish=False,
    )
    _record_model_evidence(session, model, document_id, token, 65)
    return model


def _persist_page(
    session: Session,
    manual: Manual,
    page: ParsedPage,
    brand: str,
) -> tuple[KnowledgeDocument, Command | None]:
    document = KnowledgeDocument(
        manual_id=manual.id,
        source_path=page.source_path,
        title=page.title,
        toc_path_json=json_dump(page.toc_path),
        page_type=page.page_type,
        encoding=page.encoding,
        text_content=page.text_content,
        metadata_json=json_dump(page.metadata),
    )
    session.add(document)
    session.flush()
    token_models: dict[str, DeviceModel] = {}
    for token in page.model_tokens:
        model = _model_for_token(session, manual.id, document.id, brand, token)
        if model:
            token_models[token] = model
    command: Command | None = None
    if page.command:
        command = Command(
            manual_id=manual.id,
            document_id=document.id,
            canonical_name=str(page.command["canonical_name"]),
            feature=page.command["feature"] if isinstance(page.command["feature"], str) else None,
            syntax_json=json_dump(page.command["syntax"]),
            views_json=json_dump(page.command["views"]),
            parameters_json=json_dump(page.command["parameters"]),
            preconditions_json=json_dump(page.command["preconditions"]),
            constraints_json=json_dump(page.command["constraints"]),
            examples_json=json_dump(page.command["examples"]),
            evidence_json=json_dump(
                {
                    "source_path": page.source_path,
                    "toc_path": page.toc_path,
                    "support_sentences": page.support_sentences,
                }
            ),
            applicability_mode="explicit_allow" if page.support_sentences else "inherit_with_exceptions",
            extraction_confidence=90 if page.command["syntax"] and page.command["views"] else 65,
        )
        session.add(command)
        session.flush()
        if page.support_sentences:
            for token, model in token_models.items():
                # Only tokens backed by a command support sentence represent an explicit allow relation.
                if any(token in sentence.upper() for sentence in page.support_sentences):
                    supporting_sentence = next(
                        sentence for sentence in page.support_sentences if token in sentence.upper()
                    )
                    session.add(
                        CommandApplicability(
                            command_id=command.id,
                            model_id=model.id,
                            is_supported=True,
                            evidence_text=supporting_sentence,
                            confidence=85,
                        )
                    )
    return document, command


def _refresh_fts(connection, command: Command, document: KnowledgeDocument) -> None:
    content = "\n".join(
        [
            command.canonical_name,
            command.feature or "",
            document.title or "",
            document.text_content,
            command.syntax_json,
            command.preconditions_json,
            command.constraints_json,
        ]
    )
    connection.exec_driver_sql(
        "INSERT INTO command_search(command_id, manual_id, content) VALUES (?, ?, ?)",
        (command.id, command.manual_id, content),
    )


def repair_command_index(manual_id: str) -> dict[str, int]:
    """Repair imports made by early parser revisions and rebuild the FTS rows deterministically."""

    with SessionLocal() as session:
        commands = session.scalars(select(Command).where(Command.manual_id == manual_id)).all()
        invalid_ids = [
            command.id
            for command in commands
            if command.document.source_path.lower().endswith("_title.html")
            or not json.loads(command.syntax_json)
        ]
        if invalid_ids:
            session.execute(delete(Command).where(Command.id.in_(invalid_ids)))
            session.commit()
        remaining = session.scalars(select(Command).where(Command.manual_id == manual_id)).all()
        for command in remaining:
            syntax = [line for line in json.loads(command.syntax_json) if line != "命令格式"]
            views = [line for line in json.loads(command.views_json) if line != "视图"]
            command.syntax_json = json_dump(syntax)
            command.views_json = json_dump(views)
        session.commit()
        with engine.begin() as connection:
            connection.exec_driver_sql("DELETE FROM command_search WHERE manual_id = ?", (manual_id,))
            for command in remaining:
                _refresh_fts(connection, command, command.document)
        manual = session.get(Manual, manual_id)
        if manual:
            manual.command_count = len(remaining)
            session.commit()
        return {"removed": len(invalid_ids), "commands": len(remaining)}


def repair_command_syntax(
    manual_id: str, *, after_command_id: str | None = None, batch_size: int = 100
) -> dict[str, int | str | None | bool]:
    """Repair a bounded batch so command grammar upgrades are restart-safe.

    The caller passes back ``next_after_command_id`` until ``complete`` is true;
    each batch commits before its FTS refresh, so an interrupted repair is safe.
    """

    with SessionLocal() as session:
        manual = session.get(Manual, manual_id)
        if not manual or not manual.extraction_path:
            raise ImportFailure("手册没有可用的解包目录，无法修复命令语法。")
        root = Path(manual.extraction_path)
        toc_paths, _entries = parse_toc(root)
        query = select(Command).where(Command.manual_id == manual_id).order_by(Command.id).limit(batch_size)
        if after_command_id:
            query = query.where(Command.id > after_command_id)
        commands = session.scalars(query).all()
        updated = 0
        for command in commands:
            page_path = root / command.document.source_path
            if not page_path.exists():
                continue
            page = parse_html_page(page_path, root, toc_paths)
            if page.command and page.command["syntax"] != json.loads(command.syntax_json):
                command.syntax_json = json_dump(page.command["syntax"])
                updated += 1
        session.commit()
        with engine.begin() as connection:
            for command in commands:
                connection.exec_driver_sql("DELETE FROM command_search WHERE command_id = ?", (command.id,))
                _refresh_fts(connection, command, command.document)
        next_after_command_id = commands[-1].id if commands else after_command_id
        return {
            "updated": updated,
            "processed": len(commands),
            "next_after_command_id": next_after_command_id,
            "complete": len(commands) < batch_size,
        }


def repair_model_catalog(manual_id: str) -> dict[str, int]:
    """Remove parser artifacts where a series heading was duplicated as a product family."""

    with SessionLocal() as session:
        duplicates = session.scalars(
            select(DeviceModel).where(
                DeviceModel.source_manual_id == manual_id,
                DeviceModel.level == ModelLevel.family,
                DeviceModel.canonical_name.in_(["S1700", "S5700", "S6700"]),
            )
        ).all()
        removed = 0
        for duplicate in duplicates:
            if duplicate.children:
                continue
            session.delete(duplicate)
            removed += 1
        session.flush()
        manual = session.get(Manual, manual_id)
        if manual:
            manual.model_count = session.scalar(
                select(__import__("sqlalchemy").func.count(DeviceModel.id)).where(
                    DeviceModel.source_manual_id == manual_id
                )
            ) or 0
        session.commit()
        return {"removed": removed, "models": manual.model_count if manual else 0}


def _import_html_tree(session: Session, job: ImportJob, manual: Manual, root: Path, brand: str) -> None:
    toc_paths, _entries = parse_toc(root)
    pages = list(iter_html_pages(root))
    if not pages:
        raise ImportFailure("未发现可解析的 HTML 页面。")
    existing_paths = set(
        session.scalars(
            select(KnowledgeDocument.source_path).where(KnowledgeDocument.manual_id == manual.id)
        ).all()
    )
    remaining = [
        page_path for page_path in pages if page_path.relative_to(root).as_posix() not in existing_paths
    ]
    existing_command_count = session.scalar(
        select(__import__("sqlalchemy").func.count(Command.id)).where(Command.manual_id == manual.id)
    ) or 0
    command_count = existing_command_count
    issue_count = manual.issue_count
    _set_job(
        session,
        job,
        stage="parse_html",
        current=len(existing_paths),
        total=len(pages),
        detail=("从已持久化页面继续解析" if existing_paths else "正在解析目录与页面"),
    )
    pending_fts: list[tuple[Command, KnowledgeDocument]] = []
    for completed, page_path in enumerate(remaining, start=len(existing_paths) + 1):
        try:
            page = parse_html_page(page_path, root, toc_paths)
            document, command = _persist_page(session, manual, page, brand)
            if command:
                command_count += 1
                pending_fts.append((command, document))
        except Exception as exc:  # The importer must retain progress rather than discard a whole manual.
            issue_count += 1
            job.detail = f"解析 {page_path.name} 失败：{exc}"
        if completed % 100 == 0 or completed == len(pages):
            session.commit()
            with engine.begin() as connection:
                for command, document in pending_fts:
                    _refresh_fts(connection, command, document)
            pending_fts.clear()
            _set_job(session, job, current=completed, detail=f"已解析 {completed}/{len(pages)} 页")
    manual.page_count = len(pages)
    manual.command_count = command_count
    manual.issue_count = issue_count


def _import_text(session: Session, job: ImportJob, manual: Manual, source: Path, brand: str) -> None:
    text, encoding = read_text_with_fallback(source)
    document = KnowledgeDocument(
        manual_id=manual.id,
        source_path=source.name,
        title=source.stem,
        toc_path_json=json_dump([source.stem]),
        page_type="text",
        encoding=encoding,
        text_content=text,
        metadata_json="{}",
    )
    session.add(document)
    manual.page_count = 1
    manual.command_count = 0
    for token in {item.upper() for item in MODEL_TOKEN_RE.findall(text)}:
        _model_for_token(session, manual.id, document.id, brand, token)
    _set_job(session, job, stage="parse_text", current=1, total=1, detail="文本手册已入库")


def _import_pdf(session: Session, job: ImportJob, manual: Manual, source: Path, brand: str) -> None:
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover - dependency declared in pyproject
        raise ImportFailure("PDF Adapter 缺少 PyMuPDF 依赖。") from exc
    pdf = fitz.open(source)
    _set_job(session, job, stage="parse_pdf", current=0, total=pdf.page_count, detail="提取 PDF 文本")
    for number, page in enumerate(pdf, start=1):
        text = page.get_text("text")
        document = KnowledgeDocument(
            manual_id=manual.id,
            source_path=f"{source.name}#page={number}",
            title=f"{source.stem} / 第 {number} 页",
            toc_path_json=json_dump([source.stem, f"第 {number} 页"]),
            page_type="pdf_page",
            encoding="pdf_text",
            text_content=text,
            metadata_json=json_dump({"page": number}),
        )
        session.add(document)
        for token in {item.upper() for item in MODEL_TOKEN_RE.findall(text)}:
            _model_for_token(session, manual.id, document.id, brand, token)
        if number % 50 == 0 or number == pdf.page_count:
            session.commit()
            _set_job(session, job, current=number, detail=f"已提取 {number}/{pdf.page_count} 页")
    manual.page_count = pdf.page_count
    manual.command_count = 0


def run_import(job_id: str) -> None:
    """Run one persisted job; safe to invoke from the dedicated local worker."""

    with SessionLocal() as session:
        job = session.get(ImportJob, job_id)
        if job is None or job.status == ImportStatus.cancelled:
            return
        if job.status == ImportStatus.running and job.worker_pid not in {None, os.getpid()}:
            return
        manual = session.get(Manual, job.manual_id)
        if manual is None:
            return
        try:
            job.worker_pid = os.getpid()
            job.started_at = job.started_at or datetime.utcnow()
            manual.status = ImportStatus.running
            manual.error_message = None
            _set_job(session, job, status=ImportStatus.running, stage="prepare", detail="准备导入或恢复")
            source = Path(manual.stored_path)
            brand = (
                (manual.brand or "Huawei")
                if manual.file_format == "chm"
                else (manual.brand or "Unknown")
            )
            if manual.file_format == "chm":
                destination = paths.manuals_extracted / manual.id
                if not destination.exists() or not any(destination.rglob("*.html")):
                    _set_job(session, job, stage="extract_chm", detail="使用 7-Zip 解包 CHM")
                    _extract_chm(source, destination)
                manual.extraction_path = str(destination)
                _import_html_tree(session, job, manual, destination, brand)
            elif manual.file_format == "html":
                destination = paths.manuals_extracted / manual.id
                destination.mkdir(parents=True, exist_ok=True)
                copied = destination / source.name
                if not copied.exists():
                    shutil.copy2(source, copied)
                manual.extraction_path = str(destination)
                _import_html_tree(session, job, manual, destination, brand)
            elif manual.file_format == "text":
                document_exists = session.scalar(
                    select(KnowledgeDocument.id).where(KnowledgeDocument.manual_id == manual.id)
                )
                if not document_exists:
                    _import_text(session, job, manual, source, brand)
            elif manual.file_format == "pdf":
                document_exists = session.scalar(
                    select(KnowledgeDocument.id).where(KnowledgeDocument.manual_id == manual.id)
                )
                if not document_exists:
                    _import_pdf(session, job, manual, source, brand)
            else:
                raise ImportFailure("当前仅支持 CHM、HTML、TXT、Markdown 和 PDF。")
            manual.model_count = session.scalar(
                select(__import__("sqlalchemy").func.count(DeviceModel.id)).where(
                    DeviceModel.source_manual_id == manual.id
                )
            ) or 0
            manual.status = (
                ImportStatus.completed_with_issues if manual.issue_count else ImportStatus.completed
            )
            job.finished_at = datetime.utcnow()
            job.worker_pid = None
            _set_job(
                session,
                job,
                status=manual.status,
                stage="completed",
                current=job.progress_total,
                detail=f"完成：{manual.command_count} 条命令，{manual.model_count} 个型号候选",
            )
            session.commit()
        except Exception as exc:
            session.rollback()
            job = session.get(ImportJob, job_id)
            manual = session.get(Manual, job.manual_id) if job else None
            if job:
                job.status = ImportStatus.failed
                job.stage = "failed"
                job.detail = str(exc)[:4000]
                job.finished_at = datetime.utcnow()
                job.worker_pid = None
            if manual:
                manual.status = ImportStatus.failed
                manual.error_message = str(exc)[:4000]
            session.commit()


def create_manual_from_upload(session: Session, filename: str, content: bytes, brand: str | None = None,
                              release: str | None = None) -> tuple[Manual, ImportJob, bool]:
    file_format = detect_format(filename)
    digest = hashlib.sha256(content).hexdigest()
    existing = session.scalar(select(Manual).where(Manual.source_sha256 == digest))
    if existing:
        job = session.scalar(
            select(ImportJob).where(ImportJob.manual_id == existing.id).order_by(ImportJob.created_at.desc())
        )
        if job is None:
            job = ImportJob(manual_id=existing.id, status=existing.status, stage="deduplicated")
            session.add(job)
            session.flush()
        return existing, job, True
    safe_name = Path(filename).name
    destination = paths.manuals_original / f"{digest}_{safe_name}"
    destination.write_bytes(content)
    manual = Manual(
        original_filename=safe_name,
        stored_path=str(destination),
        source_sha256=digest,
        file_format=file_format,
        brand=brand,
        release=release,
        status=ImportStatus.queued,
    )
    session.add(manual)
    session.flush()
    job = ImportJob(manual_id=manual.id, status=ImportStatus.queued, stage="queued")
    session.add(job)
    session.flush()
    return manual, job, False
