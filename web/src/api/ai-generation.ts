import { api } from "./core";
import type {
  AiEngine,
  GenerationSession,
  QuestionItem,
  QuestionPool,
  QuestionSyncResult,
  QuestionType,
  Scheme,
  SchemeCreatePayload,
  SchemeRun,
  SchemeRunSummary,
  SchemeUpdatePayload,
} from "../types";

export {
  createPromptTemplate,
  deletePromptTemplate,
  listPromptTemplates,
  patchPromptTemplate,
  updatePromptTemplate,
} from "./prompt-templates";

export function startGeneration(payload: {
  skill_id: number;
  prompt_template_id: number;
  extra_instruction?: string;
  pool_id?: number;
  question_item_ids?: number[];
  auto_count?: number;
}): Promise<{ session_id: number; status: string }> {
  return api<{ session_id: number; status: string }>("/api/generation/sessions", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getGenerationSession(sessionId: number): Promise<GenerationSession> {
  return api<GenerationSession>(`/api/generation/sessions/${sessionId}`);
}

// ── 问题库 ───────────────────────────────────────────────────────────────────

export function listQuestionPools(): Promise<QuestionPool[]> {
  return api<QuestionPool[]>("/api/generation/question-pools");
}

export function createQuestionPool(payload: {
  name: string;
  feishu_app_token?: string;
  feishu_table_id?: string;
}): Promise<QuestionPool> {
  return api<QuestionPool>("/api/generation/question-pools", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function syncQuestionPool(poolId: number): Promise<QuestionSyncResult> {
  return api<QuestionSyncResult>(`/api/generation/question-pools/${poolId}/sync`, {
    method: "POST",
  });
}

export function updateQuestionPool(
  poolId: number,
  payload: {
    name?: string;
    feishu_app_token?: string;
    feishu_table_id?: string;
    auto_sync_enabled?: boolean;
  },
): Promise<QuestionPool> {
  return api<QuestionPool>(`/api/generation/question-pools/${poolId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

// 删除仅限 admin（后端 require_admin → 非 admin 返回 403）。
export function deleteQuestionPool(poolId: number): Promise<void> {
  return api<void>(`/api/generation/question-pools/${poolId}`, { method: "DELETE" });
}

export function listQuestionItems(poolId: number, status = "pending"): Promise<QuestionItem[]> {
  return api<QuestionItem[]>(`/api/generation/question-pools/${poolId}/items?status=${status}`);
}

export function listQuestionTypes(poolId: number): Promise<QuestionType[]> {
  return api<QuestionType[]>(`/api/generation/question-pools/${poolId}/question-types`);
}

// ── 方案池 / 方案运行（scheme flow）──────────────────────────────────────────

export function listAiEngines(): Promise<AiEngine[]> {
  return api<AiEngine[]>("/api/generation/ai-engines");
}

export function listSchemes(): Promise<Scheme[]> {
  return api<Scheme[]>("/api/generation/schemes");
}

export function getScheme(schemeId: number): Promise<Scheme> {
  return api<Scheme>(`/api/generation/schemes/${schemeId}`);
}

export function createScheme(payload: SchemeCreatePayload): Promise<Scheme> {
  return api<Scheme>("/api/generation/schemes", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateScheme(schemeId: number, payload: SchemeUpdatePayload): Promise<Scheme> {
  return api<Scheme>(`/api/generation/schemes/${schemeId}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function patchScheme(
  schemeId: number,
  payload: { is_enabled?: boolean },
): Promise<Scheme> {
  return api<Scheme>(`/api/generation/schemes/${schemeId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function deleteScheme(schemeId: number): Promise<void> {
  return api<void>(`/api/generation/schemes/${schemeId}`, { method: "DELETE" });
}

export function startSchemeRun(schemeId: number): Promise<{ run_id: number; status: string }> {
  return api<{ run_id: number; status: string }>(
    `/api/generation/schemes/${schemeId}/runs`,
    { method: "POST" },
  );
}

export function listSchemeRuns(schemeId: number): Promise<SchemeRunSummary[]> {
  return api<SchemeRunSummary[]>(`/api/generation/schemes/${schemeId}/runs`);
}

export function getSchemeRun(runId: number): Promise<SchemeRun> {
  return api<SchemeRun>(`/api/generation/scheme-runs/${runId}`);
}
