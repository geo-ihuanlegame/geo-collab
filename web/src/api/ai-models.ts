import { api } from "./core";
import type { AiModel, AiModelPayload } from "../types";

export function listAiModels(scope?: string): Promise<AiModel[]> {
  const qs = scope ? `?scope=${encodeURIComponent(scope)}` : "";
  return api<AiModel[]>(`/api/ai-models${qs}`);
}

export function createAiModel(payload: AiModelPayload): Promise<AiModel> {
  return api<AiModel>("/api/ai-models", { method: "POST", body: JSON.stringify(payload) });
}

export function updateAiModel(id: number, payload: Partial<AiModelPayload>): Promise<AiModel> {
  return api<AiModel>(`/api/ai-models/${id}`, { method: "PATCH", body: JSON.stringify(payload) });
}

export function deleteAiModel(id: number): Promise<void> {
  return api<void>(`/api/ai-models/${id}`, { method: "DELETE" });
}
