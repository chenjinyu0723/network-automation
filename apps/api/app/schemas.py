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
    cli_profile: str
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


class ManualActiveSearchRequest(BaseModel):
    requirement_text: str = Field(min_length=3, max_length=5_000)


class ManualActiveSearchCandidate(BaseModel):
    kind: Literal["command", "document"]
    command_id: str | None
    document_id: str
    canonical_name: str | None
    syntax: list[str] = Field(default_factory=list)
    source_path: str
    title: str
    excerpt: str
    score: float
    retrieval_sources: list[str] = Field(default_factory=list)


class ManualActiveSearchResponse(BaseModel):
    status: Literal["found", "incomplete", "not_found"]
    selected_command_ids: list[str] = Field(default_factory=list)
    candidates: list[ManualActiveSearchCandidate] = Field(default_factory=list)
    rounds: list[dict[str, Any]] = Field(default_factory=list)


class ProviderSettingsInput(BaseModel):
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_temperature: float = Field(default=0.2, ge=0, le=2)
    llm_thinking_mode: Literal["adaptive", "always", "off"] = "adaptive"
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    embedding_dimensions: int | None = Field(default=None, ge=1)
    embedding_batch_size: int = Field(default=2, ge=1, le=20)
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
    embedding_batch_size: int
    llm_api_key_configured: bool
    embedding_api_key_configured: bool


class LlmConnectionTestResponse(BaseModel):
    status: Literal["ok"]
    model: str
    thinking_requested: bool
    thinking_used: bool
    thinking_fallback: bool
    detail: str | None = None


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


class TopologySummary(BaseModel):
    id: str
    name: str
    revision_id: str
    revision: int
    updated_at: datetime


class ConfigTaskCreate(BaseModel):
    task_id: str | None = Field(default=None, min_length=16, max_length=64)
    topology_revision_id: str
    manual_id: str
    template_id: str | None = None
    requirement_text: str = Field(min_length=3, max_length=20_000)


class PlanningIdeaUpdateRequest(BaseModel):
    planning_idea: str = Field(default="", max_length=20_000)


class ManualUpdateRequest(BaseModel):
    original_filename: str = Field(min_length=1, max_length=512)
    brand: str | None = Field(default=None, max_length=120)
    release: str | None = Field(default=None, max_length=120)
    cli_profile: Literal[
        "auto", "huawei_vrp", "h3c_comware", "cisco_ios", "arista_eos", "generic_manual"
    ] = "auto"


class ExportSaveRequest(BaseModel):
    destination_path: str = Field(min_length=1, max_length=4096)


class ExportSaveResponse(BaseModel):
    saved_path: str


class TemplateCreateFromTaskRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=2_000)


class TemplateUpdateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=2_000)


class TemplateSummary(BaseModel):
    id: str
    title: str
    description: str
    source_task_id: str | None
    manual_name: str | None
    device_plan_count: int
    created_at: datetime
    updated_at: datetime


class TemplateDetail(TemplateSummary):
    topology: TopologyDraft
    requirement_text: str
    planning_idea: str
    device_plans: list[dict[str, Any]] = Field(default_factory=list)


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
    connection_hint: dict[str, Any] = Field(default_factory=dict)
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
    planning_idea: str
    planning_idea_revision: int
    planning_idea_confirmed_at: datetime | None
    blocking_reason: str | None
    cancel_requested: bool = False
    cancel_reason: str | None = None
    device_plans: list[DevicePlanResponse]
    created_at: datetime
    updated_at: datetime


class PlanningEventResponse(BaseModel):
    id: str
    task_id: str
    sequence: int
    stage: str
    event_type: str
    content: str
    created_at: datetime


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
    execution_id: str | None = Field(default=None, min_length=16, max_length=64)
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
    operation: str
    target_host: str
    target_port: int
    execution_revision: int
    preflight: dict[str, Any]
    validation: dict[str, Any]
    save: dict[str, Any]
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
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
    """Evidence-planning intent emitted before any manual search.

    ``feature`` is deliberately open ended.  Vendor capabilities evolve faster
    than the application, so it labels the requested capability (for example
    ``l3_ospf_ipv4``) instead of acting as a hard allow-list.  Structured facts
    that can affect a built-in renderer, such as VLAN IDs, remain separately
    validated by the application.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["refine_intent"]
    feature: str = Field(default="generic", min_length=1, max_length=80, pattern=r"^[a-z0-9_:-]+$")
    capabilities: list[Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_:-]+$")]] = Field(
        default_factory=list,
        max_length=12,
    )
    vlan_ids: list[Annotated[int, Field(ge=1, le=4094)]] = Field(default_factory=list, max_length=10)
    retrieval_terms: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        default_factory=list,
        max_length=10,
    )
    planning_steps: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        default_factory=list,
        max_length=12,
    )
    # Human-facing proposal.  This is intentionally separate from the structured
    # capability labels so the operator can edit the model's actual explanation.
    planning_idea: str = Field(default="", max_length=12_000)
    requirement_gaps: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list,
        max_length=20,
    )
    reason_summary: str = Field(default="", max_length=300)


class LlmManualRetrievalDecision(BaseModel):
    """Constrained decision for an explicit manual-search graph node."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["manual_retrieval"]
    verdict: Literal["sufficient", "search_more", "not_found"]
    selected_command_ids: list[str] = Field(default_factory=list, max_length=5)
    next_queries: list[Annotated[str, Field(min_length=1, max_length=160)]] = Field(
        default_factory=list,
        # A compound configuration commonly needs a command entry point,
        # mode/enable command, member command and verification command.  Three
        # follow-up terms made the retrieval node silently drop an action such
        # as ``stp enable`` after it had already identified it in its reasoning.
        max_length=4,
    )
    reason_summary: str = Field(default="", max_length=300)


class CommandInvocation(BaseModel):
    """A handbook command reference plus parameters or one evidence-bound CLI.

    Built-in capability plugins use ``arguments`` and render deterministic CLI.
    The universal path uses ``cli`` for exactly one candidate command line.  It
    is never executed directly: the compiler verifies its handbook binding and
    topology scope before it can become a device-plan command.
    """

    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(min_length=1, max_length=64)
    syntax_index: int = Field(default=0, ge=0, le=20)
    arguments: dict[str, Any] = Field(default_factory=dict)
    target_port_ref: str | None = Field(default=None, max_length=255)
    cli: str | None = Field(default=None, min_length=1, max_length=1_000)


class CommandOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: str = Field(min_length=1, max_length=200)
    invocations: list[CommandInvocation] = Field(min_length=1, max_length=8)


class LlmCommandPlan(BaseModel):
    """Evidence-bound command plan consumed by a plugin or the universal compiler."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["command_plan"]
    operations: list[CommandOperation] = Field(min_length=1, max_length=20)
    verification_notes: list[str] = Field(default_factory=list, max_length=8)
    validation_commands: list[str] = Field(default_factory=list, max_length=12)
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
