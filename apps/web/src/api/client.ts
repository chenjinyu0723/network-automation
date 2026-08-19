import axios from "axios";

export const api = axios.create({ baseURL: "/api", timeout: 60_000 });

export type ImportJob = {
  id: string;
  manual_id: string;
  status: string;
  stage: string;
  progress_current: number;
  progress_total: number;
  detail: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};
export type EmbeddingJob = { id: string; manual_id: string; model: string; status: string; progress_current: number; progress_total: number; detail: string | null; created_at: string; started_at: string | null; finished_at: string | null };
export type ActiveManualSearchCandidate = {
  kind: "command" | "document";
  command_id: string | null;
  document_id: string;
  canonical_name: string | null;
  syntax: string[];
  source_path: string;
  title: string;
  excerpt: string;
  score: number;
  retrieval_sources: string[];
};
export type ActiveManualSearch = {
  status: "found" | "incomplete" | "not_found";
  selected_command_ids: string[];
  candidates: ActiveManualSearchCandidate[];
  rounds: Array<{ round: number; queries: string[]; candidate_count: number; llm: { status: string }; decision?: { verdict: string; reason_summary: string } }>;
};

export type Manual = {
  id: string;
  original_filename: string;
  file_format: string;
  brand: string | null;
  release: string | null;
  cli_profile: "auto" | "huawei_vrp" | "h3c_comware" | "cisco_ios" | "arista_eos" | "generic_manual";
  status: string;
  page_count: number;
  command_count: number;
  model_count: number;
  issue_count: number;
  created_at: string;
  updated_at: string;
};
export type ConfigurationTemplateSummary = {
  id: string;
  title: string;
  description: string;
  source_task_id: string | null;
  manual_name: string | null;
  device_plan_count: number;
  created_at: string;
  updated_at: string;
};
export type ConfigurationTemplateDetail = ConfigurationTemplateSummary & {
  topology: { name: string; nodes: TopologyNode[]; links: TopologyLink[] };
  requirement_text: string;
  planning_idea: string;
  device_plans: Array<{
    display_name: string;
    device_node_id: string;
    intent: Record<string, unknown>;
    commands: string[];
    validation: Record<string, unknown>;
  }>;
};

export type DeviceModel = {
  id: string;
  brand: string;
  canonical_name: string;
  level: "series" | "family" | "sku";
  parent_id: string | null;
  review_status: "candidate" | "published" | "rejected";
  confidence: number;
  source_manual_id: string | null;
  aliases: string[];
  evidence_count: number;
};

export type CommandHit = {
  id: string;
  canonical_name: string;
  manual_id: string;
  document_id: string;
  feature: string | null;
  syntax: string[];
  views: string[];
  preconditions: string[];
  constraints: string[];
  applicability_mode: string;
  source_path: string;
  score: number | null;
  retrieval_sources: string[];
};

export type ProviderSettings = {
  llm_base_url: string | null;
  llm_model: string | null;
  llm_temperature: number;
  llm_thinking_mode: "adaptive" | "always" | "off";
  embedding_base_url: string | null;
  embedding_model: string | null;
  embedding_dimensions: number | null;
  embedding_batch_size: number;
  llm_api_key_configured: boolean;
  embedding_api_key_configured: boolean;
};
export type LlmConnectionTest = { status: "ok"; model: string; thinking_requested: boolean; thinking_used: boolean; thinking_fallback: boolean; detail: string | null };
export type LocalExport = { blob: Blob; saved_path: string | null };
export type SavedExport = { saved_path: string };
export type ExportKind = "manual" | "topology" | "template";

export type TopologyNode = {
  id: string;
  kind: string;
  name: string;
  x: number;
  y: number;
  model_id?: string | null;
  ip?: string | null;
  prefix?: number | null;
  gateway?: string | null;
  ssh_host?: string | null;
  ssh_port?: number | null;
  ssh_username?: string | null;
  detected_model?: string | null;
  detected_release?: string | null;
  protected_ports?: string[];
};

