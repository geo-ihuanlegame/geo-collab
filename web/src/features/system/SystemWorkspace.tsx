import { useEffect, useState } from "react";
import { api } from "../../api/client";
import type { SystemStatus } from "../../types";
import { RefreshCw } from "lucide-react";

export function SystemWorkspace() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  async function refresh() {
    setLoading(true);
    try {
      const data = await api<SystemStatus>("/api/system/status");
      setStatus(data);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "获取系统状态失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  return (
    <>
      <header className="topbar">
        <div>
          <p className="eyebrow">系统状态</p>
          <h1>运行信息</h1>
        </div>
        <div className="topActions">
          <button className="secondaryButton" type="button" disabled={loading} onClick={() => void refresh()}>
            <RefreshCw size={15} />
            刷新
          </button>
        </div>
      </header>

      {error ? (
        <div className="panel" style={{ borderColor: "var(--red-soft)", color: "var(--red)" }}>{error}</div>
      ) : null}

      {status ? (
        <div style={{ display: "grid", gap: 16, maxWidth: 760 }}>
          <div className="panel">
            <h2 style={{ marginBottom: 16 }}>服务</h2>
            <dl className="statGrid">
              <dt>状态</dt>
              <dd><span className="badge succeeded">✓ {status.service}</span></dd>
              <dt>浏览器（Chrome）</dt>
              <dd>
                <span className={`badge ${status.browser_ready ? "succeeded" : "failed"}`}>
                  {status.browser_ready ? "已检测到" : "未找到"}
                </span>
              </dd>
              <dt>发布 Worker</dt>
              <dd>
                <span className={`badge ${status.worker_online ? "succeeded" : "failed"}`}>
                  {status.worker_online ? "在线" : "未检测到"}
                </span>
              </dd>
              <dt>noVNC 运行时</dt>
              <dd>
                <span className={`badge ${status.novnc_runtime_ready ? "succeeded" : "failed"}`}>
                  {status.novnc_runtime_ready ? "就绪" : "未就绪"}
                </span>
              </dd>
            </dl>
          </div>

          <div className="panel">
            <h2 style={{ marginBottom: 16 }}>数据</h2>
            <dl className="statGrid">
              <dt>文章</dt>
              <dd>{status.article_count} 篇</dd>
              <dt>账号</dt>
              <dd>{status.account_count} 个</dd>
              <dt>任务</dt>
              <dd>{status.task_count} 个</dd>
              <dt>待执行任务</dt>
              <dd>{status.pending_task_count} 个</dd>
              <dt>远程浏览器会话</dt>
              <dd>{status.active_browser_sessions} 个</dd>
              <dt>目录就绪</dt>
              <dd>
                <span className={`badge ${status.directories_ready ? "succeeded" : "failed"}`}>
                  {status.directories_ready ? "是" : "否"}
                </span>
              </dd>
            </dl>
          </div>
        </div>
      ) : loading ? (
        <p style={{ color: "#64748b" }}>加载中…</p>
      ) : null}
    </>
  );
}
