from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.utcnow()


class ImportStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    completed_with_issues = "completed_with_issues"
    failed = "failed"
    cancelled = "cancelled"


class IndexStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


class ModelLevel(str, enum.Enum):
    series = "series"
    family = "family"
    sku = "sku"


class ReviewStatus(str, enum.Enum):
    candidate = "candidate"
    published = "published"
    rejected = "rejected"


class TaskStatus(str, enum.Enum):
    draft = "draft"
    planning = "planning"
    needs_review = "needs_review"
    blocked = "blocked"
    approved = "approved"
    executing = "executing"
    completed = "completed"
    failed = "failed"


class ExecutionStatus(str, enum.Enum):
    queued = "queued"
    preflight_blocked = "preflight_blocked"
    running = "running"
    validation_failed = "validation_failed"
    command_failed = "command_failed"
    completed = "completed"
    failed = "failed"


class CompatibilityStatus(str, enum.Enum):
    unresolved = "unresolved"
    exact = "exact"
    model_unpublished = "model_unpublished"
    series_only = "series_only"
    version_mismatch = "version_mismatch"
    incompatible = "incompatible"


class Manual(Base):
    __tablename__ = "manuals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    original_filename: Mapped[str] = mapped_column(String(512))
    stored_path: Mapped[str] = mapped_column(String(1024))
    source_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    file_format: Mapped[str] = mapped_column(String(32), index=True)
    brand: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    release: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    status: Mapped[ImportStatus] = mapped_column(Enum(ImportStatus), default=ImportStatus.queued, index=True)
    extraction_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    command_count: Mapped[int] = mapped_column(Integer, default=0)
    model_count: Mapped[int] = mapped_column(Integer, default=0)
    issue_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    documents: Mapped[list["KnowledgeDocument"]] = relationship(
        back_populates="manual", cascade="all, delete-orphan"
    )
    commands: Mapped[list["Command"]] = relationship(back_populates="manual", cascade="all, delete-orphan")


