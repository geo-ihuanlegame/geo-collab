// web/src/features/pipelines/AgentManagementWorkspace.tsx
import { useCallback, useEffect, useState } from "react";
import {
  createPipeline, deletePipeline, listPipelines, patchPipeline, startRun,
} from "../../api/pipelines";
import { useToast } from "../../components/Toast";
import type { Pipeline } from "../../types";
import { PipelineEditor } from "./PipelineEditor";

const TYPES = [
  { v: "general", label: "通用" },
  { v: "generation", label: "生成型" },
  { v: "distribution", label: "分发型" },
];
const KINDS = [
  { v: "none", label: "不定时" },
  { v: "hourly", label: "每小时" },
  { v: "daily", label: "每天" },
  { v: "weekly", label: "每周" },
];
const WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

type FormState = {
  id: number | null; name: string; type: string; tagsText: string;
  ignore_exception: boolean; is_enabled: boolean; schedule_kind: string;
  schedule_minute: number; schedule_hour: number; schedule_weekday: number;
  window_start: string; window_end: string;
};
const EMPTY: FormState = {
  id: null, name: "", type: "general", tagsText: "", ignore_exception: false,
  is_enabled: true, schedule_kind: "none", schedule_minute: 0, schedule_hour: 9,
  schedule_weekday: 0, window_start: "", window_end: "",
};

function scheduleSummary(p: Pipeline): string {
  if (p.schedule_kind === "none") return "—";
  const mm = String(p.schedule_minute ?? 0).padStart(2, "0");
  const hh = String(p.schedule_hour ?? 0).padStart(2, "0");
  if (p.schedule_kind === "hourly") return `每小时 :${mm}`;
  if (p.schedule_kind === "daily") return `每天 ${hh}:${mm}`;
  if (p.schedule_kind === "weekly") return `${WEEKDAYS[p.schedule_weekday ?? 0]} ${hh}:${mm}`;
  return "—";
}

