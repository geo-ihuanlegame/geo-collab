import { api } from "./core";
import type { Skill, PromptTemplate, GenerationSession } from "../types";

// ── Skills ────────────────────────────────────────────────────────────────

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

// ── Prompt Templates ──────────────────────────────────────────────────────

export function listPromptTemplates(): Promise<PromptTemplate[]> {
  return api<PromptTemplate[]>("/api/prompt-templates");
}

export function createPromptTemplate(payload: { name: string; content: string }): Promise<PromptTemplate> {
  return api<PromptTemplate>("/api/prompt-templates", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updatePromptTemplate(
  id: number,
  payload: { name: string; content: string },
): Promise<PromptTemplate> {
  return api<PromptTemplate>(`/api/prompt-templates/${id}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function patchPromptTemplate(
  id: number,
  payload: { is_enabled: boolean },
): Promise<PromptTemplate> {
  return api<PromptTemplate>(`/api/prompt-templates/${id}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function deletePromptTemplate(id: number): Promise<void> {
  return api<void>(`/api/prompt-templates/${id}`, { method: "DELETE" });
}

// ── Generation ────────────────────────────────────────────────────────────

export function startGeneration(payload: {
  skill_id: number;
  prompt_template_id: number;
  extra_instruction?: string;
}): Promise<{ session_id: number; status: string }> {
  return api<{ session_id: number; status: string }>("/api/generation/sessions", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getGenerationSession(sessionId: number): Promise<GenerationSession> {
  return api<GenerationSession>(`/api/generation/sessions/${sessionId}`);
}
