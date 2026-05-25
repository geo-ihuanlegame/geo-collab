import { api } from "./core";
import type { PromptScope, PromptTemplate } from "../types";

export function listPromptTemplates(scope?: PromptScope): Promise<PromptTemplate[]> {
  const params = new URLSearchParams();
  if (scope) params.set("scope", scope);
  const query = params.toString();
  return api<PromptTemplate[]>(query ? `/api/prompt-templates?${query}` : "/api/prompt-templates");
}

export function createPromptTemplate(payload: {
  name: string;
  content: string;
  scope?: PromptScope;
  is_system?: boolean;
}): Promise<PromptTemplate> {
  return api<PromptTemplate>("/api/prompt-templates", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updatePromptTemplate(
  id: number,
  payload: { name: string; content: string; scope?: PromptScope; is_system?: boolean },
): Promise<PromptTemplate> {
  return api<PromptTemplate>(`/api/prompt-templates/${id}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function patchPromptTemplate(
  id: number,
  payload: { is_enabled?: boolean; scope?: PromptScope; is_system?: boolean },
): Promise<PromptTemplate> {
  return api<PromptTemplate>(`/api/prompt-templates/${id}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function deletePromptTemplate(id: number): Promise<void> {
  return api<void>(`/api/prompt-templates/${id}`, { method: "DELETE" });
}

export function updateUserAiFormatPreset(aiFormatPresetId: number | null): Promise<{ ai_format_preset_id: number | null }> {
  return api<{ ai_format_preset_id: number | null }>("/api/users/me/settings", {
    method: "PATCH",
    body: JSON.stringify({ ai_format_preset_id: aiFormatPresetId }),
  });
}
