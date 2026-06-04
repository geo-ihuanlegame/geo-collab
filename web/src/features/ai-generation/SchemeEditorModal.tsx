import { useEffect, useMemo, useState } from "react";
import { Trash2 } from "lucide-react";
import {
  createScheme,
  listAiEngines,
  listPromptTemplates,
  listQuestionTypes,
  updateScheme,
} from "../../api/ai-generation";
import { useToast } from "../../components/Toast";
import type {
  AiEngine,
  PromptTemplate,
  QuestionBrief,
  QuestionPool,
  Scheme,
  SchemeLineInput,
} from "../../types";

type LineState = {
  question_type: string | null;
  questions: QuestionBrief[];
  checked: Set<number>;
  article_count: number;
  templateIds: Set<number>;
};

function questionLabel(q: QuestionBrief): string {
  return (q.question_text || q.record_id || "").trim();
}

export function SchemeEditorModal({
  scheme,
  pools,
  onClose,
  onSaved,
}: {
  scheme: Scheme | null;
  pools: QuestionPool[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const { toast } = useToast();
  const isEdit = scheme !== null;

  const [name, setName] = useState(scheme?.name ?? "");
  const [isEnabled, setIsEnabled] = useState(scheme?.is_enabled ?? true);
  const [aiEngine, setAiEngine] = useState(scheme?.ai_engine ?? "");
  const [poolId, setPoolId] = useState<number | "">(scheme?.pool_id ?? "");
  const [engines, setEngines] = useState<AiEngine[]>([]);
  const [templates, setTemplates] = useState<PromptTemplate[]>([]);
  const [lines, setLines] = useState<LineState[]>([]);
  const [loadingTypes, setLoadingTypes] = useState(false);
  const [saving, setSaving] = useState(false);

  const allTemplateIds = useMemo(() => templates.map((t) => t.id), [templates]);
  const templateName = (id: number) => templates.find((t) => t.id === id)?.name ?? `#${id}`;

  // 初始：引擎列表 + 启用的 generation 模板
  useEffect(() => {
    listAiEngines().then(setEngines).catch(() => setEngines([{ label: "默认写作模型", model: "" }]));
    listPromptTemplates("generation")
      .then((ts) => setTemplates(ts.filter((t) => t.is_enabled && !t.is_deleted)))
      .catch(() => {});
  }, []);

  // 选池（或编辑初始）→ 拉全部问题类型，构建行
  async function loadTypesForPool(pid: number) {
    setLoadingTypes(true);
    try {
      const [types] = await Promise.all([listQuestionTypes(pid)]);
      const tplDefault = templates.map((t) => t.id);
      setLines(
        types.map((t) => {
          const saved = scheme?.lines.find(
            (l) => (l.question_type ?? null) === (t.question_type ?? null),
          );
          const availableQ = new Set(t.questions.map((q) => q.id));
          if (saved) {
            const checked = new Set(
              saved.questions
                .map((q) => q.question_item_id)
                .filter((id): id is number => id !== null && availableQ.has(id)),
            );
            const tplIds = new Set(saved.allowed_prompt_template_ids);
            return {
              question_type: t.question_type,
              questions: t.questions,
              checked,
              article_count: saved.article_count,
              templateIds: tplIds,
            };
          }
          // 新建：全选；编辑遇到方案未含的类型：默认不选（视为此前已排除）
          return {
            question_type: t.question_type,
            questions: t.questions,
            checked: new Set(isEdit ? [] : t.questions.map((q) => q.id)),
            article_count: 1,
            templateIds: new Set(tplDefault),
          };
        }),
      );
    } catch (e) {
      toast(e instanceof Error ? e.message : "加载问题类型失败", "error");
      setLines([]);
    } finally {
      setLoadingTypes(false);
    }
  }

  // 编辑模式：挂载即加载方案池的问题类型；模板列表到位后再跑一次，补齐默认勾选的模板
  useEffect(() => {
    if (isEdit && scheme) {
      loadTypesForPool(scheme.pool_id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scheme, templates]);

  function onSelectPool(v: number | "") {
    setPoolId(v);
    setLines([]);
    if (v !== "") loadTypesForPool(v as number);
  }

  function patchLine(idx: number, patch: Partial<LineState>) {
    setLines((ls) => ls.map((l, i) => (i === idx ? { ...l, ...patch } : l)));
  }

  function toggleQuestion(idx: number, qid: number) {
    setLines((ls) =>
      ls.map((l, i) => {
        if (i !== idx) return l;
        const next = new Set(l.checked);
        if (next.has(qid)) next.delete(qid);
        else next.add(qid);
        return { ...l, checked: next };
      }),
    );
  }

  function toggleTemplate(idx: number, tid: number) {
    setLines((ls) =>
      ls.map((l, i) => {
        if (i !== idx) return l;
        const next = new Set(l.templateIds);
        if (next.has(tid)) next.delete(tid);
        else next.add(tid);
        return { ...l, templateIds: next };
      }),
    );
  }

  function toggleAll(idx: number) {
    setLines((ls) =>
      ls.map((l, i) => {
        if (i !== idx) return l;
        const all = l.checked.size === l.questions.length;
        return { ...l, checked: all ? new Set() : new Set(l.questions.map((q) => q.id)) };
      }),
    );
  }

  async function handleSave() {
    if (!name.trim()) {
      toast("请填写方案名称", "error");
      return;
    }
    if (poolId === "") {
      toast("请选择问题池", "error");
      return;
    }
    const included = lines.filter((l) => l.checked.size > 0);
    if (included.length === 0) {
      toast("请至少为一个问题类型勾选问题", "error");
      return;
    }
    for (const l of included) {
      if (l.templateIds.size === 0) {
        toast(`「${l.question_type || "未分类"}」至少要选一个允许模板`, "error");
        return;
      }
      if (l.article_count <= 0) {
        toast(`「${l.question_type || "未分类"}」文章数必须大于 0`, "error");
        return;
      }
    }
    const payloadLines: SchemeLineInput[] = included.map((l) => ({
      question_type: l.question_type,
      question_item_ids: [...l.checked],
      article_count: l.article_count,
      allowed_prompt_template_ids: [...l.templateIds],
    }));

    setSaving(true);
    try {
      if (isEdit && scheme) {
        await updateScheme(scheme.id, {
          name: name.trim(),
          is_enabled: isEnabled,
          ai_engine: aiEngine || null,
          lines: payloadLines,
        });
        toast("方案已更新", "success");
      } else {
        await createScheme({
          name: name.trim(),
          pool_id: poolId as number,
          is_enabled: isEnabled,
          ai_engine: aiEngine || null,
          lines: payloadLines,
        });
        toast("方案已创建", "success");
      }
      onSaved();
    } catch (e) {
      toast(e instanceof Error ? e.message : "保存失败", "error");
    } finally {
      setSaving(false);
    }
  }

  // 引擎下拉：若方案存的 model 不在列表里，补一个临时项
  const engineOptions = useMemo(() => {
    const opts = engines.length ? engines : [{ label: "默认写作模型", model: "" }];
    if (aiEngine && !opts.some((e) => e.model === aiEngine)) {
      return [...opts, { label: aiEngine, model: aiEngine }];
    }
    return opts;
  }, [engines, aiEngine]);

  return (
    <div className="modalBackdrop" role="dialog" aria-modal="true" onClick={onClose}>
      <div
        className="schemePanel"
        style={{ width: "min(1100px, calc(100vw - 48px))" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="schemePanelHead">
          <div>
            <h3>{isEdit ? "编辑方案" : "新建方案"}</h3>
            <p className="schemePanelHint">
              已从问题池载入全部问题类型，默认全选；取消勾选即可排除
            </p>
          </div>
          <button className="iconButton" type="button" onClick={onClose}>
            ×
          </button>
        </div>

        <div className="schemePanelBody">
          <div className="schemeFormRow">
            <label className="schemeField">
              <span className="schemeFieldLabel">方案名称</span>
              <input
                className="aiSelect"
                value={name}
                placeholder="给方案起个名字"
                onChange={(e) => setName(e.target.value)}
              />
            </label>
            <label className="schemeField">
              <span className="schemeFieldLabel">问题池</span>
              <select
                className="aiSelect"
                value={poolId}
                disabled={isEdit}
                onChange={(e) => onSelectPool(e.target.value ? Number(e.target.value) : "")}
              >
                <option value="">请选择问题池…</option>
                {pools.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="schemeField">
              <span className="schemeFieldLabel">AI 引擎</span>
              <select
                className="aiSelect"
                value={aiEngine}
                onChange={(e) => setAiEngine(e.target.value)}
              >
                {engineOptions.map((e) => (
                  <option key={e.model || "__default__"} value={e.model}>
                    {e.label}
                  </option>
                ))}
              </select>
            </label>
            <div className="schemeField" style={{ flex: "0 0 auto" }}>
              <span className="schemeFieldLabel">启用</span>
              <button
                type="button"
                className={`schemeToggle ${isEnabled ? "on" : "off"}`}
                style={{ height: 38, alignItems: "center" }}
                onClick={() => setIsEnabled((v) => !v)}
              >
                <span className="knob" />
              </button>
            </div>
          </div>

          <div className="schemeFieldLabel">
            {poolId === ""
              ? "选择问题池后载入问题类型"
              : `问题类型 · 共 ${lines.length} 类（默认全部纳入，取消勾选即可排除）`}
          </div>

          {loadingTypes ? (
            <div className="schemeEmpty">加载问题类型中…</div>
          ) : poolId === "" ? (
            <div className="schemeEmpty">请先在上方选择一个问题池</div>
          ) : lines.length === 0 ? (
            <div className="schemeEmpty">该问题池暂无有效问题，请先到问题池同步飞书</div>
          ) : (
            <div className="schemeLineScroll">
              {lines.map((l, idx) => {
                const allChecked = l.checked.size === l.questions.length && l.questions.length > 0;
                return (
                  <div
                    className="schemeLineCard"
                    key={l.question_type ?? "__none__"}
                    style={{ opacity: l.checked.size === 0 ? 0.6 : 1 }}
                  >
                    <div className="schemeLineHead">
                      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                        <span className="schemeTypeBadge">{l.question_type || "未分类"}</span>
                        <span style={{ fontSize: 12, color: "var(--fg-3)" }}>
                          共 {l.questions.length} 题
                        </span>
                      </div>
                      <div className="schemeLineActions">
                        <span style={{ color: "var(--fg-3)" }}>
                          已选 {l.checked.size} / {l.questions.length}
                        </span>
                        <button
                          type="button"
                          className="schemeLink"
                          onClick={() => toggleAll(idx)}
                        >
                          {allChecked ? "取消全选" : "全选"}
                        </button>
                        <button
                          type="button"
                          className="schemeLink"
                          style={{ color: "var(--fg-3)", display: "inline-flex", gap: 4, alignItems: "center" }}
                          onClick={() => patchLine(idx, { checked: new Set() })}
                          title="排除该问题类型（取消其全部勾选）"
                        >
                          <Trash2 size={12} />
                          移除
                        </button>
                      </div>
                    </div>

                    <div className="schemeLineSub">
                      <span className="schemeSubLabel">选择问题</span>
                      <div className="schemeChips">
                        {l.questions.map((q) => {
                          const on = l.checked.has(q.id);
                          return (
                            <button
                              key={q.id}
                              type="button"
                              className={`schemeChip${on ? " on" : ""}`}
                              title={questionLabel(q)}
                              onClick={() => toggleQuestion(idx, q.id)}
                            >
                              <span className="schemeChipText">{questionLabel(q)}</span>
                            </button>
                          );
                        })}
                      </div>
                    </div>

                    <div className="schemeLineSub" style={{ alignItems: "center" }}>
                      <span className="schemeSubLabel" style={{ paddingTop: 0 }}>
                        允许模板
                      </span>
                      <div className="schemeChips">
                        {allTemplateIds.length === 0 && (
                          <span style={{ fontSize: 12, color: "var(--fg-3)" }}>
                            暂无启用的 generation 模板，请先到「提示词管理」创建
                          </span>
                        )}
                        {templates.map((t) => {
                          const on = l.templateIds.has(t.id);
                          return (
                            <button
                              key={t.id}
                              type="button"
                              className={`schemeChip${on ? " on" : ""}`}
                              title={templateName(t.id)}
                              onClick={() => toggleTemplate(idx, t.id)}
                            >
                              <span className="schemeChipText">{t.name}</span>
                            </button>
                          );
                        })}
                      </div>
                      <span className="schemeSubLabel" style={{ paddingTop: 0 }}>
                        文章数
                      </span>
                      <input
                        className="aiSelect schemeNumBox"
                        type="number"
                        min={1}
                        max={50}
                        value={l.article_count}
                        onChange={(e) =>
                          patchLine(idx, {
                            article_count: Math.max(1, Math.min(50, Number(e.target.value) || 1)),
                          })
                        }
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div className="schemePanelFoot">
          <button className="secondaryButton" type="button" onClick={onClose}>
            取消
          </button>
          <button className="primaryButton" type="button" onClick={handleSave} disabled={saving}>
            {saving ? "保存中…" : isEdit ? "保存修改" : "保存方案"}
          </button>
        </div>
      </div>
    </div>
  );
}
