import { api } from "./core";
import type { NodeTypeDef, Pipeline, PipelineRun, PipelineVersionSummary, RunLogPage } from "../types";

export interface AgentFields {
  type?: string; tags?: string[]; ignore_exception?: boolean; is_enabled?: boolean;
  schedule_kind?: string; schedule_minute?: number | null; schedule_hour?: number | null;
  schedule_weekday?: number | null; window_start?: string | null; window_end?: string | null;
}

export const listPipelines = () => api<Pipeline[]>("/api/pipelines");
export const getPipeline = (id: number) => api<Pipeline>(`/api/pipelines/${id}`);
export const createPipeline = (p: { name: string; description?: string } & AgentFields) =>
  api<Pipeline>("/api/pipelines", { method: "POST", body: JSON.stringify(p) });
export const patchPipeline = (id: number, p: { name?: string; description?: string } & AgentFields) =>
  api<Pipeline>(`/api/pipelines/${id}`, { method: "PATCH", body: JSON.stringify(p) });
export const deletePipeline = (id: number) =>
  api<void>(`/api/pipelines/${id}`, { method: "DELETE" });

export const getNodeTypes = () =>
  api<{ node_types: NodeTypeDef[]; registered: string[] }>("/api/pipelines/node-types");

export const saveDraft = (id: number, snapshot: unknown) =>
  api<{ ok: boolean }>(`/api/pipelines/${id}/draft`, { method: "POST", body: JSON.stringify({ snapshot }) });
export const publishPipeline = (id: number, remark?: string) =>
  api<{ version_no: number }>(`/api/pipelines/${id}/publish`, { method: "POST", body: JSON.stringify({ remark }) });
export const discardDraft = (id: number) =>
  api<{ ok: boolean }>(`/api/pipelines/${id}/draft/discard`, { method: "POST" });

export const listVersions = (id: number) =>
  api<PipelineVersionSummary[]>(`/api/pipelines/${id}/versions`);
export const rollbackVersion = (versionId: number) =>
  api<{ ok: boolean }>(`/api/pipelines/versions/${versionId}/rollback`, { method: "POST" });

export const startRun = (id: number) =>
  api<{ run_id: number; status: string }>(`/api/pipelines/${id}/runs`, { method: "POST" });
export const getRun = (runId: number) => api<PipelineRun>(`/api/pipelines/runs/${runId}`);

export const listPipelineLogs = (
  id: number,
  opts: { page?: number; pageSize?: number; startDate?: string; endDate?: string } = {},
) => {
  const p = new URLSearchParams();
  p.set("page", String(opts.page ?? 1));
  p.set("page_size", String(opts.pageSize ?? 30));
  if (opts.startDate) p.set("start_date", opts.startDate);
  if (opts.endDate) p.set("end_date", opts.endDate);
  return api<RunLogPage>(`/api/pipelines/${id}/logs?${p.toString()}`);
};
