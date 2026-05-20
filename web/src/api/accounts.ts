import { api } from "./core";
import type { Account, AccountBrowserSession, AccountBrowserSessionFinish, PlatformLoginPayload, PlatformOption } from "../types";

export async function pollLoginSessionUntilActive(
  accountId: number,
  sessionId: string,
  timeoutMs = 90_000,
): Promise<{ novnc_url: string; session_id: string }> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const status = await api<{
      status: string;
      novnc_url: string | null;
      error_message: string | null;
      browser_session_id: string | null;
    }>(`/api/accounts/${accountId}/login-session/${sessionId}/status`);

    if (status.status === "active") {
      return {
        session_id: status.browser_session_id ?? sessionId,
        novnc_url: status.novnc_url ?? "",
      };
    }
    if (status.status === "failed" || status.status === "cancelled") {
      throw new Error(status.error_message || "Login session failed to start");
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error("Login session did not become active within 90s");
}

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
    if (response.status === 401) {
      window.dispatchEvent(new CustomEvent("auth:unauthorized"));
      throw new Error("登录已过期，请重新登录");
    }
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `${response.status} ${response.statusText}`);
  }
  return response;
}
