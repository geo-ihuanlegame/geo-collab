import { useEffect, useMemo, useState } from "react";
import { Pencil, Plus, Search, Trash2 } from "lucide-react";
import {
  createPromptTemplate,
  deletePromptTemplate as deletePromptTemplateApi,
  listPromptTemplates,
  patchPromptTemplate,
  updatePromptTemplate,
} from "../../api/prompt-templates";
import { Modal } from "../../components/Modal";
import { useToast } from "../../components/Toast";
import { useAuth } from "../auth/AuthContext";
import type { PromptScope, PromptTemplate } from "../../types";

const scopeTabs: { scope: PromptScope; label: string }[] = [
  { scope: "generation", label: "AI生文提示词" },
  { scope: "ai_format", label: "AI格式提示词" },
  { scope: "image_search", label: "搜图关键词" },
  { scope: "image_companion", label: "陪衬配图提示词" },
];

// 每个 scope 在编辑弹窗里的填写提示，帮助快速上手测试调优
const scopeHints: Partial<Record<PromptScope, string>> = {
  image_search:
    "百度搜图关键词。用 {game} 占位游戏名（AI 判断出的游戏），如「{game} 横版 官方宣传图」会搜「原神 横版 官方宣传图」；不写 {game} 则自动在游戏名后空格拼接。同类目同时只启用一条生效，配合启停做 A/B 测试。",
  image_companion:
    "AI 配图时「陪衬游戏（图库里没有的游戏）」插图的额外提示词，调它可影响 AI 对陪衬游戏配图的积极度。同类目同时只启用一条生效。",
};

function placeholderFor(scope: PromptScope): string {
  if (scope === "image_search") return "如：{game} 横版 官方宣传图";
  if (scope === "image_companion") return "陪衬游戏插图的额外提示词";
  if (scope === "ai_format") return "用于 AI 格式调整的系统提示词";
  return "用于 AI 生文的提示词";
}

function HighlightedContent({ text }: { text: string }) {
  const parts = text.split(/(\{\{[^}]+\}\})/);
  return (
    <>
      {parts.map((part, i) =>
        /^\{\{[^}]+\}\}$/.test(part) ? (
          <span key={i} className="aiParamHighlight">
            {part}
          </span>
        ) : (
          <span key={i}>{part}</span>
        ),
      )}
    </>
  );
}

function Toggle({ on, onChange }: { on: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      type="button"
      className={`aiToggle${on ? " on" : ""}`}
      onClick={() => onChange(!on)}
      title={on ? "停用" : "启用"}
      aria-pressed={on}
    />
  );
}

