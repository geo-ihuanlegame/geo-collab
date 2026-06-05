// web/src/features/pipelines/PipelineEditor.tsx
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { listAccounts } from "../../api/accounts";
import { listAiEngines, listQuestionPools, listQuestionTypes } from "../../api/ai-generation";
import { listArticleGroups } from "../../api/articles";
import {
  discardDraft, getNodeTypes, getPipeline, getRun, publishPipeline, saveDraft, startRun,
} from "../../api/pipelines";
import { listPromptTemplates } from "../../api/prompt-templates";
import { useToast } from "../../components/Toast";
import type {
  Account, AiEngine, ArticleGroup, NodeTypeDef, Pipeline, PipelineNodeDef,
  PromptTemplate, QuestionPool,
} from "../../types";
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
  const [pools, setPools] = useState<QuestionPool[]>([]);
  const [engines, setEngines] = useState<AiEngine[]>([]);
  const [genTemplates, setGenTemplates] = useState<PromptTemplate[]>([]);
  // 每个池缓存问题类型选项；null 分类映射为「未分类」哨兵值，供下拉选取。
  const [typesByPool, setTypesByPool] =
    useState<Record<number, { value: string; label: string }[]>>({});
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
    listQuestionPools().then(setPools).catch(() => {});
    listAiEngines().then(setEngines).catch(() => {});
    listPromptTemplates("generation")
      .then((ts) => setGenTemplates(ts.filter((t) => t.scope === "generation" && t.is_enabled)))
      .catch(() => {});
  }, []);

  // Lazily load a pool's question types (cascade for question_type fields).
  const ensureTypes = useCallback((poolId: number) => {
    if (poolId && typesByPool[poolId] === undefined) {
      listQuestionTypes(poolId)
        .then((ts) => setTypesByPool((m) => ({
          ...m,
          [poolId]: ts.map((t) => t.question_type
            ? { value: t.question_type, label: t.question_type }
            : { value: "__uncategorized__", label: "未分类" }),
        })))
        .catch(() => setTypesByPool((m) => ({ ...m, [poolId]: [] })));
    }
  }, [typesByPool]);

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
      const timer = window.setInterval(async () => {
        // 只有当本 timer 仍是当前轮询时才处理（防切换/重跑后的脏写）
        if (pollRef.current !== timer) { clearInterval(timer); return; }
        try {
          const r = await getRun(run_id);
          if (pollRef.current !== timer) { clearInterval(timer); return; }
          failures = 0;
          setRunStatus(`${r.status}（文章 ${r.article_ids.length} 篇）`);
          if (["done", "failed", "partial_failed"].includes(r.status)) {
            clearInterval(timer);
            if (pollRef.current === timer) pollRef.current = null;
          }
        } catch {
          failures += 1;
          if (failures >= 5) {
            clearInterval(timer);
            if (pollRef.current === timer) { pollRef.current = null; setRunStatus("运行状态获取失败，请刷新"); }
          }
        }
      }, 1500);
      pollRef.current = timer;
    } catch (e) {
      toast(e instanceof Error ? e.message : "运行失败", "error");
    }
  };

  const sel = selected != null ? nodes[selected] : null;
  const selDef = sel ? nodeTypes.find((t) => t.type === sel.node_type) : null;

  return (
    <div className="peEditor">
      <div className="peToolbar">
        {hasDraft && <span className="peDraftBadge">● 有未发布草稿</span>}
        <button onClick={onSaveDraft}>保存草稿</button>
        <button className="peBtnPrimary" onClick={onPublish}>发布</button>
        <button onClick={onDiscard} disabled={!hasDraft}>丢弃草稿</button>
        <button onClick={() => setShowVersions((v) => !v)}>版本历史</button>
        <button onClick={onRun}>运行</button>
        {runStatus && <span className="peRunStatus">运行状态：{runStatus}</span>}
      </div>

      <div className="peAddBar">
        <span className="peAddLabel">添加节点</span>
        {nodeTypes.map((t) => (
          <button key={t.type} className="peAddBtn" onClick={() => addNode(t.type)}>+ {t.label}</button>
        ))}
      </div>

      <div className="peLayout">
        {/* 线性节点列表 */}
        <div className="peNodeList">
          {nodes.length === 0 && <p className="agentHint">还没有节点，从上方「添加节点」开始。</p>}
          {nodes.map((n, i) => (
            <div key={i}>
              <div className={`peNode${i === selected ? " selected" : ""}`} onClick={() => setSelected(i)}>
                <div className="peNodeHead">
                  <span className="peNodeIdx">#{n.node_index}</span>
                  <span className="peNodeName">{n.name}</span>
                  <span className="peNodeType">{n.node_type}</span>
                </div>
                <div className="peNodeBtns">
                  <button title="上移" onClick={(e) => { e.stopPropagation(); move(i, -1); }}>↑</button>
                  <button title="下移" onClick={(e) => { e.stopPropagation(); move(i, 1); }}>↓</button>
                  <button className="danger" title="删除" onClick={(e) => { e.stopPropagation(); removeNode(i); }}>删</button>
                </div>
              </div>
              {i < nodes.length - 1 && <div className="peConnector">↓</div>}
            </div>
          ))}
        </div>

        {/* 属性面板 */}
        <div className="pePanel">
          {sel && selDef ? (
            <div className="peCard">
              <div className="peCardTitle">{sel.name}<span className="peNodeType">{sel.node_type}</span></div>
              <label className="agentField">
                <span className="agentFieldLabel">节点名称</span>
                <input type="text" value={sel.name}
                  onChange={(e) => updateNode(selected!, { name: e.target.value })} />
              </label>
              {selDef.config_schema.map((f) => (
                <label className="agentField" key={f.key}>
                  <span className="agentFieldLabel">{f.label}</span>
                  {f.type === "question_pool"
                    ? <select value={String(sel.config[f.key] ?? "")}
                        onChange={(e) => {
                          const v = e.target.value ? Number(e.target.value) : undefined;
                          updateNode(selected!,
                            { config: { ...sel.config, [f.key]: v, question_type: "" } });
                          if (v) ensureTypes(v);
                        }}>
                        <option value="">选择问题池</option>
                        {pools.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                      </select>
                    : f.type === "question_type"
                    ? (() => {
                        const poolId = Number(sel.config["pool_id"]) || 0;
                        const opts = typesByPool[poolId] ?? [];
                        if (poolId) ensureTypes(poolId);
                        return (
                          <select value={String(sel.config[f.key] ?? "")} disabled={!poolId}
                            onChange={(e) => updateNode(selected!,
                              { config: { ...sel.config, [f.key]: e.target.value } })}>
                            <option value="">{poolId ? "全部类型" : "请先选问题池"}</option>
                            {opts.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
                          </select>
                        );
                      })()
                    : f.type === "ai_engine"
                    ? <select value={String(sel.config[f.key] ?? "")}
                        onChange={(e) => updateNode(selected!,
                          { config: { ...sel.config, [f.key]: e.target.value || null } })}>
                        <option value="">系统默认</option>
                        {engines.map((en) => (
                          <option key={en.model} value={en.model}>{en.label || en.model}</option>
                        ))}
                      </select>
                    : f.type === "prompt_templates"
                    ? <select className="peMultiSelect" multiple
                        value={((sel.config[f.key] as number[] | undefined) ?? []).map(String)}
                        onChange={(e) => updateNode(selected!,
                          { config: { ...sel.config,
                            [f.key]: Array.from(e.target.selectedOptions, (o) => Number(o.value)) } })}>
                        {genTemplates.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
                      </select>
                    : f.type === "article_group"
                    ? <select value={String(sel.config[f.key] ?? "")}
                        onChange={(e) => updateNode(selected!,
                          { config: { ...sel.config,
                            [f.key]: e.target.value ? Number(e.target.value) : undefined } })}>
                        <option value="">选择分组</option>
                        {groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
                      </select>
                    : f.type === "accounts"
                    ? <select className="peMultiSelect" multiple
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
              ))}

              {/* 数据传递 */}
              <div className="peSection">
                <div className="peSectionTitle">数据传递</div>
                <label className="agentField">
                  <span className="agentFieldLabel">上游节点</span>
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
                <div className="agentFieldLabel" style={{ marginTop: 4 }}>字段映射</div>
                <div style={{ fontSize: 12, opacity: 0.6, marginBottom: 4 }}>
                  留空 = 自动透传上游全部字段；仅需改名/筛选时才添加映射
                </div>
                {(sel.flow_meta?.inputMapping ?? []).map((m, mi) => (
                  <div className="peMapRow" key={mi}>
                    <input type="text" placeholder="上游字段" value={m.from}
                      onChange={(e) => {
                        const im = [...(sel.flow_meta?.inputMapping ?? [])];
                        im[mi] = { ...im[mi], from: e.target.value };
                        updateNode(selected!, { flow_meta: { ...(sel.flow_meta ?? {}), inputMapping: im } });
                      }} />
                    <span className="peMapArrow">→</span>
                    <input type="text" placeholder="本节点字段" value={m.to}
                      onChange={(e) => {
                        const im = [...(sel.flow_meta?.inputMapping ?? [])];
                        im[mi] = { ...im[mi], to: e.target.value };
                        updateNode(selected!, { flow_meta: { ...(sel.flow_meta ?? {}), inputMapping: im } });
                      }} />
                    <button className="peMiniBtn danger" onClick={() => {
                      const im = (sel.flow_meta?.inputMapping ?? []).filter((_, x) => x !== mi);
                      updateNode(selected!, { flow_meta: { ...(sel.flow_meta ?? {}), inputMapping: im } });
                    }}>删</button>
                  </div>
                ))}
                <button className="peMiniBtn" onClick={() => {
                  const im = [...(sel.flow_meta?.inputMapping ?? []), { from: "", to: "" }];
                  updateNode(selected!, { flow_meta: { ...(sel.flow_meta ?? {}), inputMapping: im } });
                }}>+ 添加映射</button>
              </div>

              {/* 跳过条件 */}
              <div className="peSection">
                <div className="peSectionTitle">跳过条件（满足则不跳过；不满足则跳过本节点）</div>
                <div className="peMapRow">
                  <input type="text" placeholder="字段" value={sel.flow_meta?.condition?.field ?? ""}
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
                  <input type="text" placeholder="值" value={sel.flow_meta?.condition?.value ?? ""}
                    onChange={(e) => updateNode(selected!, { flow_meta: { ...(sel.flow_meta ?? {}),
                      condition: { field: sel.flow_meta?.condition?.field ?? "",
                        op: sel.flow_meta?.condition?.op ?? "eq", value: e.target.value } } })} />
                </div>
              </div>
            </div>
          ) : <div className="peEmpty">选择左侧一个节点以编辑其配置</div>}

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
