// web/src/features/pipelines/PipelineEditor.tsx
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Brain, Globe, Trash2 } from "lucide-react";
import { listAccounts } from "../../api/accounts";
import { listAiEngines, listFormatEngines, listQuestionPools, listQuestionTypes } from "../../api/ai-generation";
import { listArticleGroups } from "../../api/articles";
import { listCategories } from "../../api/image-library";
import {
  discardDraft, getNodeTypes, getPipeline, getRun, publishPipeline, saveDraft, startRun,
} from "../../api/pipelines";
import { listPromptTemplates } from "../../api/prompt-templates";
import { useToast } from "../../components/Toast";
import type {
  Account, AiEngine, ArticleGroup, NodeTypeDef, Pipeline, PipelineNodeDef,
  PromptTemplate, QuestionPool, QuestionType, StockCategory,
} from "../../types";
import { AccountSelector } from "./AccountSelector";
import { VersionHistory } from "./VersionHistory";

// 问题源「类型卡 + 问题 chip」选择器（交互对齐 AI 生文的方案编辑）。
// 受控组件：每个问题类型 = 一个 PickerUnit，状态从节点 config.units 派生、改动写回 config.units。
// 每单元 record_ids: null=整类(自动跟进新同步问题) / 非空数组=精选 / []=未选(不入 units)；
// 另带 allowed_prompt_template_ids（允许模板）与 article_count（文章数）。兼容旧扁平 config（首次改动迁移为 units）。
const UNCATEGORIZED = "__uncategorized__";

function typeSentinel(t: QuestionType): string {
  return t.question_type ?? UNCATEGORIZED;
}

// 问题源 unit 是否已自带模板 / 数量（既用于 AI生文「接管/兜底」覆盖度，也用于类型卡上的标记）。
// 接受 PickerUnit 或 raw config unit（键名与后端一致）。
type FieldCoverage = { state: "full" | "partial" | "none"; have: number; total: number };
type UnitLike = { allowed_prompt_template_ids?: unknown; article_count?: unknown };
function unitHasTemplate(u: UnitLike): boolean {
  return Array.isArray(u.allowed_prompt_template_ids) && u.allowed_prompt_template_ids.length > 0;
}
function unitHasCount(u: UnitLike): boolean {
  return typeof u.article_count === "number" && u.article_count > 0;
}

// config → 已勾选 record_id 集合。优先级与后端 question_source 一致：record_ids > types > 整池。
function deriveCheckedRecordIds(types: QuestionType[], config: Record<string, unknown>): Set<string> {
  const recordIds = (config.question_record_ids as string[] | undefined) ?? [];
  let qtypes = config.question_types as string[] | undefined;
  if (qtypes === undefined) {
    const legacy = config.question_type as string | null | undefined; // 兼容旧单选
    qtypes = legacy == null || legacy === "" ? [] : [legacy];
  }
  const all = types.flatMap((t) => t.questions.map((q) => q.record_id));
  if (recordIds.length > 0) {
    const set = new Set(recordIds);
    return new Set(all.filter((rid) => set.has(rid)));
  }
  if (qtypes.length > 0) {
    const set = new Set(qtypes);
    const out = new Set<string>();
    for (const t of types) {
      if (set.has(typeSentinel(t))) t.questions.forEach((q) => out.add(q.record_id));
    }
    return out;
  }
  return new Set(all);
}

type PickerUnit = {
  question_type: string;
  record_ids: string[] | null;          // null = 整类（自动跟进）
  allowed_prompt_template_ids: number[];
  article_count: number | null;
};

