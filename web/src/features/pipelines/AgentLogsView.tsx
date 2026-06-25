// web/src/features/pipelines/AgentLogsView.tsx
import { useCallback, useEffect, useState } from "react";
import { getPipeline, listPipelineLogs } from "../../api/pipelines";
import { useToast } from "../../components/Toast";
import type { Pipeline, RunLogRow } from "../../types";

function fmtTime(t: string | null): string {
  if (!t) return "—";
  return new Date(t).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
}

function fmtDuration(ms: number | null): string {
  if (ms == null) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

const ERR = { color: "#c0392b" };
const ERR_BOLD = { color: "#c0392b", fontWeight: 600 };

export function AgentLogsView({ pipelineId, onBack }:
  { pipelineId: number; onBack: () => void }) {
  const { toast } = useToast();
  const [rows, setRows] = useState<RunLogRow[]>([]);
  const [total, setTotal] = useState(0);
  const [agent, setAgent] = useState<Pipeline | null>(null);
  const [loading, setLoading] = useState(true);

  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(30);
  // 输入框值（编辑中）；点「筛选」后才落到 applied*，再用于请求
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [appliedStart, setAppliedStart] = useState("");
  const [appliedEnd, setAppliedEnd] = useState("");

  // 智能体名只随 pipelineId 拉一次，不随翻页/筛选反复请求
  useEffect(() => {
    let alive = true;
    getPipeline(pipelineId)
      .then((a) => { if (alive) setAgent(a); })
      .catch(() => { /* 标题非关键：失败静默，日志加载失败另有 toast */ });
    return () => { alive = false; };
  }, [pipelineId]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await listPipelineLogs(pipelineId, {
        page,
        pageSize,
        startDate: appliedStart || undefined,
        endDate: appliedEnd || undefined,
      });
      setRows(r.items);
      setTotal(r.total);
    } catch (e) {
      toast(e instanceof Error ? e.message : "加载日志失败", "error");
    } finally {
      setLoading(false);
    }
  }, [pipelineId, page, pageSize, appliedStart, appliedEnd, toast]);
  useEffect(() => { load(); }, [load]);

  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  // total 变化后若当前页越界（如筛选缩小结果集），回退到最后一页
  useEffect(() => {
    if (page > totalPages) setPage(totalPages);
  }, [page, totalPages]);

  const isErrBatch = (s: string) => s === "failed" || s === "partial_failed";

  const applyFilter = () => {
    setAppliedStart(startDate);
    setAppliedEnd(endDate);
    setPage(1);
  };
  const resetFilter = () => {
    setStartDate("");
    setEndDate("");
    setAppliedStart("");
    setAppliedEnd("");
    setPage(1);
  };

  return (
    <div className="agentsWorkspace">
      <div className="topbar">
        <div>
          <p className="eyebrow">智能体 · 日志</p>
          <h1>{agent ? agent.name : `智能体 ${pipelineId}`}</h1>
        </div>
        <div className="topActions">
          <button className="secondaryButton" onClick={load}>刷新</button>
          <button className="secondaryButton" onClick={onBack}>← 返回智能体列表</button>
        </div>
      </div>

      <div className="agentLogsFilter">
        <label>
          开始日期
          <input
            type="date"
            value={startDate}
            max={endDate || undefined}
            onChange={(e) => setStartDate(e.target.value)}
          />
        </label>
        <label>
          结束日期
          <input
            type="date"
            value={endDate}
            min={startDate || undefined}
            onChange={(e) => setEndDate(e.target.value)}
          />
        </label>
        <button onClick={applyFilter}>筛选</button>
        <button onClick={resetFilter}>重置</button>
      </div>

      <div className="agentLogsScroll">
        <table className="agentTable">
          <thead>
            <tr>
              <th>批次</th><th>任务名称</th><th>步骤</th><th>日志等级</th><th>日志</th><th>耗时</th><th>时间</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.batch}-${r.step}`}>
                <td style={isErrBatch(r.run_status) ? ERR : undefined}>{r.batch}</td>
                <td>{r.task_name}</td>
                <td>{r.step}</td>
                <td style={r.level === "ERROR" ? ERR_BOLD : undefined}>{r.level}</td>
                <td style={r.level === "ERROR" ? ERR : undefined}>{r.message}</td>
                <td>{fmtDuration(r.duration_ms)}</td>
                <td>{fmtTime(r.time)}</td>
              </tr>
            ))}
            {!loading && rows.length === 0 && (
              <tr><td colSpan={6}><div className="agentEmpty">暂无运行日志</div></td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="agentLogsPager">
        <button disabled={page <= 1 || loading} onClick={() => setPage((p) => Math.max(1, p - 1))}>
          上一页
        </button>
        <span className="agentLogsPageInfo">第 {page} / {totalPages} 页 · 共 {total} 条</span>
        <button disabled={page >= totalPages || loading} onClick={() => setPage((p) => p + 1)}>
          下一页
        </button>
        <select
          value={pageSize}
          onChange={(e) => { setPageSize(Number(e.target.value)); setPage(1); }}
        >
          <option value={20}>20 条/页</option>
          <option value={30}>30 条/页</option>
        </select>
      </div>
    </div>
  );
}