class ImportJob(Base):
    __tablename__ = "import_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    manual_id: Mapped[str] = mapped_column(ForeignKey("manuals.id", ondelete="CASCADE"), index=True)
    status: Mapped[ImportStatus] = mapped_column(Enum(ImportStatus), default=ImportStatus.queued, index=True)
    stage: Mapped[str] = mapped_column(String(80), default="queued")
    progress_current: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The import runs in a separate local process.  These fields make an interrupted
    # process visible and allow the next retry to resume persisted page batches.
    worker_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (UniqueConstraint("manual_id", "source_path", name="uq_document_manual_source_path"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    manual_id: Mapped[str] = mapped_column(ForeignKey("manuals.id", ondelete="CASCADE"), index=True)
    source_path: Mapped[str] = mapped_column(String(1024))
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    toc_path_json: Mapped[str] = mapped_column(Text, default="[]")
    page_type: Mapped[str] = mapped_column(String(64), default="topic", index=True)
    encoding: Mapped[str | None] = mapped_column(String(32), nullable=True)
    text_content: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    manual: Mapped[Manual] = relationship(back_populates="documents")
    commands: Mapped[list["Command"]] = relationship(back_populates="document")


class DeviceModel(Base):
    __tablename__ = "device_models"
    __table_args__ = (
        UniqueConstraint("brand", "canonical_name", "level", name="uq_device_model_brand_name_level"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    brand: Mapped[str] = mapped_column(String(120), index=True)
    canonical_name: Mapped[str] = mapped_column(String(255), index=True)
    level: Mapped[ModelLevel] = mapped_column(Enum(ModelLevel), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("device_models.id"), nullable=True, index=True)
    review_status: Mapped[ReviewStatus] = mapped_column(Enum(ReviewStatus), default=ReviewStatus.candidate)
    confidence: Mapped[int] = mapped_column(Integer, default=50)
    source_manual_id: Mapped[str | None] = mapped_column(ForeignKey("manuals.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    parent: Mapped["DeviceModel | None"] = relationship(remote_side="DeviceModel.id", backref="children")
    aliases: Mapped[list["ModelAlias"]] = relationship(back_populates="model", cascade="all, delete-orphan")
    evidence: Mapped[list["ModelEvidence"]] = relationship(
        cascade="all, delete-orphan", foreign_keys="ModelEvidence.model_id"
    )


class ModelAlias(Base):
    __tablename__ = "model_aliases"
    __table_args__ = (UniqueConstraint("model_id", "alias", name="uq_model_alias"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    model_id: Mapped[str] = mapped_column(ForeignKey("device_models.id", ondelete="CASCADE"), index=True)
    alias: Mapped[str] = mapped_column(String(255), index=True)
    source: Mapped[str] = mapped_column(String(32), default="extractor")
    model: Mapped[DeviceModel] = relationship(back_populates="aliases")


class ModelEvidence(Base):
    __tablename__ = "model_evidence"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    model_id: Mapped[str] = mapped_column(ForeignKey("device_models.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="SET NULL"),
        nullable=True,
    )
    evidence_text: Mapped[str] = mapped_column(Text)
    confidence: Mapped[int] = mapped_column(Integer, default=50)
    source_kind: Mapped[str] = mapped_column(String(64), default="text_match")


class Command(Base):
    __tablename__ = "commands"
    __table_args__ = (
        UniqueConstraint(
            "manual_id",
            "document_id",
            "canonical_name",
            name="uq_command_page_name",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    manual_id: Mapped[str] = mapped_column(ForeignKey("manuals.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        index=True,
    )
    canonical_name: Mapped[str] = mapped_column(String(512), index=True)
    feature: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    syntax_json: Mapped[str] = mapped_column(Text, default="[]")
    views_json: Mapped[str] = mapped_column(Text, default="[]")
    parameters_json: Mapped[str] = mapped_column(Text, default="[]")
    preconditions_json: Mapped[str] = mapped_column(Text, default="[]")
    constraints_json: Mapped[str] = mapped_column(Text, default="[]")
    examples_json: Mapped[str] = mapped_column(Text, default="[]")
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    applicability_mode: Mapped[str] = mapped_column(String(48), default="inherit_with_exceptions")
    extraction_confidence: Mapped[int] = mapped_column(Integer, default=70)
    manual: Mapped[Manual] = relationship(back_populates="commands")
    document: Mapped[KnowledgeDocument] = relationship(back_populates="commands")
    applicability: Mapped[list["CommandApplicability"]] = relationship(
        back_populates="command", cascade="all, delete-orphan"
    )


class CommandApplicability(Base):
    __tablename__ = "command_applicability"
    __table_args__ = (UniqueConstraint("command_id", "model_id", name="uq_command_model_applicability"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    command_id: Mapped[str] = mapped_column(ForeignKey("commands.id", ondelete="CASCADE"), index=True)
    model_id: Mapped[str] = mapped_column(ForeignKey("device_models.id", ondelete="CASCADE"), index=True)
    is_supported: Mapped[bool] = mapped_column(Boolean, default=True)
    evidence_text: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[int] = mapped_column(Integer, default=60)
    command: Mapped[Command] = relationship(back_populates="applicability")


class EmbeddingJob(Base):
    __tablename__ = "embedding_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    manual_id: Mapped[str] = mapped_column(ForeignKey("manuals.id", ondelete="CASCADE"), index=True)
    model: Mapped[str] = mapped_column(String(255))
    status: Mapped[IndexStatus] = mapped_column(Enum(IndexStatus), default=IndexStatus.queued, index=True)
    progress_current: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    worker_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CommandEmbedding(Base):
    """CPU-searchable Float32 command vector; no external vector database."""

    __tablename__ = "command_embeddings"
    __table_args__ = (
        UniqueConstraint("command_id", "model", name="uq_command_embedding_model"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    command_id: Mapped[str] = mapped_column(ForeignKey("commands.id", ondelete="CASCADE"), index=True)
    manual_id: Mapped[str] = mapped_column(ForeignKey("manuals.id", ondelete="CASCADE"), index=True)
    model: Mapped[str] = mapped_column(String(255), index=True)
    dimensions: Mapped[int] = mapped_column(Integer)
    source_hash: Mapped[str] = mapped_column(String(64), index=True)
    vector_blob: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    is_secret_reference: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Topology(Base):
    __tablename__ = "topologies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), default="未命名拓扑")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    revisions: Mapped[list["TopologyRevision"]] = relationship(
        back_populates="topology", cascade="all, delete-orphan"
    )


class TopologyRevision(Base):
    __tablename__ = "topology_revisions"
    __table_args__ = (UniqueConstraint("topology_id", "revision", name="uq_topology_revision"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    topology_id: Mapped[str] = mapped_column(ForeignKey("topologies.id", ondelete="CASCADE"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    graph_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    topology: Mapped[Topology] = relationship(back_populates="revisions")
    tasks: Mapped[list["ConfigTask"]] = relationship(back_populates="topology_revision")


class ConfigTask(Base):
    __tablename__ = "config_tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    topology_revision_id: Mapped[str] = mapped_column(
        ForeignKey("topology_revisions.id", ondelete="RESTRICT"), index=True
    )
    manual_id: Mapped[str] = mapped_column(ForeignKey("manuals.id", ondelete="RESTRICT"), index=True)
    requirement_text: Mapped[str] = mapped_column(Text)
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.draft, index=True)
    intent_json: Mapped[str] = mapped_column(Text, default="{}")
    blocking_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    topology_revision: Mapped[TopologyRevision] = relationship(back_populates="tasks")
    device_plans: Mapped[list["DevicePlan"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class DevicePlan(Base):
    __tablename__ = "device_plans"
    __table_args__ = (UniqueConstraint("task_id", "device_node_id", name="uq_task_device_plan"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("config_tasks.id", ondelete="CASCADE"), index=True)
    device_node_id: Mapped[str] = mapped_column(String(255), index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    detected_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detected_release: Mapped[str | None] = mapped_column(String(120), nullable=True)
    mapped_series: Mapped[str | None] = mapped_column(String(120), nullable=True)
    compatibility_status: Mapped[CompatibilityStatus] = mapped_column(
        Enum(CompatibilityStatus), default=CompatibilityStatus.unresolved, index=True
    )
    compatibility_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    intent_json: Mapped[str] = mapped_column(Text, default="{}")
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    commands_json: Mapped[str] = mapped_column(Text, default="[]")
    validation_json: Mapped[str] = mapped_column(Text, default="{}")
    rollback_json: Mapped[str] = mapped_column(Text, default="{}")
    approval_revision: Mapped[int] = mapped_column(Integer, default=0)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    task: Mapped[ConfigTask] = relationship(back_populates="device_plans")
    executions: Mapped[list["ExecutionRun"]] = relationship(
        back_populates="device_plan", cascade="all, delete-orphan"
    )


class ExecutionRun(Base):
    """One user-confirmed, single-device execution attempt.

    Credentials never appear here.  Command and output records are retained only
    for audit/debugging under the local application's data directory.
    """

    __tablename__ = "execution_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("config_tasks.id", ondelete="RESTRICT"), index=True)
    device_plan_id: Mapped[str] = mapped_column(
        ForeignKey("device_plans.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[ExecutionStatus] = mapped_column(
        Enum(ExecutionStatus), default=ExecutionStatus.queued, index=True
    )
    target_host: Mapped[str] = mapped_column(String(255))
    target_port: Mapped[int] = mapped_column(Integer, default=22)
    execution_revision: Mapped[int] = mapped_column(Integer)
    preflight_json: Mapped[str] = mapped_column(Text, default="{}")
    validation_json: Mapped[str] = mapped_column(Text, default="{}")
    save_json: Mapped[str] = mapped_column(Text, default="{}")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    device_plan: Mapped[DevicePlan] = relationship(back_populates="executions")
    commands: Mapped[list["ExecutionCommand"]] = relationship(
        back_populates="execution", cascade="all, delete-orphan"
    )


class ExecutionCommand(Base):
    __tablename__ = "execution_commands"
    __table_args__ = (UniqueConstraint("execution_id", "sequence", name="uq_execution_command_sequence"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    execution_id: Mapped[str] = mapped_column(
        ForeignKey("execution_runs.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    phase: Mapped[str] = mapped_column(String(32))
    command: Mapped[str] = mapped_column(Text)
    output: Mapped[str] = mapped_column(Text, default="")
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    execution: Mapped[ExecutionRun] = relationship(back_populates="commands")


class PcPingRun(Base):
    __tablename__ = "pc_ping_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    execution_id: Mapped[str] = mapped_column(
        ForeignKey("execution_runs.id", ondelete="CASCADE"), index=True
    )
    source_host: Mapped[str] = mapped_column(String(255))
    target_ip: Mapped[str] = mapped_column(String(255))
    command: Mapped[str] = mapped_column(Text)
    output: Mapped[str] = mapped_column(Text, default="")
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