// config → 每个类型的 PickerUnit（用于渲染）。优先读新 units；否则从旧扁平 config 推导。
function deriveUnitMap(types: QuestionType[], config: Record<string, unknown>): Map<string, PickerUnit> {
  const map = new Map<string, PickerUnit>();
  const rawUnits = config.units as PickerUnit[] | undefined;
  if (Array.isArray(rawUnits)) {
    for (const t of types) {
      const sent = typeSentinel(t);
      const u = rawUnits.find((x) => x.question_type === sent);
      map.set(sent, u
        ? { question_type: sent,
            record_ids: u.record_ids === null ? null : [...(u.record_ids ?? [])],
            allowed_prompt_template_ids: [...(u.allowed_prompt_template_ids ?? [])],
            article_count: u.article_count ?? null }
        : { question_type: sent, record_ids: [], allowed_prompt_template_ids: [], article_count: null });
    }
    return map;
  }
  // 旧扁平 config → 每类型 record_ids（整类=null，部分=子集，无=[]），无模板/数量
  const checked = deriveCheckedRecordIds(types, config);
  for (const t of types) {
    const sent = typeSentinel(t);
    const rids = t.questions.map((q) => q.record_id);
    const on = rids.filter((r) => checked.has(r));
    const record_ids = on.length === 0 || rids.length === 0 ? [] : on.length === rids.length ? null : on;
    map.set(sent, { question_type: sent, record_ids, allowed_prompt_template_ids: [], article_count: null });
  }
  return map;
}

// PickerUnit map → config.units（只保留「有勾选问题」的类型：record_ids===null 或非空数组）。
function unitsToConfig(map: Map<string, PickerUnit>): Record<string, unknown> {
  const units: PickerUnit[] = [];
  for (const u of map.values()) {
    const included = u.record_ids === null || (Array.isArray(u.record_ids) && u.record_ids.length > 0);
    if (included) units.push(u);
  }
  // 清掉旧扁平字段，统一走 units
  return { units, question_type: undefined, question_types: undefined, question_record_ids: undefined };
}

