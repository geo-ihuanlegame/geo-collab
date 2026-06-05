// web/src/features/pipelines/PipelineEditor.tsx
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { listAccounts } from "../../api/accounts";
import { listArticleGroups } from "../../api/articles";
import {
  discardDraft, getNodeTypes, getPipeline, getRun, publishPipeline, saveDraft, startRun,
} from "../../api/pipelines";
import { useToast } from "../../components/Toast";
import type { Account, ArticleGroup, NodeTypeDef, Pipeline, PipelineNodeDef } from "../../types";
import { VersionHistory } from "./VersionHistory";

export function PipelineEditor({ pipelineId, onChanged }:
  { pipelineId: number; onChanged: () => void }) {
  const { toast } = useToast();
  const [nodes, setNodes] = useState<PipelineNodeDef[]>([]);
  const [hasDraft, setHasDraft] = useState(false);
  const [nodeTypes, setNodeTypes] = useState<NodeTypeDef[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [showVersions, setShowVersions] = useState(false);
  const [runStatus, setRunStatus] = useState<string | null>(null);
  const [groups, setGroups] = useState<ArticleGroup[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const pollRef = useRef<number | null>(null);

  const load = useCallback(async () => {
    const p: Pipeline = await getPipeline(pipelineId);
    setNodes(p.nodes);
    setHasDraft(p.has_draft);
    setSelected(p.nodes.length ? 0 : null);
  }, [pipelineId]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { getNodeTypes().then((r) => setNodeTypes(r.node_types)).catch(() => {}); }, []);
  useEffect(() => {
    listArticleGroups().then(setGroups).catch(() => {});
    listAccounts().then(setAccounts).catch(() => {});
  }, []);

  // Stop polling and reset run status when switching pipelines.
  useEffect(() => {
    if (pollRef.current != null) { clearInterval(pollRef.current); pollRef.current = null; }
    setRunStatus(null);
  }, [pipelineId]);

  // Clear any pending poll on unmount.
  useEffect(() => () => {
    if (pollRef.current != null) { clearInterval(pollRef.current); pollRef.current = null; }
  }, []);

  const reindex = (list: PipelineNodeDef[]) => list.map((n, i) => ({ ...n, node_index: i }));

  const addNode = (type: string) => {
    const def = nodeTypes.find((t) => t.type === type);
    const next = reindex([...nodes, {
      node_type: type, name: def?.label ?? type, node_index: nodes.length,
      config: {}, flow_meta: null,
    }]);
    setNodes(next); setSelected(next.length - 1);
  };
  const removeNode = (i: number) => {
    const next = reindex(nodes.filter((_, idx) => idx !== i));
    setNodes(next); setSelected(next.length ? 0 : null);
  };
  const move = (i: number, dir: -1 | 1) => {
    const j = i + dir;
    if (j < 0 || j >= nodes.length) return;
    const copy = [...nodes];
    [copy[i], copy[j]] = [copy[j], copy[i]];
    setNodes(reindex(copy)); setSelected(j);
  };
  const updateNode = (i: number, patch: Partial<PipelineNodeDef>) =>
    setNodes(nodes.map((n, idx) => (idx === i ? { ...n, ...patch } : n)));

  // Serialize nodes for save/publish. Empty (whitespace-only field) conditions are
  // emitted as null instead of persisting {field:"",op:"eq",value:""}. Does not mutate state.
  const snapshot = useMemo(() => ({
    schemaVersion: 1,
    nodes: nodes.map((n) => {
      if (n.flow_meta == null) return n;
      if (n.flow_meta.condition && !n.flow_meta.condition.field.trim()) {
        return { ...n, flow_meta: { ...n.flow_meta, condition: null } };
      }
      return n;
    }),
  }), [nodes]);

  const onSaveDraft = async () => {
    await saveDraft(pipelineId, snapshot); setHasDraft(true); onChanged();
    toast("草稿已保存", "success");
  };
  const onPublish = async () => {
    await saveDraft(pipelineId, snapshot);
    const { version_no } = await publishPipeline(pipelineId);
    setHasDraft(false); onChanged(); toast(`已发布 v${version_no}`, "success");
  };
  const onDiscard = async () => {
    if (!window.confirm("丢弃未发布改动？")) return;
    await discardDraft(pipelineId); await load(); onChanged();
  };
  const onRun = async () => {
    try {
      const { run_id } = await startRun(pipelineId);
      setRunStatus("running");
      if (pollRef.current != null) { clearInterval(pollRef.current); pollRef.current = null; }
      let failures = 0;
      pollRef.current = setInterval(async () => {
        try {
          const r = await getRun(run_id);
          failures = 0;
          setRunStatus(`${r.status}（文章 ${r.article_ids.length} 篇）`);
          if (["done", "failed", "partial_failed"].includes(r.status)) {
            if (pollRef.current != null) { clearInterval(pollRef.current); pollRef.current = null; }
          }
        } catch {
          failures += 1;
          if (failures >= 5 && pollRef.current != null) {
            clearInterval(pollRef.current); pollRef.current = null;
            setRunStatus("运行状态获取失败，请刷新");
          }
        }
      }, 1500);
    } catch (e) {
      toast(e instanceof Error ? e.message : "运行失败", "error");
    }
  };

  const sel = selected != null ? nodes[selected] : null;
  const selDef = sel ? nodeTypes.find((t) => t.type === sel.node_type) : null;

  return (
    <div>
      <div style={{ marginBottom: 8 }}>
        {hasDraft && <span style={{ color: "orange", marginRight: 8 }}>● 有未发布草稿</span>}
        <button onClick={onSaveDraft}>保存草稿</button>
        <button onClick={onPublish}>发布</button>
        <button onClick={onDiscard} disabled={!hasDraft}>丢弃草稿</button>
        <button onClick={() => setShowVersions((v) => !v)}>版本历史</button>
        <button onClick={onRun}>运行</button>
        {runStatus && <span style={{ marginLeft: 8 }}>运行状态：{runStatus}</span>}
      </div>

      <div style={{ marginBottom: 8 }}>
        {nodeTypes.map((t) => (
          <button key={t.type} onClick={() => addNode(t.type)}>+ {t.label}</button>
        ))}
      </div>

      <div style={{ display: "flex", gap: 16 }}>
        {/* 线性节点列表 */}
        <div style={{ width: 240 }}>
          {nodes.map((n, i) => (
            <div key={i} onClick={() => setSelected(i)}
                 style={{ border: i === selected ? "2px solid #06f" : "1px solid #ccc",
                          padding: 8, marginBottom: 6, cursor: "pointer" }}>
              <div>#{n.node_index} {n.name} <em>({n.node_type})</em></div>
              <button onClick={(e) => { e.stopPropagation(); move(i, -1); }}>↑</button>
              <button onClick={(e) => { e.stopPropagation(); move(i, 1); }}>↓</button>
              <button onClick={(e) => { e.stopPropagation(); removeNode(i); }}>删</button>
              {i < nodes.length - 1 && <div style={{ textAlign: "center" }}>↓</div>}
            </div>
          ))}
        </div>

        {/* 属性面板 */}
        <div style={{ flex: 1 }}>
          {sel && selDef ? (
            <div>
              <h4>{sel.name} 配置</h4>
              <label>节点名称
                <input value={sel.name}
                       onChange={(e) => updateNode(selected!, { name: e.target.value })} />
              </label>
              {selDef.config_schema.map((f) => (
                <div key={f.key}>
                  <label>{f.label}
                    {f.type === "article_group"
                      ? <select value={String(sel.config[f.key] ?? "")}
                          onChange={(e) => updateNode(selected!,
                            { config: { ...sel.config,
                              [f.key]: e.target.value ? Number(e.target.value) : undefined } })}>
                          <option value="">选择分组</option>
                          {groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
                        </select>
                      : f.type === "accounts"
                      ? <select multiple
                          value={((sel.config[f.key] as number[] | undefined) ?? []).map(String)}
                          onChange={(e) => updateNode(selected!,
                            { config: { ...sel.config,
                              [f.key]: Array.from(e.target.selectedOptions, (o) => Number(o.value)) } })}>
                          {accounts.map((a) => (
                            <option key={a.id} value={a.id}>{a.display_name}</option>
                          ))}
                        </select>
                      : f.type === "textarea"
                      ? <textarea value={String(sel.config[f.key] ?? "")}
                          onChange={(e) => updateNode(selected!,
                            { config: { ...sel.config, [f.key]: e.target.value } })} />
                      : <input type={f.type === "number" ? "number" : "text"}
                          value={String(sel.config[f.key] ?? "")}
                          onChange={(e) => updateNode(selected!,
                            { config: { ...sel.config,
                              [f.key]: f.type === "number" ? Number(e.target.value) : e.target.value } })} />}
                  </label>
                </div>
              ))}

              {/* 数据传递 */}
              <hr /><h5>数据传递</h5>
              <label>上游节点
                <select value={sel.flow_meta?.dependsOnIndex ?? ""}
                  onChange={(e) => updateNode(selected!, { flow_meta: {
                    ...(sel.flow_meta ?? {}),
                    dependsOnIndex: e.target.value === "" ? null : Number(e.target.value),
                  } })}>
                  <option value="">默认（合并全部上游）</option>
                  {nodes.filter((n) => n.node_index < sel.node_index).map((n) => (
                    <option key={n.node_index} value={n.node_index}>#{n.node_index} {n.name}</option>
                  ))}
                </select>
              </label>
              <div>
                <strong>字段映射</strong>
                {(sel.flow_meta?.inputMapping ?? []).map((m, mi) => (
                  <div key={mi}>
                    <input placeholder="上游字段" value={m.from}
                      onChange={(e) => {
                        const im = [...(sel.flow_meta?.inputMapping ?? [])];
                        im[mi] = { ...im[mi], from: e.target.value };
                        updateNode(selected!, { flow_meta: { ...(sel.flow_meta ?? {}), inputMapping: im } });
                      }} />→
                    <input placeholder="本节点字段" value={m.to}
                      onChange={(e) => {
                        const im = [...(sel.flow_meta?.inputMapping ?? [])];
                        im[mi] = { ...im[mi], to: e.target.value };
                        updateNode(selected!, { flow_meta: { ...(sel.flow_meta ?? {}), inputMapping: im } });
                      }} />
                    <button onClick={() => {
                      const im = (sel.flow_meta?.inputMapping ?? []).filter((_, x) => x !== mi);
                      updateNode(selected!, { flow_meta: { ...(sel.flow_meta ?? {}), inputMapping: im } });
                    }}>删</button>
                  </div>
                ))}
                <button onClick={() => {
                  const im = [...(sel.flow_meta?.inputMapping ?? []), { from: "", to: "" }];
                  updateNode(selected!, { flow_meta: { ...(sel.flow_meta ?? {}), inputMapping: im } });
                }}>+ 映射</button>
              </div>
              <div>
                <strong>跳过条件</strong>
                <input placeholder="字段" value={sel.flow_meta?.condition?.field ?? ""}
                  onChange={(e) => updateNode(selected!, { flow_meta: { ...(sel.flow_meta ?? {}),
                    condition: { field: e.target.value,
                      op: sel.flow_meta?.condition?.op ?? "eq",
                      value: sel.flow_meta?.condition?.value ?? "" } } })} />
                <select value={sel.flow_meta?.condition?.op ?? "eq"}
                  onChange={(e) => updateNode(selected!, { flow_meta: { ...(sel.flow_meta ?? {}),
                    condition: { field: sel.flow_meta?.condition?.field ?? "",
                      op: e.target.value as "eq" | "neq" | "contains",
                      value: sel.flow_meta?.condition?.value ?? "" } } })}>
                  <option value="eq">等于</option><option value="neq">不等于</option>
                  <option value="contains">包含</option>
                </select>
                <input placeholder="值" value={sel.flow_meta?.condition?.value ?? ""}
                  onChange={(e) => updateNode(selected!, { flow_meta: { ...(sel.flow_meta ?? {}),
                    condition: { field: sel.flow_meta?.condition?.field ?? "",
                      op: sel.flow_meta?.condition?.op ?? "eq", value: e.target.value } } })} />
              </div>
            </div>
          ) : <p>选择一个节点以编辑</p>}

          {showVersions && (
            <VersionHistory pipelineId={pipelineId}
              onRolledBack={async () => { await load(); onChanged(); setShowVersions(false);
                toast("已载入草稿，请确认后发布", "success"); }} />
          )}
        </div>
      </div>
    </div>
  );
}
