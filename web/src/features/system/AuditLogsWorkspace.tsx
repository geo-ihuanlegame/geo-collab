import { useEffect, useState } from "react";
import { RefreshCw, Search, RotateCcw } from "lucide-react";
import { api } from "../../api/client";
import { useToast } from "../../components/Toast";
import { formatDateTime } from "../../utils/dateFormat";

type AuditLogItem = {
  id: number;
  user_id: number | null;
  username: string | null;
  action: string;
  target_type: string;
  target_id: string | null;
  payload_json: unknown | null;
  ip_address: string | null;
  user_agent: string | null;
  created_at: string;
};

type AuditLogListResponse = {
  items: AuditLogItem[];
  next_cursor: number | null;
};

const TARGET_TYPE_OPTIONS = [
  "user",
  "account",
  "article",
  "article_group",
  "task",
  "publish_record",
  "skill",
  "prompt_template",
  "stock_category",
  "stock_image",
  "generation_session",
  "question_pool",
  "asset",
  "system",
];

type Filters = {
  user_id: string;
  action_prefix: string;
  target_type: string;
  start_at: string;
  end_at: string;
};

const EMPTY_FILTERS: Filters = {
  user_id: "",
  action_prefix: "",
  target_type: "",
  start_at: "",
  end_at: "",
};

