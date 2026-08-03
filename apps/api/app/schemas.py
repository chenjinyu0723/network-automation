from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ManualSummary(BaseModel):
    id: str
    original_filename: str
    file_format: str
    brand: str | None
    release: str | None
    status: str
    page_count: int
    command_count: int
    model_count: int
    issue_count: int
    created_at: datetime
    updated_at: datetime


class ManualDetail(ManualSummary):
    extraction_path: str | None
    error_message: str | None


class ImportJobResponse(BaseModel):
    id: str
    manual_id: str
    status: str
    stage: str
    progress_current: int
    progress_total: int
    detail: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class EmbeddingJobResponse(BaseModel):
    id: str
    manual_id: str
    model: str
    status: str
    progress_current: int
    progress_total: int
    detail: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ModelResponse(BaseModel):
    id: str
    brand: str
    canonical_name: str
    level: str
    parent_id: str | None
    review_status: str
    confidence: int
    source_manual_id: str | None
    aliases: list[str] = Field(default_factory=list)
    evidence_count: int = 0


class ModelCorrectionRequest(BaseModel):
    parent_id: str | None = None
    review_status: str | None = None
    canonical_name: str | None = Field(default=None, min_length=1, max_length=255)
    aliases_to_add: list[str] = Field(default_factory=list, max_length=30)


class CommandSearchHit(BaseModel):
    id: str
    canonical_name: str
    manual_id: str
    document_id: str
    feature: str | None
    syntax: list[str]
    views: list[str]
    preconditions: list[str]
    constraints: list[str]
    applicability_mode: str
    source_path: str
    score: float | None = None
    retrieval_sources: list[str] = Field(default_factory=list)


class CommandSearchResponse(BaseModel):
    query: str
    hits: list[CommandSearchHit]


class ProviderSettingsInput(BaseModel):
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_temperature: float = Field(default=0.2, ge=0, le=2)
    llm_thinking_mode: Literal["adaptive", "always", "off"] = "adaptive"
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    embedding_dimensions: int | None = Field(default=None, ge=1)
    # Incoming secrets are accepted but never persisted in the database.
    llm_api_key: str | None = Field(default=None, exclude=True)
    embedding_api_key: str | None = Field(default=None, exclude=True)


class ProviderSettingsResponse(BaseModel):
    llm_base_url: str | None
    llm_model: str | None
    llm_temperature: float
    llm_thinking_mode: Literal["adaptive", "always", "off"]
    embedding_base_url: str | None
    embedding_model: str | None
    embedding_dimensions: int | None
    llm_api_key_configured: bool
    embedding_api_key_configured: bool


class TopologyNodeInput(BaseModel):
    id: str
    kind: str
    name: str
    x: float
    y: float
    model_id: str | None = None
    ip: str | None = None
    prefix: int | None = Field(default=None, ge=0, le=128)
    gateway: str | None = None
    ssh_host: str | None = None
    ssh_port: int | None = Field(default=None, ge=1, le=65535)
    ssh_username: str | None = None
    detected_model: str | None = None
    detected_release: str | None = None
    protected_ports: list[str] = Field(default_factory=list)


class TopologyLinkInput(BaseModel):
    id: str
    source: str
    source_port: str
    target: str
    target_port: str


class TopologyDraft(BaseModel):
    name: str = Field(default="未命名拓扑", min_length=1, max_length=255)
    nodes: list[TopologyNodeInput]
    links: list[TopologyLinkInput]


class TopologyResponse(BaseModel):
    id: str
    name: str
    revision_id: str
    revision: int
    graph: TopologyDraft


class ConfigTaskCreate(BaseModel):
    topology_revision_id: str
    manual_id: str
    requirement_text: str = Field(min_length=3, max_length=20_000)