export type TopologyLink = { id: string; source: string; source_port: string; target: string; target_port: string };
export type SavedTopology = { id: string; name: string; revision_id: string; revision: number; graph: { name: string; nodes: TopologyNode[]; links: TopologyLink[] } };
export type TopologySummary = { id: string; name: string; revision_id: string; revision: number; updated_at: string };
export type DevicePlan = { id: string; device_node_id: string; display_name: string; detected_model: string | null; detected_release: string | null; mapped_series: string | null; compatibility_status: string; compatibility_reason: string | null; intent: Record<string, unknown>; command_plan: Record<string, unknown>; connection_hint: { host?: string | null; port?: number; username?: string | null }; evidence: CommandHit[]; commands: string[]; validation: { status?: string; errors?: string[]; warnings?: string[]; source?: string; [key: string]: unknown }; rollback: { level?: string; commands?: string[]; reason?: string; [key: string]: unknown }; approval_revision: number; approved_at: string | null };
export type ConfigTask = { id: string; topology_revision_id: string; manual_id: string; requirement_text: string; status: string; intent: Record<string, unknown>; planning_idea: string; planning_idea_revision: number; planning_idea_confirmed_at: string | null; blocking_reason: string | null; cancel_requested: boolean; cancel_reason: string | null; device_plans: DevicePlan[]; created_at: string; updated_at: string };
export type PlanningEvent = { id: string; task_id: string; sequence: number; stage: string; event_type: "stage" | "thinking" | "output" | "done" | "cancelled" | "error"; content: string; created_at: string };
export type ReadOnlyProbe = { command: string; output: string; detected_model: string | null; detected_release: string | null; warnings: string[] };
export type ExecutionCommand = { sequence: number; phase: string; command: string; output: string; success: boolean };
export type ExecutionRun = { id: string; task_id: string; device_plan_id: string; status: string; operation: "apply" | "undo"; target_host: string; target_port: number; execution_revision: number; preflight: { errors?: string[]; protected_ports?: string[]; allowed_undo_ports?: string[] }; validation: Record<string, unknown>; save: Record<string, unknown>; error_message: string | null; started_at: string | null; finished_at: string | null; created_at: string; commands: ExecutionCommand[] };
export type PcPingRun = { id: string; command: string; output: string; success: boolean; error_message: string | null };

export async function listManuals(): Promise<Manual[]> {
  return (await api.get<Manual[]>("/manuals")).data;
}

export async function updateManual(id: string, payload: { original_filename: string; brand?: string | null; release?: string | null; cli_profile?: Manual["cli_profile"] }): Promise<Manual> {
  return (await api.patch<Manual>(`/manuals/${id}`, payload)).data;
}

export async function uploadManual(file: File, brand?: string, release?: string): Promise<ImportJob> {
  const form = new FormData();
  form.append("file", file);
  if (brand) form.append("brand", brand);
  if (release) form.append("release", release);
  return (await api.post<ImportJob>("/manuals/upload", form)).data;
}

export async function getImportJob(id: string): Promise<ImportJob> {
  return (await api.get<ImportJob>(`/manual-imports/${id}`)).data;
}

export async function listImportJobs(): Promise<ImportJob[]> {
  return (await api.get<ImportJob[]>("/manual-imports")).data;
}

export async function retryImportJob(id: string): Promise<ImportJob> {
  return (await api.post<ImportJob>(`/manual-imports/${id}/retry`)).data;
}

export async function createEmbeddingIndex(manualId: string): Promise<EmbeddingJob> {
  return (await api.post<EmbeddingJob>(`/manuals/${manualId}/embedding-index`)).data;
}

export async function activeManualSearch(manualId: string, requirementText: string): Promise<ActiveManualSearch> {
  return (await api.post<ActiveManualSearch>(`/manuals/${manualId}/active-search`, { requirement_text: requirementText })).data;
}

export async function listModels(publishedOnly = false): Promise<DeviceModel[]> {
  return (await api.get<DeviceModel[]>("/models", { params: { published_only: publishedOnly } })).data;
}

export async function updateModel(id: string, update: { parent_id?: string | null; review_status?: string; canonical_name?: string; aliases_to_add?: string[] }): Promise<DeviceModel> {
  return (await api.patch<DeviceModel>(`/models/${id}`, update)).data;
}

export async function searchCommands(query: string, modelId?: string): Promise<CommandHit[]> {
  const response = await api.get<{ hits: CommandHit[] }>("/commands/search", {
    params: { q: query, model_id: modelId }
  });
  return response.data.hits;
}