export function AgentManagementWorkspace() {
  const { toast } = useToast();
  const [items, setItems] = useState<Pipeline[]>([]);
  const [form, setForm] = useState<FormState | null>(null);
  const [editingId, setEditingId] = useState<number | null>(null);

  const reload = useCallback(async () => {
    try { setItems(await listPipelines()); }
    catch (e) { toast(e instanceof Error ? e.message : "加载失败", "error"); }
  }, [toast]);
  useEffect(() => { reload(); }, [reload]);

  const openCreate = () => setForm({ ...EMPTY });
  const openEdit = (p: Pipeline) => setForm({
    id: p.id, name: p.name, type: p.type, tagsText: (p.tags || []).join(","),
    ignore_exception: p.ignore_exception, is_enabled: p.is_enabled,
    schedule_kind: p.schedule_kind, schedule_minute: p.schedule_minute ?? 0,
    schedule_hour: p.schedule_hour ?? 9, schedule_weekday: p.schedule_weekday ?? 0,
    window_start: (p.window_start ?? "").slice(0, 5), window_end: (p.window_end ?? "").slice(0, 5),
  });

  const buildPayload = (f: FormState) => {
    const tags = f.tagsText.split(",").map((s) => s.trim()).filter(Boolean);
    const base: Record<string, unknown> = {
      name: f.name, type: f.type, tags, ignore_exception: f.ignore_exception,
      is_enabled: f.is_enabled, schedule_kind: f.schedule_kind,
      window_start: f.window_start ? f.window_start + ":00" : null,
      window_end: f.window_end ? f.window_end + ":00" : null,
      schedule_minute: null, schedule_hour: null, schedule_weekday: null,
    };
    if (["hourly", "daily", "weekly"].includes(f.schedule_kind)) base.schedule_minute = f.schedule_minute;
    if (["daily", "weekly"].includes(f.schedule_kind)) base.schedule_hour = f.schedule_hour;
    if (f.schedule_kind === "weekly") base.schedule_weekday = f.schedule_weekday;
    return base;
  };

  const save = async () => {
    if (!form) return;
    try {
      const payload = buildPayload(form);
      if (form.id == null) await createPipeline(payload as { name: string });
      else await patchPipeline(form.id, payload as { name?: string });
      setForm(null); reload(); toast("已保存", "success");
    } catch (e) { toast(e instanceof Error ? e.message : "保存失败", "error"); }
  };

  const remove = async (p: Pipeline) => {
    if (!window.confirm(`确认删除智能体「${p.name}」？此操作不可撤销。`)) return;
    try { await deletePipeline(p.id); reload(); } catch (e) { toast(e instanceof Error ? e.message : "删除失败", "error"); }
  };

  const runNow = async (p: Pipeline) => {
    try { await startRun(p.id); toast("已触发运行", "success"); }
    catch (e) { toast(e instanceof Error ? e.message : "运行失败（需先发布节点）", "error"); }
  };

  // 工作流编排绑定到具体智能体：进入某智能体 → 全页节点编辑器（复用 PipelineEditor）。
  if (editingId != null) {
    const agent = items.find((p) => p.id === editingId);
    return (
      <div className="agentsWorkspace">
        <div className="topbar">
          <div>
            <p className="eyebrow">智能体 · 工作流</p>
            <h1>{agent ? agent.name : `智能体 ${editingId}`}</h1>
          </div>
          <button onClick={() => { setEditingId(null); reload(); }}>← 返回智能体列表</button>
        </div>
        {/* key 让切换智能体时 PipelineEditor 重挂载，重置 runStatus/轮询 timer，避免在途轮询脏写到另一个智能体 */}
        <PipelineEditor key={editingId} pipelineId={editingId} onChanged={reload} />
      </div>
    );
  }

  return (
    <div className="agentsWorkspace">
      <div className="topbar">
        <div><p className="eyebrow">智能体</p><h1>智能体管理</h1></div>
        <button className="agentNewBtn" onClick={openCreate}>+ 新建智能体</button>
      </div>

      <table className="agentTable">
        <thead>
          <tr>
            <th>名称</th><th>类型</th><th>标签</th><th>调度（北京时间）</th><th>启用</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          {items.map((p) => (
            <tr key={p.id}>
              <td>
                <span className="agentName">{p.name}</span>
                {p.has_draft && <span className="agentDraftDot" title="有未发布草稿">●</span>}
              </td>
              <td>{TYPES.find((t) => t.v === p.type)?.label ?? p.type}</td>
              <td>
                {(p.tags || []).length
                  ? (p.tags || []).map((t) => <span key={t} className="agentTag">{t}</span>)
                  : <span className="agentHint">—</span>}
              </td>
              <td>{scheduleSummary(p)}</td>
              <td>{p.is_enabled ? "启用" : "停用"}</td>
              <td>
                <div className="agentRowActions">
                  <button onClick={() => openEdit(p)}>编辑</button>
                  <button onClick={() => setEditingId(p.id)}>配置流程</button>
                  <button onClick={() => runNow(p)}>立即运行</button>
                  <button className="danger" onClick={() => remove(p)}>删除</button>
                </div>
              </td>
            </tr>
          ))}
          {items.length === 0 && (
            <tr><td colSpan={6}><div className="agentEmpty">还没有智能体，点右上角「新建智能体」开始。</div></td></tr>
          )}
        </tbody>
      </table>

      {form && (
        <div className="modalBackdrop" role="dialog" aria-modal="true" onClick={() => setForm(null)}>
          <div className="modal agentModal" onClick={(e) => e.stopPropagation()}>
            <div className="modalHeader">
              <div><h3>{form.id == null ? "新建智能体" : "编辑智能体"}</h3></div>
              <button onClick={() => setForm(null)} aria-label="关闭">✕</button>
            </div>
            <div className="modalContent">
              <div className="agentModalBody">
                <label className="agentField">
                  <span className="agentFieldLabel">名称（≤50）</span>
                  <input type="text" value={form.name} maxLength={50} placeholder="智能体名称"
                    onChange={(e) => setForm({ ...form, name: e.target.value })} />
                </label>
                <div className="agentFieldRow">
                  <label className="agentField">
                    <span className="agentFieldLabel">类型</span>
                    <select value={form.type} onChange={(e) => setForm({ ...form, type: e.target.value })}>
                      {TYPES.map((t) => <option key={t.v} value={t.v}>{t.label}</option>)}
                    </select>
                  </label>
                  <label className="agentField">
                    <span className="agentFieldLabel">标签（逗号分隔，≤5）</span>
                    <input type="text" value={form.tagsText} placeholder="如：营销, 日更"
                      onChange={(e) => setForm({ ...form, tagsText: e.target.value })} />
                  </label>
                </div>
                <div className="agentToggles">
                  <label className="agentToggle">
                    <input type="checkbox" checked={form.is_enabled}
                      onChange={(e) => setForm({ ...form, is_enabled: e.target.checked })} />
                    启用
                  </label>
                  <label className="agentToggle">
                    <input type="checkbox" checked={form.ignore_exception}
                      onChange={(e) => setForm({ ...form, ignore_exception: e.target.checked })} />
                    异常忽略（出错继续后续节点）
                  </label>
                </div>

                <div className="agentField">
                  <span className="agentFieldLabel">定时调度（北京时间）</span>
                  <div className="agentSchedule">
                    <label className="agentField">
                      <span className="agentFieldLabel">频率</span>
                      <select value={form.schedule_kind}
                        onChange={(e) => setForm({ ...form, schedule_kind: e.target.value })}>
                        {KINDS.map((k) => <option key={k.v} value={k.v}>{k.label}</option>)}
                      </select>
                    </label>
                    {form.schedule_kind === "weekly" && (
                      <label className="agentField">
                        <span className="agentFieldLabel">星期</span>
                        <select value={form.schedule_weekday}
                          onChange={(e) => setForm({ ...form, schedule_weekday: Number(e.target.value) })}>
                          {WEEKDAYS.map((w, i) => <option key={i} value={i}>{w}</option>)}
                        </select>
                      </label>
                    )}
                    {["daily", "weekly"].includes(form.schedule_kind) && (
                      <label className="agentField">
                        <span className="agentFieldLabel">时</span>
                        <input type="number" min={0} max={23} value={form.schedule_hour}
                          onChange={(e) => setForm({ ...form, schedule_hour: Number(e.target.value) })} />
                      </label>
                    )}
                    {["hourly", "daily", "weekly"].includes(form.schedule_kind) && (
                      <label className="agentField">
                        <span className="agentFieldLabel">分</span>
                        <input type="number" min={0} max={59} value={form.schedule_minute}
                          onChange={(e) => setForm({ ...form, schedule_minute: Number(e.target.value) })} />
                      </label>
                    )}
                  </div>
                </div>

                {form.schedule_kind !== "none" && (
                  <div className="agentFieldRow">
                    <label className="agentField">
                      <span className="agentFieldLabel">时间窗起（可选）</span>
                      <input type="time" value={form.window_start}
                        onChange={(e) => setForm({ ...form, window_start: e.target.value })} />
                    </label>
                    <label className="agentField">
                      <span className="agentFieldLabel">时间窗止（可选）</span>
                      <input type="time" value={form.window_end}
                        onChange={(e) => setForm({ ...form, window_end: e.target.value })} />
                    </label>
                  </div>
                )}
              </div>
            </div>
            <div className="modalActions">
              <button onClick={() => setForm(null)}>取消</button>
              <button onClick={save}>保存</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