function QuestionTypePicker({ poolId, types, config, templates, onChange }: {
  poolId: number;
  types: QuestionType[] | undefined;
  config: Record<string, unknown>;
  templates: PromptTemplate[];
  onChange: (patch: Record<string, unknown>) => void;
}) {
  if (!poolId) return <div className="schemeEmpty">请先在上方选择问题池</div>;
  if (types === undefined) return <div className="schemeEmpty">加载问题类型中…</div>;
  if (types.length === 0) {
    return <div className="schemeEmpty">该问题池暂无问题，请先到「AI 生文 · 问题池」同步飞书</div>;
  }

  const unitMap = deriveUnitMap(types, config);
  const commit = (next: Map<string, PickerUnit>) => onChange(unitsToConfig(next));
  const cloneMap = () => new Map([...unitMap].map(([k, v]) => [k, { ...v,
    record_ids: v.record_ids === null ? null : [...v.record_ids],
    allowed_prompt_template_ids: [...v.allowed_prompt_template_ids] }]));

  const checkedSet = (t: QuestionType): Set<string> => {
    const u = unitMap.get(typeSentinel(t))!;
    if (u.record_ids === null) return new Set(t.questions.map((q) => q.record_id)); // 整类=全选
    return new Set(u.record_ids);
  };

  const toggleQuestion = (t: QuestionType, rid: string) => {
    const sent = typeSentinel(t);
    const cur = checkedSet(t);
    if (cur.has(rid)) cur.delete(rid); else cur.add(rid);
    const all = t.questions.map((q) => q.record_id);
    const next = cloneMap();
    const u = next.get(sent)!;
    u.record_ids = cur.size === all.length ? null : [...cur];   // 全选→null(自动跟进)
    commit(next);
  };
  const toggleAll = (t: QuestionType) => {
    const sent = typeSentinel(t);
    const u0 = unitMap.get(sent)!;
    const allOn = u0.record_ids === null;
    const next = cloneMap();
    next.get(sent)!.record_ids = allOn ? [] : null;
    commit(next);
  };
  const removeType = (t: QuestionType) => {          // 排除该类型（清空其勾选）
    const next = cloneMap();
    next.get(typeSentinel(t))!.record_ids = [];
    commit(next);
  };
  const setTemplates = (t: QuestionType, ids: number[]) => {
    const next = cloneMap();
    next.get(typeSentinel(t))!.allowed_prompt_template_ids = ids;
    commit(next);
  };
  const setCount = (t: QuestionType, n: number | null) => {
    const next = cloneMap();
    next.get(typeSentinel(t))!.article_count = n;
    commit(next);
  };

  return (
    <>
      <div className="schemeFieldLabel">
        问题类型 · 共 {types.length} 类（勾选问题=启用该类型；可各自配模板/数量，留空则用 AI 生文兜底）
      </div>
      <div className="schemeLineScroll">
        {types.map((t) => {
          const u = unitMap.get(typeSentinel(t))!;
          const checked = checkedSet(t);
          const checkedCount = t.questions.filter((q) => checked.has(q.record_id)).length;
          const allChecked = checkedCount === t.questions.length && t.questions.length > 0;
          return (
            <div className="schemeLineCard" key={typeSentinel(t)}
              style={{ opacity: checkedCount === 0 ? 0.6 : 1 }}>
              <div className="schemeLineHead">
                <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <span className="schemeTypeBadge">{t.question_type ?? "未分类"}</span>
                  <span style={{ fontSize: 12, color: "var(--fg-3)" }}>共 {t.questions.length} 题</span>
                  {checkedCount > 0 && (
                    unitHasTemplate(u) && unitHasCount(u)
                      ? <span style={{ fontSize: 11, padding: "1px 6px", borderRadius: 4,
                          background: "var(--green-soft)", color: "var(--green)" }}
                          title="该类型已自带模板+文章数，AI 生文将直接用它">已接管</span>
                      : <span style={{ fontSize: 11, padding: "1px 6px", borderRadius: 4,
                          background: "var(--bg-2, #f1f1f4)", color: "var(--fg-3)" }}
                          title="该类型未配模板或文章数，缺的部分将回退到 AI 生文节点的兜底">将用兜底</span>
                  )}
                </div>
                <div className="schemeLineActions">
                  <span style={{ color: "var(--fg-3)" }}>已选 {checkedCount} / {t.questions.length}</span>
                  <button type="button" className="schemeLink" onClick={() => toggleAll(t)}>
                    {allChecked ? "取消全选" : "全选"}
                  </button>
                  <button type="button" className="schemeLink"
                    style={{ color: "var(--fg-3)", display: "inline-flex", gap: 4, alignItems: "center" }}
                    onClick={() => removeType(t)} title="排除该问题类型（取消其全部勾选）">
                    <Trash2 size={12} /> 移除
                  </button>
                </div>
              </div>
              <div className="schemeLineSub">
                <span className="schemeSubLabel">选择问题</span>
                <div className="schemeChips">
                  {t.questions.map((q) => {
                    const on = checked.has(q.record_id);
                    const label = (q.question_text || q.record_id || "").trim();
                    return (
                      <button key={q.record_id} type="button"
                        className={`schemeChip${on ? " on" : ""}`} title={label}
                        onClick={() => toggleQuestion(t, q.record_id)}>
                        <span className="schemeChipText">{label}</span>
                      </button>
                    );
                  })}
                </div>
              </div>
              <div className="schemeLineSub" style={{ alignItems: "center" }}>
                <span className="schemeSubLabel" style={{ paddingTop: 0 }}>允许模板</span>
                <div className="schemeChips">
                  {templates.length === 0 && (
                    <span style={{ fontSize: 12, color: "var(--fg-3)" }}>暂无启用的模板</span>
                  )}
                  {templates.map((tp) => {
                    const on = u.allowed_prompt_template_ids.includes(tp.id);
                    return (
                      <button key={tp.id} type="button"
                        className={`schemeChip${on ? " on" : ""}`}
                        title={tp.name}
                        onClick={() => {
                          const next = [...u.allowed_prompt_template_ids];
                          const idx = next.indexOf(tp.id);
                          if (idx >= 0) next.splice(idx, 1);
                          else next.push(tp.id);
                          setTemplates(t, next);
                        }}>
                        <span className="schemeChipText">{tp.name}</span>
                      </button>
                    );
                  })}
                </div>
                <span className="schemeSubLabel" style={{ paddingTop: 0 }}>文章数</span>
                <input className="aiSelect schemeNumBox" type="number" min={1} max={50}
                  value={u.article_count ?? ""}
                  onChange={(e) =>
                    setCount(t, e.target.value ? Math.max(1, Math.min(50, Number(e.target.value) || 1)) : null)} />
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}

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
  const [formatEngines, setFormatEngines] = useState<AiEngine[]>([]);
  const [genTemplates, setGenTemplates] = useState<PromptTemplate[]>([]);
  const [formatTemplates, setFormatTemplates] = useState<PromptTemplate[]>([]);
  const [mainCategories, setMainCategories] = useState<StockCategory[]>([]);
  // 每个池缓存完整问题类型(含各类问题)，供"类型多选"与"具体问题多选"联动。
  const [typesByPool, setTypesByPool] = useState<Record<number, QuestionType[]>>({});
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
    listFormatEngines().then(setFormatEngines).catch(() => {});
    listPromptTemplates("generation")
      .then((ts) => setGenTemplates(ts.filter((t) => t.scope === "generation" && t.is_enabled)))
      .catch(() => {});
    listPromptTemplates("ai_format")
      .then((ts) => setFormatTemplates(ts.filter((t) => t.scope === "ai_format" && t.is_enabled)))
      .catch(() => {});
    listCategories("main").then(setMainCategories).catch(() => {});
  }, []);

  // Lazily load a pool's question types (cascade for question_types/question_records fields).
  const ensureTypes = useCallback((poolId: number) => {
    if (poolId && typesByPool[poolId] === undefined) {
      listQuestionTypes(poolId)
        .then((ts) => setTypesByPool((m) => ({ ...m, [poolId]: ts })))
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

  // AI生文 / AI创作：统计上游问题源对「模板/数量」的覆盖度（两节点共用逐单元接管语义）。
  // full=每个启用类型都配了→字段灰显(已接管)；partial=只配了一部分→字段仍可编辑(未配类型用本节点兜底)；
  // none=都没配/无上游问题源→纯兜底。三态修掉了「部分配置时永不灰显，看着像屏蔽没生效」的困惑。
  const aiGenMask = useMemo<{ template: FieldCoverage; count: FieldCoverage }>(() => {
    const none: FieldCoverage = { state: "none", have: 0, total: 0 };
    const blank = { template: none, count: none };
    if (!sel || (sel.node_type !== "ai_generate" && sel.node_type !== "ai_compose")) return blank;
    // dependsOnIndex 存的是 node_index（见数据传递选择器），按 node_index 查；留空则取数组里前一个。
    const dep = sel.flow_meta?.dependsOnIndex;
    const up = dep != null
      ? nodes.find((n) => n.node_index === dep)
      : (selected != null && selected > 0 ? nodes[selected - 1] : undefined);
    if (!up || up.node_type !== "question_source") return blank;
    const units = up.config?.units as Array<Record<string, unknown>> | undefined;
    if (!Array.isArray(units) || units.length === 0) return blank;
    const enabled = units.filter((u) => u.record_ids === null ||
      (Array.isArray(u.record_ids) && (u.record_ids as unknown[]).length > 0));
    const total = enabled.length;
    if (total === 0) return blank;
    const cover = (ok: (u: Record<string, unknown>) => boolean): FieldCoverage => {
      const have = enabled.filter(ok).length;
      const state: FieldCoverage["state"] = have === 0 ? "none" : have === total ? "full" : "partial";
      return { state, have, total };
    };
    return {
      template: cover((u) => unitHasTemplate(u)),
      count: cover((u) => unitHasCount(u)),
    };
  }, [sel, selected, nodes]);

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
        {/* ai_generate(AI生文) 已下线、能力并入 ai_compose(AI创作)：隐藏出新；保留在 nodeTypes 里，
            存量 ai_generate 节点仍能渲染属性面板、继续运行。 */}
        {nodeTypes.filter((t) => t.type !== "ai_generate").map((t) => (
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
              {selDef.config_schema.map((f) => {
                // ai_compose 的「模型能力」：联网搜索 + 深度思考合并为同一行并排展示。
                // 拦在 web_search 处一次性渲染两者；deep_thinking 自身跳过（已随上者渲染）。
                if (sel.node_type === "ai_compose" && (f.key === "web_search" || f.key === "deep_thinking")) {
                  if (f.key === "deep_thinking") return null;
                  const dtField = selDef.config_schema.find((x) => x.key === "deep_thinking");
                  const webOn = "web_search" in sel.config ? !!sel.config["web_search"] : !!f.default;
                  const dtOn =
                    "deep_thinking" in sel.config ? !!sel.config["deep_thinking"] : !!dtField?.default;
                  return (
                    <div className="agentField" key="model-capabilities">
                      <span className="agentFieldLabel">模型能力</span>
                      <div className="peCapabilityRow">
                        <div className="peCapabilityItem">
                          <span className="peCapabilityLabel"><Globe size={15} />联网搜索</span>
                          <button
                            type="button"
                            role="switch"
                            aria-checked={webOn}
                            className={`peToggle${webOn ? " on" : ""}`}
                            onClick={() =>
                              updateNode(selected!, { config: { ...sel.config, web_search: !webOn } })}
                          >
                            <span className="peToggleKnob" />
                          </button>
                        </div>
                        <div className="peCapabilityItem">
                          <span className="peCapabilityLabel"><Brain size={15} />深度思考</span>
                          <button
                            type="button"
                            role="switch"
                            aria-checked={dtOn}
                            className={`peToggle${dtOn ? " on" : ""}`}
                            onClick={() =>
                              updateNode(selected!, { config: { ...sel.config, deep_thinking: !dtOn } })}
                          >
                            <span className="peToggleKnob" />
                          </button>
                        </div>
                      </div>
                    </div>
                  );
                }
                // 开关：存布尔配置（如「联网兜底」），样式为 toggle
                if (f.type === "toggle") {
                  const on = f.key in sel.config ? !!sel.config[f.key] : !!f.default;
                  return (
                    <div className="agentField" key={f.key}>
                      <span className="agentFieldLabel">{f.label}</span>
                      <div className="peToggleRow">
                        <span className="peToggleText">{f.hint}</span>
                        <button
                          type="button"
                          role="switch"
                          aria-checked={on}
                          className={`peToggle${on ? " on" : ""}`}
                          onClick={() =>
                            updateNode(selected!, { config: { ...sel.config, [f.key]: !on } })}
                        >
                          <span className="peToggleKnob" />
                        </button>
                      </div>
                    </div>
                  );
                }
                // 问题源：用「类型卡 + 问题 chip」选择器替代原生多选；它一处同时写
                // question_types + question_record_ids，故 question_records 字段交给它、不再单独渲染。
                if (f.type === "question_types") {
                  const poolId = Number(sel.config["pool_id"]) || 0;
                  if (poolId) ensureTypes(poolId);
                  return (
                    <div className="agentField" key={f.key}>
                      <QuestionTypePicker
                        poolId={poolId}
                        types={poolId ? typesByPool[poolId] : []}
                        config={sel.config}
                        templates={genTemplates}
                        onChange={(patch) =>
                          updateNode(selected!, { config: { ...sel.config, ...patch } })}
                      />
                    </div>
                  );
                }
                if (f.type === "question_records") return null;
                // 内容分发：账号选择器（平台动态规则 + 账号级启用开关），替代原生多选。
                if (f.type === "account_selector") {
                  return (
                    <div className="agentField" key={f.key}>
                      <AccountSelector
                        accounts={accounts}
                        config={sel.config}
                        onChange={(patch) =>
                          updateNode(selected!, { config: { ...sel.config, ...patch } })}
                      />
                    </div>
                  );
                }
                // AI生文/AI创作：数量(及AI生文的单模板)字段——上游问题源覆盖度三态。
                // full→灰显禁用「已接管」；partial→可编辑「部分接管，未配类型用此兜底」；none→「兜底」。
                // ai_compose 的模板是多选(prompt_templates)、另行渲染，这里只接管其 count。
                if (
                  (sel.node_type === "ai_generate" &&
                    (f.key === "prompt_template_id" || f.key === "count")) ||
                  (sel.node_type === "ai_compose" && f.key === "count")
                ) {
                  const cov = f.key === "prompt_template_id" ? aiGenMask.template : aiGenMask.count;
                  const hint = cov.state === "full"
                    ? "（已由上游问题源接管）"
                    : cov.state === "partial"
                    ? `（部分类型已接管，未配类型用此兜底：已配 ${cov.have}/${cov.total} 类）`
                    : "（上游未配时的兜底）";
                  return (
                    <label className="agentField" key={f.key}>
                      <span className="agentFieldLabel">{f.label}{hint}</span>
                      <input
                        type={f.key === "count" ? "number" : "text"}
                        disabled={cov.state === "full"}
                        value={String(sel.config[f.key] ?? "")}
                        onChange={(e) => updateNode(selected!, { config: { ...sel.config,
                          [f.key]: f.key === "count" ? Number(e.target.value) : e.target.value } })} />
                    </label>
                  );
                }
                return (
                <label className="agentField" key={f.key}>
                  <span className="agentFieldLabel">{f.label}</span>
                  {f.type === "question_pool"
                    ? <select value={String(sel.config[f.key] ?? "")}
                        onChange={(e) => {
                          const v = e.target.value ? Number(e.target.value) : undefined;
                          updateNode(selected!,
                            { config: { ...sel.config, [f.key]: v, units: undefined,
                              question_types: undefined, question_record_ids: undefined } });
                          if (v) ensureTypes(v);
                        }}>
                        <option value="">选择问题池</option>
                        {pools.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                      </select>
                    : f.type === "ai_engine"
                    ? <select value={String(sel.config[f.key] ?? "")}
                        onChange={(e) => updateNode(selected!,
                          { config: { ...sel.config, [f.key]: e.target.value || null } })}>
                        <option value="">系统默认</option>
                        {engines.map((en) => (
                          <option key={en.model} value={en.model}>{en.label || en.model}</option>
                        ))}
                      </select>
                    : f.type === "format_engine"
                    ? <select value={String(sel.config[f.key] ?? "")}
                        onChange={(e) => updateNode(selected!,
                          { config: { ...sel.config, [f.key]: e.target.value || null } })}>
                        <option value="">系统默认</option>
                        {formatEngines.map((en) => (
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
                    : f.type === "ai_format_template"
                    ? <select value={String(sel.config[f.key] ?? "")}
                        onChange={(e) => updateNode(selected!,
                          { config: { ...sel.config,
                            [f.key]: e.target.value ? Number(e.target.value) : undefined } })}>
                        <option value="">内置默认</option>
                        {formatTemplates.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
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
                    : f.type === "stock_category_main"
                    ? <select value={String(sel.config[f.key] ?? "")}
                        onChange={(e) => updateNode(selected!,
                          { config: { ...sel.config,
                            [f.key]: e.target.value ? Number(e.target.value) : undefined } })}>
                        <option value="">选择主推游戏</option>
                        {mainCategories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                      </select>
                    : f.type === "checkbox"
                    ? <input type="checkbox"
                        checked={f.key in sel.config ? !!sel.config[f.key] : !!f.default}
                        onChange={(e) => updateNode(selected!,
                          { config: { ...sel.config, [f.key]: e.target.checked } })} />
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
                );
              })}

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