export async function getProviderSettings(): Promise<ProviderSettings> {
  return (await api.get<ProviderSettings>("/settings/providers")).data;
}

export async function saveProviderSettings(payload: Record<string, unknown>): Promise<ProviderSettings> {
  return (await api.put<ProviderSettings>("/settings/providers", payload)).data;
}

export async function testLlmProvider(): Promise<LlmConnectionTest> {
  return (await api.post<LlmConnectionTest>("/settings/providers/test-llm")).data;
}

export async function health(): Promise<{ status: string }> {
  return (await api.get<{ status: string }>("/health")).data;
}

export async function saveTopology(payload: { name: string; nodes: TopologyNode[]; links: TopologyLink[] }): Promise<SavedTopology> {
  return (await api.post<SavedTopology>("/topologies", payload)).data;
}

export async function updateTopology(id: string, payload: { name: string; nodes: TopologyNode[]; links: TopologyLink[] }): Promise<SavedTopology> {
  return (await api.put<SavedTopology>(`/topologies/${id}`, payload)).data;
}

export async function listTopologies(): Promise<TopologySummary[]> {
  return (await api.get<TopologySummary[]>("/topologies")).data;
}

export async function getTopology(id: string): Promise<SavedTopology> {
  return (await api.get<SavedTopology>(`/topologies/${id}`)).data;
}

export async function deleteTopology(id: string): Promise<void> {
  await api.delete(`/topologies/${id}`);
}

function localExport(response: { data: Blob; headers: Record<string, string | undefined> }): LocalExport {
  const rawPath = response.headers["x-network-automation-export-path"];
  return { blob: response.data, saved_path: rawPath ? decodeURIComponent(rawPath) : null };
}

export async function exportTopology(id: string): Promise<LocalExport> {
  return localExport(await api.get(`/topologies/${id}/export`, { responseType: "blob" }));
}

export async function saveTopologyExport(id: string, destinationPath: string): Promise<SavedExport> {
  return (await api.post<SavedExport>(`/topologies/${id}/export`, { destination_path: destinationPath })).data;
}

export async function importTopology(file: File, overwrite = false): Promise<SavedTopology> {
  const form = new FormData();
  form.append("file", file);
  return (await api.post<SavedTopology>(`/topologies/import?overwrite=${overwrite}`, form)).data;
}

export async function createConfigTask(payload: { task_id?: string; topology_revision_id: string; manual_id: string; requirement_text: string; template_id?: string }): Promise<ConfigTask> {
  // Planning may wait on a local OpenAI-compatible endpoint for several minutes.
  // Its progress is delivered through SSE, so this initiating request must not use
  // the short default timeout for ordinary UI calls.
  return (await api.post<ConfigTask>("/config-tasks", payload, { timeout: 0 })).data;
}

export async function listTemplates(): Promise<ConfigurationTemplateSummary[]> {
  return (await api.get<ConfigurationTemplateSummary[]>("/templates")).data;
}

export async function getTemplate(id: string): Promise<ConfigurationTemplateDetail> {
  return (await api.get<ConfigurationTemplateDetail>(`/templates/${id}`)).data;
}

export async function saveTaskAsTemplate(taskId: string, payload: { title: string; description: string }): Promise<ConfigurationTemplateDetail> {
  return (await api.post<ConfigurationTemplateDetail>(`/config-tasks/${taskId}/templates`, payload)).data;
}

export async function updateTemplate(id: string, payload: { title: string; description: string }): Promise<ConfigurationTemplateSummary> {
  return (await api.put<ConfigurationTemplateSummary>(`/templates/${id}`, payload)).data;
}

export async function deleteTemplate(id: string): Promise<void> {
  await api.delete(`/templates/${id}`);
}

export async function exportTemplate(id: string): Promise<LocalExport> {
  return localExport(await api.get(`/templates/${id}/export`, { responseType: "blob" }));
}

export async function saveTemplateExport(id: string, destinationPath: string): Promise<SavedExport> {
  return (await api.post<SavedExport>(`/templates/${id}/export`, { destination_path: destinationPath })).data;
}

export async function importTemplate(file: File, overwrite = false): Promise<ConfigurationTemplateSummary> {
  const form = new FormData();
  form.append("file", file);
  return (await api.post<ConfigurationTemplateSummary>(`/templates/import?overwrite=${overwrite}`, form)).data;
}

