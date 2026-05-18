import { api } from "./core";
import type { Account, AccountBrowserSession, AccountBrowserSessionFinish, PlatformLoginPayload, PlatformOption } from "../types";

export type AccountLoginPayload = PlatformLoginPayload & {
  channel?: string;
  wait_seconds?: number;
};

export type ExistingAccountLoginPayload = {
  channel?: string;
  wait_seconds?: number;
  use_browser?: boolean;
};

export type AccountImportResult = {
  imported: string[];
  skipped: string[];
};

export function listAccounts(): Promise<Account[]> {
  return api<Account[]>("/api/accounts");
}

export function listPlatforms(): Promise<PlatformOption[]> {
  return api<PlatformOption[]>("/api/accounts/platforms");
}

export function loginPlatformAccount(platformCode: string, payload: AccountLoginPayload): Promise<Account> {
  return api<Account>(`/api/accounts/${platformCode}/login`, { method: "POST", body: JSON.stringify(payload) });
}

export function startPlatformLoginSession(platformCode: string, payload: AccountLoginPayload): Promise<AccountBrowserSession> {
  return api<AccountBrowserSession>(`/api/accounts/${platformCode}/login-session`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function startAccountLoginSession(accountId: number, payload: ExistingAccountLoginPayload): Promise<AccountBrowserSession> {
  return api<AccountBrowserSession>(`/api/accounts/${accountId}/login-session`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function finishAccountLoginSession(accountId: number, sessionId: string): Promise<AccountBrowserSessionFinish> {
  return api<AccountBrowserSessionFinish>(`/api/accounts/${accountId}/login-session/${sessionId}/finish`, { method: "POST" });
}

export function stopAccountLoginSession(accountId: number, sessionId: string): Promise<void> {
  return api<void>(`/api/accounts/${accountId}/login-session/${sessionId}`, { method: "DELETE" });
}

export function deleteAccount(accountId: number): Promise<void> {
  return api<void>(`/api/accounts/${accountId}`, { method: "DELETE" });
}

export function importAccountPackage(formData: FormData): Promise<AccountImportResult> {
  return api<AccountImportResult>("/api/accounts/import", { method: "POST", body: formData });
}

export function updateAccountDisplayName(accountId: number, displayName: string): Promise<Account> {
  return api<Account>(`/api/accounts/${accountId}`, {
    method: "PATCH",
    body: JSON.stringify({ display_name: displayName }),
  });
}

export async function exportAccountPackage(accountIds: number[]): Promise<Response> {
  const response = await fetch("/api/accounts/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ account_ids: accountIds }),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `${response.status} ${response.statusText}`);
  }
  return response;
}
