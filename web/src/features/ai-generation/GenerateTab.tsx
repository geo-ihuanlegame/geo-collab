import { useEffect, useMemo, useRef, useState } from "react";
import { Sparkles, RefreshCw, Plus, ExternalLink } from "lucide-react";
import {
  listSkills,
  listPromptTemplates,
  startGeneration,
  getGenerationSession,
  listQuestionPools,
  createQuestionPool,
  syncQuestionPool,
  listQuestionItems,
} from "../../api/ai-generation";
import { getArticle } from "../../api/articles";
import { useToast } from "../../components/Toast";
import type {
  Article,
  GenerationSession,
  PromptTemplate,
  QuestionItem,
  QuestionPool,
  Skill,
} from "../../types";

// 同步层若没抽到 question_text 时的兜底：把 fields 拍平展示
function segText(seg: unknown): string {
  if (typeof seg === "string") return seg;
  if (seg && typeof seg === "object") {
    const o = seg as Record<string, unknown>;
    return String(o.text ?? o.name ?? o.link ?? "");
  }
  return seg == null ? "" : String(seg);
}
function fieldsToText(fields: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [k, v] of Object.entries(fields || {})) {
    const s = (Array.isArray(v) ? v.map(segText).join("") : segText(v)).trim();
    if (s) parts.push(`${k}：${s}`);
  }
  return parts.join("   /   ");
}
function itemQuestion(it: QuestionItem): string {
  return (it.question_text || fieldsToText(it.fields) || it.record_id).trim();
}