export async function deleteManual(id: string): Promise<void> {
  await api.delete(`/manuals/${id}`);
}

export async function exportManual(id: string): Promise<LocalExport> {
  return localExport(await api.get(`/manuals/${id}/export`, { responseType: "blob" }));
}

export async function saveManualExport(id: string, destinationPath: string): Promise<SavedExport> {
  return (await api.post<SavedExport>(`/manuals/${id}/export`, { destination_path: destinationPath })).data;
}

type DesktopBridge = {
  choose_export_path: (suggestedFilename: string, archiveKind: ExportKind) => Promise<string | null>;
};

export async function chooseDesktopExportPath(
  suggestedFilename: string,
  archiveKind: ExportKind,
): Promise<string | null | undefined> {
  const bridge = (window as Window & { pywebview?: { api?: DesktopBridge } }).pywebview?.api;
  if (!bridge) return undefined;
  return bridge.choose_export_path(suggestedFilename, archiveKind);
}

export async function importManual(file: File, overwrite = false): Promise<Manual> {
  const form = new FormData();
  form.append("file", file);
  return (await api.post<Manual>(`/manuals/import?overwrite=${overwrite}`, form)).data;
}

export async function updatePlanningIdea(taskId: string, planningIdea: string): Promise<ConfigTask> {
  return (await api.put<ConfigTask>(`/config-tasks/${taskId}/planning-idea`, { planning_idea: planningIdea })).data;
}

export async function generateConfigCommands(taskId: string, planningIdea: string): Promise<ConfigTask> {
  return (await api.post<ConfigTask>(`/config-tasks/${taskId}/generate-commands`, { planning_idea: planningIdea }, { timeout: 0 })).data;
}

export async function getConfigTask(taskId: string): Promise<ConfigTask> {
  return (await api.get<ConfigTask>(`/config-tasks/${taskId}`)).data;
}

export async function cancelConfigTask(taskId: string): Promise<ConfigTask> {
  return (await api.post<ConfigTask>(`/config-tasks/${taskId}/cancel`)).data;
}

export function planningEventStreamUrl(taskId: string, after = 0): string {
  return `/api/config-tasks/${taskId}/events?after=${after}`;
}

export async function listConfigTasks(): Promise<ConfigTask[]> {
  return (await api.get<ConfigTask[]>("/config-tasks")).data;
}

export async function approveDevicePlan(taskId: string, planId: string, payload: { approval_revision: number; command_overrides?: string[] }): Promise<DevicePlan> {
  return (await api.post<DevicePlan>(`/config-tasks/${taskId}/devices/${planId}/approve`, payload)).data;
}

export async function executeHuaweiPlan(taskId: string, planId: string, payload: { execution_id?: string; host: string; port: number; username: string; password: string }): Promise<ExecutionRun> {
  return (await api.post<ExecutionRun>(`/config-tasks/${taskId}/devices/${planId}/execute-huawei`, payload)).data;
}

export async function undoHuaweiPlan(taskId: string, planId: string, payload: { execution_id?: string; host: string; port: number; username: string; password: string }): Promise<ExecutionRun> {
  return (await api.post<ExecutionRun>(`/config-tasks/${taskId}/devices/${planId}/undo-huawei`, payload)).data;
}

export async function listPlanExecutions(taskId: string, planId: string): Promise<ExecutionRun[]> {
  return (await api.get<ExecutionRun[]>(`/config-tasks/${taskId}/devices/${planId}/executions`)).data;
}

export function executionEventStreamUrl(executionId: string, after = 0): string {
  return `/api/executions/${executionId}/events?after=${after}`;
}

export async function executePcPing(executionId: string, payload: { host: string; port: number; username: string; password: string; os_family: "linux" | "windows"; target_ip: string }): Promise<PcPingRun> {
  return (await api.post<PcPingRun>(`/executions/${executionId}/pc-ping`, payload)).data;
}

export async function huaweiReadOnlyProbe(payload: { host: string; port: number; username: string; password: string; command: string }): Promise<ReadOnlyProbe> {
  return (await api.post<ReadOnlyProbe>("/devices/huawei/read-only-probe", payload)).data;
}