class DevicePlanResponse(BaseModel):
    id: str
    device_node_id: str
    display_name: str
    detected_model: str | None
    detected_release: str | None
    mapped_series: str | None
    compatibility_status: str
    compatibility_reason: str | None
    intent: dict[str, Any]
    command_plan: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]]
    commands: list[str]
    validation: dict[str, Any]
    rollback: dict[str, Any]
    approval_revision: int
    approved_at: datetime | None


class ConfigTaskResponse(BaseModel):
    id: str
    topology_revision_id: str
    manual_id: str
    requirement_text: str
    status: str
    intent: dict[str, Any]
    blocking_reason: str | None
    device_plans: list[DevicePlanResponse]
    created_at: datetime
    updated_at: datetime


class DeviceApprovalRequest(BaseModel):
    approval_revision: int = Field(ge=0)
    command_overrides: list[str] | None = None


class ReadOnlyProbeRequest(BaseModel):
    host: str
    port: int = Field(default=22, ge=1, le=65535)
    username: str
    password: str = Field(exclude=True)
    command: str = "display version"


class ReadOnlyProbeResponse(BaseModel):
    command: str
    output: str
    detected_model: str | None = None
    detected_release: str | None = None
    warnings: list[str] = []


class DeviceExecutionRequest(BaseModel):
    host: str
    port: int = Field(default=22, ge=1, le=65535)
    username: str
    password: str = Field(exclude=True)


class ExecutionCommandResponse(BaseModel):
    sequence: int
    phase: str
    command: str
    output: str
    success: bool


class ExecutionRunResponse(BaseModel):
    id: str
    task_id: str
    device_plan_id: str
    status: str
    target_host: str
    target_port: int
    execution_revision: int
    preflight: dict[str, Any]
    validation: dict[str, Any]
    save: dict[str, Any]
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None
    commands: list[ExecutionCommandResponse]


class PcPingRequest(BaseModel):
    host: str
    port: int = Field(default=22, ge=1, le=65535)
    username: str
    password: str = Field(exclude=True)
    os_family: str = Field(pattern="^(linux|windows)$")
    target_ip: str


class PcPingResponse(BaseModel):
    id: str
    command: str
    output: str
    success: bool
    error_message: str | None


class IntentInstruction(BaseModel):
    action: str
    args: dict[str, Any] = Field(default_factory=dict)
    reason_summary: str = ""


class LlmIntentRefinement(BaseModel):
    """The only LLM output schema accepted by the first planning workflow."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["refine_intent"]
    feature: Literal["vlan_access", "unclassified"]
    vlan_ids: list[Annotated[int, Field(ge=1, le=4094)]] = Field(default_factory=list, max_length=10)
    retrieval_terms: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        default_factory=list,
        max_length=8,
    )
    reason_summary: str = Field(default="", max_length=300)


class CommandInvocation(BaseModel):
    """A handbook command reference plus validated arguments, never raw CLI."""

    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(min_length=1, max_length=64)
    syntax_index: int = Field(default=0, ge=0, le=20)
    arguments: dict[str, Any] = Field(default_factory=dict)
    target_port_ref: str | None = Field(default=None, max_length=255)


class CommandOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: str = Field(min_length=1, max_length=200)
    invocations: list[CommandInvocation] = Field(min_length=1, max_length=8)


class LlmCommandPlan(BaseModel):
    """LLM command plan; the deterministic compiler is the only CLI producer."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["command_plan"]
    operations: list[CommandOperation] = Field(min_length=1, max_length=20)
    verification_notes: list[str] = Field(default_factory=list, max_length=8)
    assumptions: list[str] = Field(default_factory=list, max_length=8)
    risks: list[str] = Field(default_factory=list, max_length=8)


class LlmCommandReview(BaseModel):
    """Independent review of a compiled command set; no command generation."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["command_review"]
    verdict: Literal["approve", "reject"]
    issues: list[str] = Field(default_factory=list, max_length=12)
    required_changes: list[str] = Field(default_factory=list, max_length=8)
    reason_summary: str = Field(default="", max_length=400)
