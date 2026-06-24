import { api } from "./core";
import type {
  Account,
  AccountBrowserSession,
  AccountBrowserSessionFinish,
  AccountLoginSessionStatusResponse,
  AccountMember,
  BackfillIdentitySummary,
  PlatformLoginPayload,
  PlatformOption,
} from "../types";

// Sentinel to distinguish terminal session errors from transient network errors.
class LoginSessionTerminalError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "LoginSessionTerminalError";
  }
}

export async function pollLoginSessionUntilActive(
  accountId: number,
  sessionId: string,
  timeoutMs = 90_000,
  onStatus?: (status: AccountLoginSessionStatusResponse) => void,
): Promise<{ novnc_url: string; session_id: string; queue_reason: string | null; error_message: string | null }> {
  const deadline = Date.now() + timeoutMs;
  let lastStatus: AccountLoginSessionStatusResponse | null = null;
  while (Date.now() < deadline) {
    try {
      const status = await api<AccountLoginSessionStatusResponse>(`/api/accounts/${accountId}/login-session/${sessionId}/status`);
      lastStatus = status;
      onStatus?.(status);

      if (status.status === "active") {
        return {
          session_id: status.browser_session_id ?? sessionId,
          novnc_url: status.novnc_url ?? "",
          queue_reason: status.queue_reason ?? null,
          error_message: status.error_message ?? null,
        };
      }
      if (status.status === "failed" || status.status === "cancelled") {
        // Terminal status from the session — do not retry.
        throw new LoginSessionTerminalError(status.error_message || status.queue_reason || "Login session failed to start");
      }
      // pending / queued / starting — keep polling
    } catch (err) {
      if (err instanceof LoginSessionTerminalError) {
        // Re-throw terminal session errors; these are not transient.
        throw err;
      }
      // api() throws a plain Error for HTTP errors. Treat 404 (session not
      // found) as terminal; treat all other errors (network failures, 5xx,
      // etc.) as transient and keep polling.
      if (err instanceof Error && err.message.startsWith("404 ")) {
        throw err;
      }
      // Transient error — continue loop and retry after delay.
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  const reason = lastStatus?.error_message || lastStatus?.queue_reason;
  throw new Error(reason ? `Login session did not become active in time: ${reason}` : "Login session did not become active in time");
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

export function listAccounts(q?: string): Promise<Account[]> {
  const keyword = q?.trim();
  const path = keyword ? `/api/accounts?q=${encodeURIComponent(keyword)}` : "/api/accounts";
  return api<Account[]>(path);
}

export function listPlatforms(): Promise<PlatformOption[]> {
  return api<PlatformOption[]>("/api/accounts/platforms");
}

// TODO: This function is defined but currently unused in the frontend.
// Wire it up when implementing the platform login flow.
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

export function updateAccount(accountId: number, payload: Record<string, unknown>): Promise<Account> {
  return api<Account>(`/api/accounts/${accountId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function createApiAccount(payload: Record<string, unknown>): Promise<Account> {
  return api<Account>("/api/accounts", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function verifyCredentials(accountId: number): Promise<Account> {
  return api<Account>(`/api/accounts/${accountId}/verify-credentials`, { method: "POST" });
}

// TapTap 论坛绑定配置（app_id/group_id/x_ua）。x_ua 留空则后端由 VID 合成。
export function setTaptapForum(
  accountId: number,
  payload: { app_id: string; group_id: string; x_ua?: string },
): Promise<Account> {
  return api<Account>(`/api/accounts/${accountId}/taptap-forum`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

// 浏览器接入账号（头条等）的登录态探测：开浏览器载入 storage_state，由 driver.detect_logged_in
// 判定，刷新 status 为 valid/expired（不抛错，失效时返回 status==expired）。需浏览器栈（容器内）。
export function checkAccount(accountId: number, useBrowser = true): Promise<Account> {
  return api<Account>(`/api/accounts/${accountId}/check`, {
    method: "POST",
    body: JSON.stringify({ use_browser: useBrowser }),
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

export function listAccountMembers(accountId: number): Promise<AccountMember[]> {
  return api<AccountMember[]>(`/api/accounts/${accountId}/members`);
}

export function removeAccountMember(accountId: number, userId: number): Promise<void> {
  return api<void>(`/api/accounts/${accountId}/members/${userId}`, { method: "DELETE" });
}

export function backfillIdentity(): Promise<BackfillIdentitySummary> {
  return api<BackfillIdentitySummary>("/api/accounts/backfill-identity", { method: "POST" });
}