function buildQuery(filters: Filters, cursor: number | null, limit = 100): string {
  const params = new URLSearchParams();
  if (filters.user_id.trim()) params.set("user_id", filters.user_id.trim());
  if (filters.action_prefix.trim()) params.set("action_prefix", filters.action_prefix.trim());
  if (filters.target_type.trim()) params.set("target_type", filters.target_type.trim());
  if (filters.start_at) {
    const d = new Date(filters.start_at);
    if (!Number.isNaN(d.getTime())) params.set("start_at", d.toISOString());
  }
  if (filters.end_at) {
    const d = new Date(filters.end_at);
    if (!Number.isNaN(d.getTime())) params.set("end_at", d.toISOString());
  }
  if (cursor !== null) params.set("cursor", String(cursor));
  params.set("limit", String(limit));
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

// 按 action 前缀的不同段，映射到现有 badge 颜色，使列表更易扫读。
function actionBadgeClass(action: string): string {
  const prefix = action.split(".")[0] ?? "";
  switch (prefix) {
    case "user":
    case "auth":
      return "running";
    case "account":
    case "task":
    case "publish_record":
      return "succeeded";
    case "article":
    case "article_group":
    case "skill":
    case "prompt_template":
    case "generation_session":
      return "pending";
    case "stock_category":
    case "stock_image":
    case "image_library":
    case "asset":
      return "waiting_manual_publish";
    case "system":
      return "cancelled";
    default:
      return "pending";
  }
}

function truncate(value: string | null | undefined, max: number): string {
  if (!value) return "—";
  if (value.length <= max) return value;
  return `${value.slice(0, max)}…`;
}

export function AuditLogsWorkspace() {
  const { toast } = useToast();
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  // 已应用的筛选条件（点击"筛选"后才会落到 appliedFilters，再用于请求）
  const [appliedFilters, setAppliedFilters] = useState<Filters>(EMPTY_FILTERS);
  const [items, setItems] = useState<AuditLogItem[]>([]);
  const [nextCursor, setNextCursor] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);

  async function loadFirstPage(currentFilters: Filters) {
    setLoading(true);
    try {
      const data = await api<AuditLogListResponse>(`/api/audit-logs${buildQuery(currentFilters, null)}`);
      setItems(data.items);
      setNextCursor(data.next_cursor);
    } catch (err) {
      toast(err instanceof Error ? err.message : "加载审计日志失败", "error");
    } finally {
      setLoading(false);
    }
  }

  async function loadMore() {
    if (nextCursor === null || loadingMore) return;
    setLoadingMore(true);
    try {
      const data = await api<AuditLogListResponse>(
        `/api/audit-logs${buildQuery(appliedFilters, nextCursor)}`,
      );
      setItems((prev) => [...prev, ...data.items]);
      setNextCursor(data.next_cursor);
    } catch (err) {
      toast(err instanceof Error ? err.message : "加载下一页失败", "error");
    } finally {
      setLoadingMore(false);
    }
  }

  useEffect(() => {
    void loadFirstPage(EMPTY_FILTERS);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function handleApplyFilters(e: React.FormEvent) {
    e.preventDefault();
    if (filters.user_id.trim() && Number.isNaN(Number(filters.user_id.trim()))) {
      toast("user_id 必须是数字", "error");
      return;
    }
    setAppliedFilters(filters);
    void loadFirstPage(filters);
  }

  function handleReset() {
    setFilters(EMPTY_FILTERS);
    setAppliedFilters(EMPTY_FILTERS);
    void loadFirstPage(EMPTY_FILTERS);
  }

  function handleRefresh() {
    void loadFirstPage(appliedFilters);
  }

  return (
    <>
      <header className="topbar">
        <div>
          <p className="eyebrow">系统管理</p>
          <h1>审计日志</h1>
        </div>
        <div className="topActions">
          <button
            className="secondaryButton"
            type="button"
            disabled={loading}
            onClick={handleRefresh}
          >
            <RefreshCw size={15} />
            刷新
          </button>
        </div>
      </header>

      <form
        className="panel"
        style={{ marginBottom: 16, display: "flex", flexWrap: "wrap", gap: 12, alignItems: "flex-end" }}
        onSubmit={handleApplyFilters}
      >
        <label style={filterLabelStyle}>
          <span style={filterLabelTextStyle}>用户 ID</span>
          <input
            type="number"
            min={1}
            value={filters.user_id}
            onChange={(e) => setFilters((f) => ({ ...f, user_id: e.target.value }))}
            placeholder="例如 3"
            style={inputStyle}
          />
        </label>
        <label style={filterLabelStyle}>
          <span style={filterLabelTextStyle}>动作前缀</span>
          <input
            type="text"
            value={filters.action_prefix}
            onChange={(e) => setFilters((f) => ({ ...f, action_prefix: e.target.value }))}
            placeholder="例如 account."
            style={inputStyle}
          />
        </label>
        <label style={filterLabelStyle}>
          <span style={filterLabelTextStyle}>目标类型</span>
          <select
            value={filters.target_type}
            onChange={(e) => setFilters((f) => ({ ...f, target_type: e.target.value }))}
            style={inputStyle}
          >
            <option value="">全部</option>
            {TARGET_TYPE_OPTIONS.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <label style={filterLabelStyle}>
          <span style={filterLabelTextStyle}>开始时间</span>
          <input
            type="datetime-local"
            value={filters.start_at}
            onChange={(e) => setFilters((f) => ({ ...f, start_at: e.target.value }))}
            style={inputStyle}
          />
        </label>
        <label style={filterLabelStyle}>
          <span style={filterLabelTextStyle}>结束时间</span>
          <input
            type="datetime-local"
            value={filters.end_at}
            onChange={(e) => setFilters((f) => ({ ...f, end_at: e.target.value }))}
            style={inputStyle}
          />
        </label>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="primaryButton" type="submit" disabled={loading}>
            <Search size={15} />
            筛选
          </button>
          <button className="secondaryButton" type="button" onClick={handleReset} disabled={loading}>
            <RotateCcw size={15} />
            重置
          </button>
        </div>
      </form>

      <div className="panel" style={{ padding: 0, overflow: "hidden" }}>
        {loading && items.length === 0 ? (
          <p style={{ padding: 24, color: "#64748b" }}>加载中…</p>
        ) : items.length === 0 ? (
          <p style={{ padding: 24, color: "#64748b" }}>暂无审计日志</p>
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr style={{ borderBottom: "1px solid var(--border)", background: "var(--bg-alt)" }}>
                  <th style={thStyle}>时间</th>
                  <th style={thStyle}>用户</th>
                  <th style={thStyle}>动作</th>
                  <th style={thStyle}>目标</th>
                  <th style={thStyle}>IP</th>
                  <th style={thStyle}>Payload</th>
                </tr>
              </thead>
              <tbody>
                {items.map((it) => {
                  const targetLabel = it.target_id ? `${it.target_type}:${it.target_id}` : it.target_type;
                  const payloadStr =
                    it.payload_json === null || it.payload_json === undefined
                      ? null
                      : JSON.stringify(it.payload_json, null, 2);
                  return (
                    <tr key={it.id} style={{ borderBottom: "1px solid var(--border)", verticalAlign: "top" }}>
                      <td style={tdStyle}>
                        <span style={{ whiteSpace: "nowrap" }}>{formatDateTime(it.created_at)}</span>
                      </td>
                      <td style={tdStyle}>
                        {it.username ? (
                          <>
                            <span style={{ fontWeight: 500 }}>{it.username}</span>
                            {it.user_id !== null && (
                              <span style={{ marginLeft: 4, color: "#94a3b8", fontSize: 12 }}>
                                ({it.user_id})
                              </span>
                            )}
                          </>
                        ) : (
                          <span style={{ color: "#94a3b8" }}>—</span>
                        )}
                      </td>
                      <td style={tdStyle}>
                        <span className={`badge ${actionBadgeClass(it.action)}`}>{it.action}</span>
                      </td>
                      <td style={tdStyle}>
                        <span style={{ fontFamily: "var(--mono, monospace)", fontSize: 12 }}>
                          {targetLabel}
                        </span>
                      </td>
                      <td style={tdStyle}>
                        <span
                          title={it.ip_address ?? ""}
                          style={{
                            fontFamily: "var(--mono, monospace)",
                            fontSize: 12,
                            color: it.ip_address ? undefined : "#94a3b8",
                          }}
                        >
                          {truncate(it.ip_address, 24)}
                        </span>
                      </td>
                      <td style={tdStyle}>
                        {payloadStr ? (
                          <details>
                            <summary style={{ cursor: "pointer", color: "var(--accent-deep, #0369a1)", fontSize: 12 }}>
                              查看
                            </summary>
                            <pre
                              style={{
                                margin: "6px 0 0 0",
                                padding: 10,
                                background: "var(--bg-alt, #f8fafc)",
                                border: "1px solid var(--border, #e2e8f0)",
                                borderRadius: 6,
                                fontSize: 12,
                                whiteSpace: "pre-wrap",
                                wordBreak: "break-word",
                                maxWidth: 460,
                                maxHeight: 260,
                                overflow: "auto",
                              }}
                            >
                              {payloadStr}
                            </pre>
                          </details>
                        ) : (
                          <span style={{ color: "#94a3b8" }}>—</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <div
          style={{
            display: "flex",
            justifyContent: "center",
            padding: 16,
            borderTop: items.length > 0 ? "1px solid var(--border)" : undefined,
          }}
        >
          {nextCursor === null ? (
            <span style={{ color: "#94a3b8", fontSize: 13 }}>
              {items.length === 0 ? "" : "无更多"}
            </span>
          ) : (
            <button
              className="secondaryButton"
              type="button"
              disabled={loadingMore}
              onClick={() => void loadMore()}
            >
              {loadingMore ? "加载中…" : "加载更多"}
            </button>
          )}
        </div>
      </div>
    </>
  );
}

const thStyle: React.CSSProperties = {
  padding: "10px 16px",
  textAlign: "left",
  fontWeight: 600,
  color: "#64748b",
  fontSize: 12,
  textTransform: "uppercase",
  letterSpacing: "0.04em",
  whiteSpace: "nowrap",
};

const tdStyle: React.CSSProperties = {
  padding: "12px 16px",
  verticalAlign: "top",
};

const filterLabelStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 4,
  fontSize: 12,
};

const filterLabelTextStyle: React.CSSProperties = {
  color: "#64748b",
  fontWeight: 500,
};

const inputStyle: React.CSSProperties = {
  padding: "6px 10px",
  border: "1px solid var(--border, #e2e8f0)",
  borderRadius: 6,
  fontSize: 13,
  background: "white",
  minWidth: 160,
};
