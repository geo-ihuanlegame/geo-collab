import { api } from "./core";
import type { GenerationSession, QuestionItem, QuestionPool, QuestionSyncResult, Skill } from "../types";

export {
  createPromptTemplate,
  deletePromptTemplate,
  listPromptTemplates,
  patchPromptTemplate,
  updatePromptTemplate,
} from "./prompt-templates";

export function listSkills(): Promise<Skill[]> {
  return api<Skill[]>("/api/skills");
}

export function uploadSkill(file: File): Promise<Skill> {
  const form = new FormData();
  form.append("file", file);
  return api<Skill>("/api/skills", { method: "POST", body: form });
}

export function patchSkill(id: number, payload: { is_enabled: boolean }): Promise<Skill> {
  return api<Skill>(`/api/skills/${id}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function deleteSkill(id: number): Promise<void> {
  return api<void>(`/api/skills/${id}`, { method: "DELETE" });
}

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

export function listQuestionItems(poolId: number, status = "pending"): Promise<QuestionItem[]> {
  return api<QuestionItem[]>(`/api/generation/question-pools/${poolId}/items?status=${status}`);
}