export function GenerateTab({ onNavigateToContent }: { onNavigateToContent: () => void }) {
  const { toast } = useToast();

  const [pools, setPools] = useState<QuestionPool[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [prompts, setPrompts] = useState<PromptTemplate[]>([]);
  const [selectedPoolId, setSelectedPoolId] = useState<number | "">("");
  const [items, setItems] = useState<QuestionItem[]>([]);
  const [checked, setChecked] = useState<Set<number>>(new Set());
  const [selectedSkillId, setSelectedSkillId] = useState<number | "">("");
  const [selectedPromptId, setSelectedPromptId] = useState<number | "">("");
  const [autoN, setAutoN] = useState<number>(5);

  const [syncing, setSyncing] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newPool, setNewPool] = useState({ name: "", feishu_app_token: "", feishu_table_id: "" });

  const [isGenerating, setIsGenerating] = useState(false);
  const [hasResult, setHasResult] = useState(false);
  const [session, setSession] = useState<GenerationSession | null>(null);
  const [generatedArticles, setGeneratedArticles] = useState<Article[]>([]);
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    listQuestionPools().then(setPools).catch(() => {});
    listSkills().then((s) => setSkills(s.filter((x) => x.is_enabled))).catch(() => {});
    listPromptTemplates("generation").then((p) => setPrompts(p.filter((x) => x.is_enabled))).catch(() => {});
  }, []);

  function stopPolling() {
    if (pollTimerRef.current) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }
  useEffect(() => () => stopPolling(), []);

  async function refreshItems(poolId: number) {
    const its = await listQuestionItems(poolId, "pending");
    setItems(its);
    setChecked(new Set());
  }

  function onSelectPool(id: number | "") {
    setSelectedPoolId(id);
    setItems([]);
    setChecked(new Set());
    if (id !== "") refreshItems(id as number).catch(() => {});
  }

  async function handleCreatePool() {
    if (!newPool.name.trim()) {
      toast("请填写问题池名称", "error");
      return;
    }
    try {
      const pool = await createQuestionPool({
        name: newPool.name.trim(),
        feishu_app_token: newPool.feishu_app_token.trim() || undefined,
        feishu_table_id: newPool.feishu_table_id.trim() || undefined,
      });
      setPools((ps) => [pool, ...ps]);
      setCreating(false);
      setNewPool({ name: "", feishu_app_token: "", feishu_table_id: "" });
      onSelectPool(pool.id);
      toast("已创建问题池", "success");
    } catch (e) {
      toast(e instanceof Error ? e.message : "创建失败", "error");
    }
  }

  async function handleSync() {
    if (selectedPoolId === "") return;
    setSyncing(true);
    try {
      const r = await syncQuestionPool(selectedPoolId as number);
      toast(`同步：新增 ${r.added}、更新 ${r.updated}、已消费跳过 ${r.skipped_consumed}`, "success");
      await refreshItems(selectedPoolId as number);
      listQuestionPools().then(setPools).catch(() => {});
    } catch (e) {
      toast(e instanceof Error ? e.message : "同步失败", "error");
    } finally {
      setSyncing(false);
    }
  }

  function toggle(id: number) {
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }
  function toggleAll() {
    setChecked((prev) => (prev.size === items.length ? new Set() : new Set(items.map((i) => i.id))));
  }

  // 勾选时按 category 分组 → 篇数 = 板块数（与后端一致）
  const checkedCategoryCount = useMemo(() => {
    const cats = new Set<string>();
    for (const it of items) {
      if (checked.has(it.id)) cats.add(it.category ?? "__none__");
    }
    return cats.size;
  }, [checked, items]);

  const expectedArticleCount = checked.size > 0 ? checkedCategoryCount : autoN;
  const mode: "manual" | "auto" = checked.size > 0 ? "manual" : "auto";

  async function handleGenerate() {
    if (!selectedSkillId || !selectedPromptId) {
      toast("请选择 Skill 和提示词", "error");
      return;
    }
    if (selectedPoolId === "") {
      toast("请选择问题池", "error");
      return;
    }
    if (mode === "auto" && (!autoN || autoN <= 0)) {
      toast("自动模式请填写要生成的篇数", "error");
      return;
    }

    setIsGenerating(true);
    setHasResult(false);
    setGeneratedArticles([]);
    setSession(null);

    const payload: Parameters<typeof startGeneration>[0] = {
      skill_id: selectedSkillId as number,
      prompt_template_id: selectedPromptId as number,
      pool_id: selectedPoolId as number,
    };
    if (mode === "manual") payload.question_item_ids = Array.from(checked);
    else payload.auto_count = autoN;

    try {
      const { session_id } = await startGeneration(payload);
      pollTimerRef.current = setInterval(async () => {
        try {
          const s = await getGenerationSession(session_id);
          setSession(s);
          if (s.status === "done" || s.status === "failed") {
            stopPolling();
            setIsGenerating(false);
            setHasResult(true);
            if (s.status === "done") {
              const settled = await Promise.allSettled(s.article_ids.map((id) => getArticle(id)));
              setGeneratedArticles(settled.flatMap((r) => (r.status === "fulfilled" ? [r.value] : [])));
            } else {
              toast(s.error_message || "生成失败，请重试", "error");
            }
            // 已在 handleGenerate 入口检查 selectedPoolId 非空，此处可直接用
            refreshItems(selectedPoolId as number).catch(() => {});
            listQuestionPools().then(setPools).catch(() => {});
          }
        } catch {
          // 网络抖动，继续轮询
        }
      }, 2000);
    } catch (e) {
      setIsGenerating(false);
      toast(e instanceof Error ? e.message : "启动生成失败", "error");
    }
  }

  return (
    <div className="aiGenerateLayout">
      {/* Left: pool + items + skill/prompt + N + generate */}
      <div className="aiConfigPanel">
        <div className="aiFormGroup">
          <label className="aiFormLabel">问题池</label>
          <div style={{ display: "flex", gap: 8 }}>
            <select
              className="aiSelect"
              style={{ flex: 1 }}
              value={selectedPoolId}
              onChange={(e) => onSelectPool(e.target.value ? Number(e.target.value) : "")}
              disabled={isGenerating}
            >
              <option value="">请选择问题池…</option>
              {pools.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}（待生成 {p.pending_count}）
                </option>
              ))}
            </select>
            <button className="secondaryButton" type="button" title="新建问题池" onClick={() => setCreating((v) => !v)}>
              <Plus size={14} />
            </button>
            <button
              className="secondaryButton"
              type="button"
              title="从飞书多维表同步"
              onClick={handleSync}
              disabled={selectedPoolId === "" || syncing}
            >
              <RefreshCw size={14} />
            </button>
          </div>
        </div>

        {creating && (
          <div className="aiFormGroup" style={{ background: "var(--cream-2)", padding: 12, borderRadius: 8 }}>
            <input
              className="aiSelect"
              style={{ marginBottom: 6 }}
              placeholder="池名称（必填）"
              value={newPool.name}
              onChange={(e) => setNewPool({ ...newPool, name: e.target.value })}
            />
            <input
              className="aiSelect"
              style={{ marginBottom: 6 }}
              placeholder="飞书多维表 app_token（可选）"
              value={newPool.feishu_app_token}
              onChange={(e) => setNewPool({ ...newPool, feishu_app_token: e.target.value })}
            />
            <input
              className="aiSelect"
              style={{ marginBottom: 6 }}
              placeholder="飞书表 table_id（可选）"
              value={newPool.feishu_table_id}
              onChange={(e) => setNewPool({ ...newPool, feishu_table_id: e.target.value })}
            />
            <button className="aiGenerateBtn" type="button" onClick={handleCreatePool}>
              创建
            </button>
          </div>
        )}

        {selectedPoolId !== "" && (
          <div className="aiFormGroup">
            <label className="aiFormLabel" style={{ display: "flex", justifyContent: "space-between" }}>
              <span>待生成问题（按板块分组合并 = 一篇）</span>
              {items.length > 0 && (
                <button
                  type="button"
                  className="aiOpenLink"
                  onClick={toggleAll}
                  style={{ border: "none", background: "none", cursor: "pointer" }}
                >
                  {checked.size === items.length ? "取消全选" : "全选"}
                </button>
              )}
            </label>
            <div style={{ maxHeight: 280, overflowY: "auto", border: "1px solid var(--border)", borderRadius: 8 }}>
              {items.length === 0 ? (
                <p style={{ padding: 12, color: "var(--muted)", fontSize: 13, margin: 0 }}>
                  暂无待生成问题。绑定飞书表后点右上「同步」拉取。
                </p>
              ) : (
                items.map((it) => (
                  <label
                    key={it.id}
                    style={{
                      display: "flex",
                      gap: 8,
                      padding: "8px 10px",
                      borderBottom: "1px solid var(--border)",
                      cursor: isGenerating ? "default" : "pointer",
                      alignItems: "flex-start",
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={checked.has(it.id)}
                      onChange={() => toggle(it.id)}
                      disabled={isGenerating}
                      style={{ marginTop: 3 }}
                    />
                    <span
                      style={{
                        fontSize: 11,
                        padding: "2px 6px",
                        borderRadius: 4,
                        background: "var(--cream-2)",
                        color: "var(--muted)",
                        whiteSpace: "nowrap",
                        marginTop: 1,
                      }}
                    >
                      {it.category || "未分类"}
                    </span>
                    <span style={{ fontSize: 13, lineHeight: 1.5, flex: 1 }}>{itemQuestion(it)}</span>
                  </label>
                ))
              )}
            </div>
          </div>
        )}

        <div className="aiFormGroup">
          <label className="aiFormLabel">Skill</label>
          <select
            className="aiSelect"
            value={selectedSkillId}
            onChange={(e) => setSelectedSkillId(e.target.value ? Number(e.target.value) : "")}
            disabled={isGenerating}
          >
            <option value="">请选择技能…</option>
            {skills.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </div>

        <div className="aiFormGroup">
          <label className="aiFormLabel">提示词（用 {"{{问题}}"} 占位接收问题）</label>
          <select
            className="aiSelect"
            value={selectedPromptId}
            onChange={(e) => setSelectedPromptId(e.target.value ? Number(e.target.value) : "")}
            disabled={isGenerating}
          >
            <option value="">请选择提示词…</option>
            {prompts.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </div>

        <div className="aiFormGroup">
          <label className="aiFormLabel">
            生成篇数 N{checked.size > 0 ? "（已勾选 → 忽略，按板块数定）" : "（未勾选 → 自动选题）"}
          </label>
          <input
            className="aiSelect"
            type="number"
            min={1}
            max={20}
            value={autoN}
            onChange={(e) => setAutoN(Math.max(1, Math.min(20, Number(e.target.value) || 1)))}
            disabled={isGenerating || checked.size > 0}
          />
        </div>

        {/* 过渡期 stopgap：问题池直连生成已硬切下线，迁移到「方案池 / 方案运行」。
            新界面上线前禁用生成按钮并提示，避免触发已下线接口。问题池同步/列表、Skill/提示词管理不受影响。 */}
        <div
          style={{
            margin: "8px 0",
            padding: "8px 10px",
            borderRadius: 6,
            background: "rgba(0,0,0,0.04)",
            fontSize: 13,
            lineHeight: 1.5,
          }}
        >
          生成流已升级为「方案池 / 方案运行」（scheme）。问题池直连生成暂不可用，新的方案录入与运行界面即将上线。
        </div>
        <div className="aiConfigActions">
          <button
            className="aiGenerateBtn"
            type="button"
            onClick={handleGenerate}
            disabled
            title="问题池直连生成已升级为「方案池」，新界面即将上线"
          >
            <Sparkles size={15} />
            生成流已升级（方案池）
          </button>
        </div>
      </div>

      {/* Right: result */}
      <div className="aiResultPanel">
        {!isGenerating && !hasResult && (
          <div className="aiEmptyState">
            <Sparkles size={40} style={{ opacity: 0.2 }} />
            <p className="aiEmptyText">
              选择问题池 → 勾选问题（按板块合并）或留空让"最近没上的板块"自动选题 →
              选 Skill/提示词 → 生成
            </p>
          </div>
        )}
        {isGenerating && (
          <div>
            <div className="aiGeneratingBar" />
            <p className="aiGeneratingText">正在生成 {expectedArticleCount} 篇，请稍候…</p>
          </div>
        )}
        {hasResult && !isGenerating && session?.status === "done" && (
          <div className="aiArticleList">
            {generatedArticles.length === 0 ? (
              <div className="aiEmptyState">
                <p className="aiEmptyText">生成完成，但未产出文章，请检查 Skill / 提示词配置</p>
              </div>
            ) : (
              generatedArticles.map((a) => (
                <div key={a.id} className="aiArticleCard">
                  <div className="aiArticleTitle">{a.title}</div>
                  {a.plain_text && (
                    <div className="aiArticleBody">
                      {a.plain_text.slice(0, 120)}
                      {a.plain_text.length > 120 ? "…" : ""}
                    </div>
                  )}
                  <div className="aiArticleFooter">
                    <span className="aiSavedBadge">已保存</span>
                    <button
                      className="aiOpenLink"
                      type="button"
                      onClick={onNavigateToContent}
                      title="切换到内容管理查看"
                    >
                      在文章管理中打开
                      <ExternalLink size={11} />
                    </button>
                  </div>
                </div>
              ))
            )}
          </div>
        )}
        {hasResult && !isGenerating && session?.status === "failed" && (
          <div className="aiEmptyState">
            <p style={{ color: "var(--red)" }}>生成失败：{session.error_message || "未知错误"}</p>
          </div>
        )}
      </div>
    </div>
  );
}
