import { api } from "./core";
import type { AssignmentPreview, ManualConfirmPayload, PublishRecord, Task, TaskCreatePayload, TaskLog } from "../types";

export type TaskPreviewPayload = Omit<TaskCreatePayload, "client_request_id"> & {
  client_request_id?: string;
};

export function listTasks(): Promise<Task[]> {
  return api<Task[]>("/api/tasks");
}

export function listTaskRecords(taskId: number): Promise<PublishRecord[]> {
  return api<PublishRecord[]>(`/api/tasks/${taskId}/records`);
}

export function listTaskLogs(taskId: number, afterId: number): Promise<TaskLog[]> {
  return api<TaskLog[]>(`/api/tasks/${taskId}/logs?after_id=${afterId}`);
}

export function createTask(payload: TaskCreatePayload): Promise<Task> {
  return api<Task>("/api/tasks", { method: "POST", body: JSON.stringify(payload) });
}

export function previewTaskAssignment(payload: TaskPreviewPayload): Promise<AssignmentPreview> {
  return api<AssignmentPreview>("/api/tasks/preview", { method: "POST", body: JSON.stringify(payload) });
}

export function executeTask(taskId: number): Promise<{ queued: boolean }> {
  return api<{ queued: boolean }>(`/api/tasks/${taskId}/execute`, { method: "POST" });
}

export function cancelTask(taskId: number): Promise<Task> {
  return api<Task>(`/api/tasks/${taskId}/cancel`, { method: "POST" });
}

export function resolveRecordUserInput(recordId: number): Promise<PublishRecord> {
  return api<PublishRecord>(`/api/publish-records/${recordId}/resolve-user-input`, { method: "POST" });
}

export function manualConfirmRecord(recordId: number, payload: ManualConfirmPayload): Promise<PublishRecord> {
  return api<PublishRecord>(`/api/publish-records/${recordId}/manual-confirm`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function retryRecord(recordId: number): Promise<PublishRecord> {
  return api<PublishRecord>(`/api/publish-records/${recordId}/retry`, { method: "POST" });
}