function PromptModal({
  initial,
  scope,
  canCreateSystem,
  onSave,
  onClose,
}: {
  initial?: PromptTemplate;
  scope: PromptScope;
  canCreateSystem: boolean;
  onSave: (name: string, content: string, isSystem: boolean) => Promise<void>;
  onClose: () => void;
}) {
  const [name, setName] = useState(initial?.name ?? "");
  const [content, setContent] = useState(initial?.content ?? "");
  const [isSystem, setIsSystem] = useState(initial?.is_system ?? false);
  const [saving, setSaving] = useState(false);

  async function handleSave() {
    if (!name.trim() || !content.trim()) return;
    setSaving(true);
    try {
      // isSystem 初值来自 initial?.is_system，普通用户隐藏勾选框故只会透传原值：
      // 编辑系统模板时保持 is_system=true（不降级），新建时保持 false（无法越权置真）。
      await onSave(name.trim(), content.trim(), isSystem);
      onClose();
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      title={initial ? "编辑提示词" : "新建提示词"}
      onClose={onClose}
      width={760}
      footer={
        <>
          <button className="secondaryButton" type="button" onClick={onClose}>
            取消
          </button>
          <button
            className="primaryButton"
            type="button"
            onClick={handleSave}
            disabled={saving || !name.trim() || !content.trim()}
          >
            {saving ? "保存中" : "保存"}
          </button>
        </>
      }
    >
      <div className="promptModalBody">
        {scopeHints[scope] && <p className="aiHintText">{scopeHints[scope]}</p>}
        <label className="aiFormGroup">
          <span className="aiFormLabel">名称</span>
          <input
            className="aiSearchInput promptModalInput"
            placeholder="提示词名称"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <label className="aiFormGroup">
          <span className="aiFormLabel">内容</span>
          <textarea
            className="aiTextarea"
            style={{ minHeight: 420 }}
            placeholder={placeholderFor(scope)}
            value={content}
            onChange={(e) => setContent(e.target.value)}
          />
        </label>
        {canCreateSystem && (
          <label className="promptSystemCheck">
            <input type="checkbox" checked={isSystem} onChange={(e) => setIsSystem(e.target.checked)} />
            <span>系统提示词</span>
          </label>
        )}
      </div>
    </Modal>
  );
}

export function PromptsWorkspace(
  { scope: propScope, isMobile, onScopeChange }:
  { scope?: PromptScope; isMobile?: boolean; onScopeChange?: (s: PromptScope) => void } = {},
) {
  const { user } = useAuth();
  const { toast } = useToast();
  const [scope, setScope] = useState<PromptScope>("generation");

  useEffect(() => {
    if (propScope) setScope(propScope);
  }, [propScope]);
  const [prompts, setPrompts] = useState<PromptTemplate[]>([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(false);
  const [modal, setModal] = useState<{ editing?: PromptTemplate } | null>(null);

  async function reload(nextScope: PromptScope = scope) {
    setLoading(true);
    try {
      setPrompts(await listPromptTemplates(nextScope));
    } catch (err) {
      toast(err instanceof Error ? err.message : "加载提示词失败", "error");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void reload(scope);
  }, [scope]);

  const filteredPrompts = useMemo(
    () =>
      prompts.filter(
        (prompt) =>
          prompt.name.toLowerCase().includes(search.toLowerCase()) ||
          prompt.content.toLowerCase().includes(search.toLowerCase()),
      ),
    [prompts, search],
  );

  // 编辑/启停：admin 通吃；普通用户可改系统/共享模板（如「基础」AI格式提示词）与自己的模板，
  // 但改不了其他普通用户的私有模板。
  function canEdit(prompt: PromptTemplate): boolean {
    return user?.role === "admin" || prompt.is_system || prompt.user_id === user?.id;
  }

  // 删除：系统/共享模板收归 admin，普通用户只能删自己的非系统模板。
  function canDelete(prompt: PromptTemplate): boolean {
    return user?.role === "admin" || (!prompt.is_system && prompt.user_id === user?.id);
  }

  async function handleSave(name: string, content: string, isSystem: boolean) {
    if (modal?.editing) {
      const updated = await updatePromptTemplate(modal.editing.id, {
        name,
        content,
        scope,
        is_system: isSystem,
      });
      setPrompts((prev) => prev.map((item) => (item.id === updated.id ? updated : item)));
      toast("已更新", "success");
      return;
    }

    const created = await createPromptTemplate({ name, content, scope, is_system: isSystem });
    setPrompts((prev) => [created, ...prev]);
    toast("已创建", "success");
  }

  async function handleToggle(prompt: PromptTemplate) {
    try {
      const updated = await patchPromptTemplate(prompt.id, { is_enabled: !prompt.is_enabled });
      setPrompts((prev) => prev.map((item) => (item.id === updated.id ? updated : item)));
    } catch (err) {
      toast(err instanceof Error ? err.message : "操作失败", "error");
    }
  }

  async function handleDelete(prompt: PromptTemplate) {
    if (!window.confirm(`确定删除提示词「${prompt.name}」？`)) return;
    try {
      await deletePromptTemplateApi(prompt.id);
      setPrompts((prev) => prev.filter((item) => item.id !== prompt.id));
      toast("已删除", "success");
    } catch (err) {
      toast(err instanceof Error ? err.message : "删除失败", "error");
    }
  }

  return (
    <div className="promptsWorkspace">
      <header className="topbar">
        <div>
          <p className="eyebrow">内容资产</p>
          <h1>提示词管理</h1>
        </div>
        <div className="topActions">
          <button className="secondaryButton" type="button" disabled={loading} onClick={() => void reload()}>
            刷新
          </button>
          <button className="primaryButton" type="button" onClick={() => setModal({})}>
            <Plus size={15} />
            新建提示词
          </button>
        </div>
      </header>

      {isMobile && (
        <div className="aiTabs">
          {scopeTabs.map((tab) => (
            <button
              key={tab.scope}
              className={`aiTabBtn${scope === tab.scope ? " active" : ""}`}
              type="button"
              onClick={() => {
                setScope(tab.scope);
                onScopeChange?.(tab.scope);
              }}
            >
              {tab.label}
            </button>
          ))}
        </div>
      )}

      <div className="promptsToolbar">
        <Search size={15} />
        <input
          value={search}
          placeholder="搜索提示词"
          onChange={(e) => setSearch(e.target.value)}
        />
        <span className="status">{filteredPrompts.length} 条</span>
      </div>

      <div className="promptTemplateList">
        {filteredPrompts.length === 0 && (
          <div className="aiEmptyState">
            <p className="aiEmptyText">{loading ? "加载中" : "暂无提示词"}</p>
          </div>
        )}
        {filteredPrompts.map((prompt) => {
          const editable = canEdit(prompt);
          const deletable = canDelete(prompt);
          return (
            <article key={prompt.id} className={`promptTemplateCard${prompt.is_enabled ? "" : " disabled"}`}>
              <div className="promptTemplateHeader">
                <div>
                  <div className="aiCardName">{prompt.name}</div>
                  <div className="promptTemplateMeta">
                    <span
                      className="badge"
                      style={{ fontFamily: "var(--mono, monospace)", color: "var(--text-muted, #888)" }}
                      title="数据库 ID"
                    >
                      ID {prompt.id}
                    </span>
                    <span className={`badge ${prompt.is_system ? "running" : "pending"}`}>
                      {prompt.is_system ? "系统" : "个人"}
                    </span>
                    <span className={`badge ${prompt.is_enabled ? "succeeded" : "cancelled"}`}>
                      {prompt.is_enabled ? "启用" : "停用"}
                    </span>
                  </div>
                </div>
                <div className="promptTemplateActions">
                  {editable && <Toggle on={prompt.is_enabled} onChange={() => void handleToggle(prompt)} />}
                  {editable && (
                    <button className="iconButton" type="button" onClick={() => setModal({ editing: prompt })} title="编辑">
                      <Pencil size={14} />
                    </button>
                  )}
                  {deletable && (
                    <button
                      className="iconButton"
                      type="button"
                      style={{ color: "var(--red)" }}
                      onClick={() => void handleDelete(prompt)}
                      title="删除"
                    >
                      <Trash2 size={14} />
                    </button>
                  )}
                </div>
              </div>
              <div className="promptTemplateContent">
                <HighlightedContent text={prompt.content} />
              </div>
            </article>
          );
        })}
      </div>

      {modal !== null && (
        <PromptModal
          initial={modal.editing}
          scope={scope}
          canCreateSystem={user?.role === "admin"}
          onSave={handleSave}
          onClose={() => setModal(null)}
        />
      )}
    </div>
  );
}
