export const emptyDoc = { type: "doc", content: [{ type: "paragraph" }] };

const inFlightKeys = new Set<string>();

export async function singleFlight<T>(key: string, fn: () => Promise<T>): Promise<T | undefined> {
  if (inFlightKeys.has(key)) return undefined;
  inFlightKeys.add(key);
  try {
    return await fn();
  } finally {
    inFlightKeys.delete(key);
  }
}

export function newClientRequestId(prefix: string): string {
  const cryptoObj = globalThis.crypto;
  const random = typeof cryptoObj?.randomUUID === "function"
    ? cryptoObj.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${random}`;
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const isFormData = init?.body instanceof FormData;

  const headers: Record<string, string> = {};
  if (!isFormData) headers["Content-Type"] = "application/json";

  const response = await fetch(path, {
    ...init,
    headers: { ...headers, ...(init?.headers as Record<string, string>) },
  });
  if (response.status === 401 && !path.startsWith("/api/auth")) {
    window.dispatchEvent(new CustomEvent("auth:unauthorized"));
    throw new Error("登录已过期，请重新登录");
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    if (response.status === 403 && payload.detail === "Password change required") {
      window.dispatchEvent(new CustomEvent("auth:password-change-required"));
    }
    const statusText: Record<number, string> = {
      400: "请求参数错误",
      401: "未登录或登录已过期",
      403: "无权限执行此操作",
      404: "请求的资源不存在",
      409: "操作冲突，请刷新后重试",
      422: "提交的数据格式有误",
      500: "服务器内部错误",
      502: "服务器网关错误",
      503: "服务暂时不可用",
    };
    const detailMessage = typeof payload.detail === "string" ? payload.detail : "";
    throw new Error(detailMessage || statusText[response.status] || `服务器错误（${response.status}）`);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export function assetSrc(assetId: string | null): string | null {
  if (!assetId) return null;
  return `/api/assets/${assetId}`;
}

export function assetThumbSrc(assetId: string | null): string | null {
  if (!assetId) return null;
  return `/api/assets/${assetId}/thumbnail`;
}

export function withAssetToken(url: string): string {
  return url;
}

export function countWords(text: string): number {
  return text.split(/\s+/).filter(Boolean).length;
}
