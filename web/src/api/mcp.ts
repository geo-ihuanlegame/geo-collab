import { api } from "./core";

export type McpStatus = {
  configured: boolean;
  suggested_base_url: string;
  tools_count: number;
};

export type McpHealthResult =
  | { ok: true }
  | { ok: false; status: number; message: string };

export function getMcpStatus(): Promise<McpStatus> {
  return api<McpStatus>("/api/mcp/status");
}

/**
 * Ping /api/mcp/health with the user's MCP token to verify config.
 *
 * - 200 → { ok: true }
 * - 401 → { ok: false, status: 401, message: "token 错或服务端未配置 GEO_MCP_TOKEN" }
 * - 其它 → { ok: false, status, message }
 * - 网络错 → { ok: false, status: 0, message }
 *
 * NOTE: 不通过 core.api 因为我们要观察 401 状态本身而不是抛错 / 触发全局退登。
 */
export async function pingMcpHealth(token: string): Promise<McpHealthResult> {
  try {
    const resp = await fetch("/api/mcp/health", {
      headers: { "X-MCP-Token": token },
      credentials: "include",
    });
    if (resp.status === 200) {
      return { ok: true };
    }
    if (resp.status === 401) {
      return {
        ok: false,
        status: 401,
        message: "token 错或服务端未配置 GEO_MCP_TOKEN",
      };
    }
    const text = await resp.text().catch(() => "");
    return {
      ok: false,
      status: resp.status,
      message: text || `HTTP ${resp.status}`,
    };
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : "网络错误";
    return { ok: false, status: 0, message };
  }
}
