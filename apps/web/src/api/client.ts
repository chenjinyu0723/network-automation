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

export type Manual = {
  id: string;
  original_filename: string;
  file_format: string;
  brand: string | null;
  release: string | null;
  status: string;
  page_count: number;
  command_count: number;
  model_count: number;
  issue_count: number;
  created_at: string;
  updated_at: string;
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
  llm_api_key_configured: boolean;
  embedding_api_key_configured: boolean;
};

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
export type DevicePlan = { id: string; device_node_id: string; display_name: string; detected_model: string | null; detected_release: string | null; mapped_series: string | null; compatibility_status: string; compatibility_reason: string | null; intent: Record<string, unknown>; command_plan: Record<string, unknown>; evidence: CommandHit[]; commands: string[]; validation: { status?: string; errors?: string[]; source?: string }; approval_revision: number; approved_at: string | null };
export type ConfigTask = { id: string; topology_revision_id: string; manual_id: string; requirement_text: string; status: string; intent: Record<string, unknown>; blocking_reason: string | null; device_plans: DevicePlan[]; created_at: string; updated_at: string };
export type ReadOnlyProbe = { command: string; output: string; detected_model: string | null; detected_release: string | null; warnings: string[] };
export type ExecutionCommand = { sequence: number; phase: string; command: string; output: string; success: boolean };
export type ExecutionRun = { id: string; task_id: string; device_plan_id: string; status: string; target_host: string; target_port: number; execution_revision: number; preflight: { errors?: string[]; protected_ports?: string[] }; validation: Record<string, unknown>; save: Record<string, unknown>; error_message: string | null; started_at: string | null; finished_at: string | null; commands: ExecutionCommand[] };
export type PcPingRun = { id: string; command: string; output: string; success: boolean; error_message: string | null };

export async function listManuals(): Promise<Manual[]> {
  return (await api.get<Manual[]>("/manuals")).data;
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

export async function retryImportJob(id: string): Promise<ImportJob> {
  return (await api.post<ImportJob>(`/manual-imports/${id}/retry`)).data;
}

export async function createEmbeddingIndex(manualId: string): Promise<EmbeddingJob> {
  return (await api.post<EmbeddingJob>(`/manuals/${manualId}/embedding-index`)).data;
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

export async function health(): Promise<{ status: string }> {
  return (await api.get<{ status: string }>("/health")).data;
}

export async function saveTopology(payload: { name: string; nodes: TopologyNode[]; links: TopologyLink[] }): Promise<SavedTopology> {
  return (await api.post<SavedTopology>("/topologies", payload)).data;
}

export async function createConfigTask(payload: { topology_revision_id: string; manual_id: string; requirement_text: string }): Promise<ConfigTask> {
  return (await api.post<ConfigTask>("/config-tasks", payload)).data;
}

export async function approveDevicePlan(taskId: string, planId: string, payload: { approval_revision: number; command_overrides?: string[] }): Promise<DevicePlan> {
  return (await api.post<DevicePlan>(`/config-tasks/${taskId}/devices/${planId}/approve`, payload)).data;
}

export async function executeHuaweiPlan(taskId: string, planId: string, payload: { host: string; port: number; username: string; password: string }): Promise<ExecutionRun> {
  return (await api.post<ExecutionRun>(`/config-tasks/${taskId}/devices/${planId}/execute-huawei`, payload)).data;
}

export async function executePcPing(executionId: string, payload: { host: string; port: number; username: string; password: string; os_family: "linux" | "windows"; target_ip: string }): Promise<PcPingRun> {
  return (await api.post<PcPingRun>(`/executions/${executionId}/pc-ping`, payload)).data;
}

export async function huaweiReadOnlyProbe(payload: { host: string; port: number; username: string; password: string; command: string }): Promise<ReadOnlyProbe> {
  return (await api.post<ReadOnlyProbe>("/devices/huawei/read-only-probe", payload)).data;
}
