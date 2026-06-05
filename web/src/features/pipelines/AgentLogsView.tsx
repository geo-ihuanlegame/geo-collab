// web/src/features/pipelines/AgentLogsView.tsx
import { useCallback, useEffect, useState } from "react";
import { getPipeline, listPipelineLogs } from "../../api/pipelines";
import { useToast } from "../../components/Toast";
import type { Pipeline, RunLogRow } from "../../types";

function fmtTime(t: string | null): string {
  if (!t) return "—";
  return new Date(t).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
}

const ERR = { color: "#c0392b" };
const ERR_BOLD = { color: "#c0392b", fontWeight: 600 };

export function AgentLogsView({ pipelineId, onBack }:
  { pipelineId: number; onBack: () => void }) {
  const { toast } = useToast();
  const [rows, setRows] = useState<RunLogRow[]>([]);
  const [agent, setAgent] = useState<Pipeline | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [a, r] = await Promise.all([getPipeline(pipelineId), listPipelineLogs(pipelineId)]);
      setAgent(a);
      setRows(r);
    } catch (e) {
      toast(e instanceof Error ? e.message : "加载日志失败", "error");
    } finally {
      setLoading(false);
    }
  }, [pipelineId, toast]);
  useEffect(() => { load(); }, [load]);

  const isErrBatch = (s: string) => s === "failed" || s === "partial_failed";

  return (
    <div className="agentsWorkspace">
      <div className="topbar">
        <div>
          <p className="eyebrow">智能体 · 日志</p>
          <h1>{agent ? agent.name : `智能体 ${pipelineId}`}</h1>
        </div>
        <div className="agentRowActions">
          <button onClick={load}>刷新</button>
          <button onClick={onBack}>← 返回智能体列表</button>
        </div>
      </div>

      <table className="agentTable">
        <thead>
          <tr>
            <th>批次</th><th>任务名称</th><th>步骤</th><th>日志等级</th><th>日志</th><th>时间</th>
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
              <td>{fmtTime(r.time)}</td>
            </tr>
          ))}
          {!loading && rows.length === 0 && (
            <tr><td colSpan={6}><div className="agentEmpty">暂无运行日志</div></td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
