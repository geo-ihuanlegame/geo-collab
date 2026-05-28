import { useEffect, useState } from "react";
import { Pencil, Plus, Trash2 } from "lucide-react";
import {
  listSkills,
  createSkill,
  updateSkill,
  patchSkill,
  deleteSkill as deleteSkillApi,
  listPromptTemplates,
  createPromptTemplate,
  updatePromptTemplate,
  patchPromptTemplate,
  deletePromptTemplate as deletePromptTemplateApi,
} from "../../api/ai-generation";
import { Modal } from "../../components/Modal";
import { useToast } from "../../components/Toast";
import type { Skill, PromptTemplate } from "../../types";

// ── Param highlight ───────────────────────────────────────────────────────

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

// ── Toggle ────────────────────────────────────────────────────────────────

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

// ── Skill modal ───────────────────────────────────────────────────────────

function SkillModal({
  initial,
  onSave,
  onClose,
}: {
  initial?: Skill;
  onSave: (name: string, content: string, description: string | null) => Promise<void>;
  onClose: () => void;
}) {
  const [name, setName] = useState(initial?.name ?? "");
  const [description, setDescription] = useState(initial?.description ?? "");
  const [content, setContent] = useState(initial?.content ?? "");
  const [saving, setSaving] = useState(false);

  async function handleSave() {
    if (!name.trim() || !content.trim()) return;
    setSaving(true);
    try {
      await onSave(name.trim(), content, description.trim() || null);
      onClose();
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      title={initial ? "编辑技能" : "新建技能"}
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
            {saving ? "保存中…" : "保存"}
          </button>
        </>
      }
    >
      <div className="aiFormGroup" style={{ marginBottom: 14 }}>
        <label className="aiFormLabel">名称</label>
        <input
          className="aiSearchInput"
          style={{ margin: 0, width: "100%" }}
          placeholder="技能名称"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </div>
      <div className="aiFormGroup" style={{ marginBottom: 14 }}>
        <label className="aiFormLabel">简介（可选）</label>
        <input
          className="aiSearchInput"
          style={{ margin: 0, width: "100%" }}
          placeholder="一句话说明这个技能的用途"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
      </div>
      <div className="aiFormGroup">
        <label className="aiFormLabel">技能内容</label>
        <textarea
          className="aiTextarea"
          style={{ minHeight: 420 }}
          placeholder="粘贴技能正文（SKILL.md + 参考资料的合集，将整体作为 system prompt 注入）"
          value={content}
          onChange={(e) => setContent(e.target.value)}
        />
      </div>
    </Modal>
  );
}

// ── Prompt modal ──────────────────────────────────────────────────────────

function PromptModal({
  initial,
  onSave,
  onClose,
}: {
  initial?: PromptTemplate;
  onSave: (name: string, content: string) => Promise<void>;
  onClose: () => void;
}) {
  const [name, setName] = useState(initial?.name ?? "");
  const [content, setContent] = useState(initial?.content ?? "");
  const [saving, setSaving] = useState(false);

  async function handleSave() {
    if (!name.trim() || !content.trim()) return;
    setSaving(true);
    try {
      await onSave(name.trim(), content.trim());
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
            {saving ? "保存中…" : "保存"}
          </button>
        </>
      }
    >
      <div className="aiFormGroup" style={{ marginBottom: 14 }}>
        <label className="aiFormLabel">名称</label>
        <input
          className="aiSearchInput"
          style={{ margin: 0, width: "100%" }}
          placeholder="提示词名称"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </div>
      <div className="aiFormGroup">
        <label className="aiFormLabel">内容（支持 {"{{参数}}"} 占位符）</label>
        <textarea
          className="aiTextarea"
          style={{ minHeight: 420 }}
          placeholder="请输入提示词正文…"
          value={content}
          onChange={(e) => setContent(e.target.value)}
        />
      </div>
    </Modal>
  );
}

// ── Main ──────────────────────────────────────────────────────────────────

export function SkillsPromptsTab() {
  const { toast } = useToast();

  const [skills, setSkills] = useState<Skill[]>([]);
  const [prompts, setPrompts] = useState<PromptTemplate[]>([]);
  const [skillSearch, setSkillSearch] = useState("");
  const [promptSearch, setPromptSearch] = useState("");
  const [skillModal, setSkillModal] = useState<{ editing?: Skill } | null>(null);
  const [promptModal, setPromptModal] = useState<{ editing?: PromptTemplate } | null>(null);

  async function reload() {
    const [s, p] = await Promise.all([listSkills(), listPromptTemplates("generation")]);
    setSkills(s);
    setPrompts(p);
  }

  useEffect(() => { reload(); }, []);

  // ── Skill actions ──

  async function handleSkillSave(name: string, content: string, description: string | null) {
    if (skillModal?.editing) {
      const updated = await updateSkill(skillModal.editing.id, { name, content, description });
      setSkills((prev) => prev.map((s) => (s.id === updated.id ? updated : s)));
      toast("已更新", "success");
    } else {
      const created = await createSkill({ name, content, description });
      setSkills((prev) => [...prev, created]);
      toast("已创建", "success");
    }
  }

  async function handleSkillToggle(skill: Skill) {
    try {
      const updated = await patchSkill(skill.id, { is_enabled: !skill.is_enabled });
      setSkills((prev) => prev.map((s) => (s.id === skill.id ? updated : s)));
    } catch (err) {
      toast(err instanceof Error ? err.message : "操作失败", "error");
    }
  }

  async function handleSkillDelete(skill: Skill) {
    if (!window.confirm(`确定删除技能「${skill.name}」？此操作不可撤销。`)) return;
    try {
      await deleteSkillApi(skill.id);
      setSkills((prev) => prev.filter((s) => s.id !== skill.id));
      toast("已删除", "success");
    } catch (err) {
      toast(err instanceof Error ? err.message : "删除失败", "error");
    }
  }

  // ── Prompt actions ──

  async function handlePromptSave(name: string, content: string) {
    if (promptModal?.editing) {
      const updated = await updatePromptTemplate(promptModal.editing.id, { name, content });
      setPrompts((prev) => prev.map((p) => (p.id === updated.id ? updated : p)));
      toast("已更新", "success");
    } else {
      const created = await createPromptTemplate({ name, content });
      setPrompts((prev) => [...prev, created]);
      toast("已创建", "success");
    }
  }

  async function handlePromptToggle(prompt: PromptTemplate) {
    try {
      const updated = await patchPromptTemplate(prompt.id, { is_enabled: !prompt.is_enabled });
      setPrompts((prev) => prev.map((p) => (p.id === prompt.id ? updated : p)));
    } catch (err) {
      toast(err instanceof Error ? err.message : "操作失败", "error");
    }
  }

  async function handlePromptDelete(prompt: PromptTemplate) {
    if (!window.confirm(`确定删除提示词「${prompt.name}」？`)) return;
    try {
      await deletePromptTemplateApi(prompt.id);
      setPrompts((prev) => prev.filter((p) => p.id !== prompt.id));
      toast("已删除", "success");
    } catch (err) {
      toast(err instanceof Error ? err.message : "删除失败", "error");
    }
  }

  const filteredSkills = skills.filter(
    (s) =>
      s.name.toLowerCase().includes(skillSearch.toLowerCase()) ||
      (s.description ?? "").toLowerCase().includes(skillSearch.toLowerCase()) ||
      s.content.toLowerCase().includes(skillSearch.toLowerCase()),
  );

  const filteredPrompts = prompts.filter(
    (p) =>
      p.name.toLowerCase().includes(promptSearch.toLowerCase()) ||
      p.content.toLowerCase().includes(promptSearch.toLowerCase()),
  );

  return (
    <>
      <div className="aiLibraryLayout">
        {/* ── Skills column ── */}
        <div className="aiLibraryCol">
          <div className="aiColHeader">
            <span className="aiColTitle">技能库</span>
            <span className="status">{skills.length} 个</span>
          </div>
          <input
            className="aiSearchInput"
            placeholder="搜索技能…"
            value={skillSearch}
            onChange={(e) => setSkillSearch(e.target.value)}
          />
          <div className="aiCardList">
            {filteredSkills.length === 0 && (
              <p style={{ color: "var(--fg-3)", fontSize: 13 }}>
                {skills.length === 0 ? "暂无技能，请先创建" : "没有匹配的技能"}
              </p>
            )}
            {filteredSkills.map((skill) => (
              <div key={skill.id} className={`aiSkillCard${skill.is_enabled ? "" : " disabled"}`}>
                <div className="aiCardName">{skill.name}</div>
                {skill.description && <div className="aiCardDesc">{skill.description}</div>}
                <div className="aiPromptContent">
                  {skill.content.slice(0, 160)}
                  {skill.content.length > 160 && "…"}
                </div>
                <div className="aiCardActions">
                  <Toggle on={skill.is_enabled} onChange={() => handleSkillToggle(skill)} />
                  <span style={{ fontSize: 12, color: "var(--fg-3)" }}>
                    {skill.is_enabled ? "已启用" : "已停用"}
                  </span>
                  <button
                    className="iconButton"
                    type="button"
                    style={{ marginLeft: "auto" }}
                    onClick={() => setSkillModal({ editing: skill })}
                    title="编辑"
                  >
                    <Pencil size={13} />
                  </button>
                  <button
                    className="iconButton"
                    type="button"
                    style={{ color: "var(--red)" }}
                    onClick={() => handleSkillDelete(skill)}
                    title="删除"
                  >
                    <Trash2 size={13} />
                  </button>
                </div>
              </div>
            ))}
          </div>
          <div className="aiColAddBtn">
            <button
              className="secondaryButton"
              type="button"
              style={{ width: "100%" }}
              onClick={() => setSkillModal({})}
            >
              <Plus size={14} />
              新建技能
            </button>
          </div>
        </div>

        {/* ── Prompts column ── */}
        <div className="aiLibraryCol">
          <div className="aiColHeader">
            <span className="aiColTitle">提示词库</span>
            <span className="status">{prompts.length} 个</span>
          </div>
          <input
            className="aiSearchInput"
            placeholder="搜索提示词…"
            value={promptSearch}
            onChange={(e) => setPromptSearch(e.target.value)}
          />
          <div className="aiCardList">
            {filteredPrompts.length === 0 && (
              <p style={{ color: "var(--fg-3)", fontSize: 13 }}>
                {prompts.length === 0 ? "暂无提示词，请先创建" : "没有匹配的提示词"}
              </p>
            )}
            {filteredPrompts.map((prompt) => (
              <div key={prompt.id} className={`aiPromptCard${prompt.is_enabled ? "" : " disabled"}`}>
                <div className="aiCardName">{prompt.name}</div>
                <div className="aiPromptContent">
                  <HighlightedContent text={prompt.content.slice(0, 120)} />
                  {prompt.content.length > 120 && "…"}
                </div>
                <div className="aiCardActions">
                  <Toggle on={prompt.is_enabled} onChange={() => handlePromptToggle(prompt)} />
                  <span style={{ fontSize: 12, color: "var(--fg-3)" }}>
                    {prompt.is_enabled ? "已启用" : "已停用"}
                  </span>
                  <button
                    className="iconButton"
                    type="button"
                    style={{ marginLeft: "auto" }}
                    onClick={() => setPromptModal({ editing: prompt })}
                    title="编辑"
                  >
                    <Pencil size={13} />
                  </button>
                  <button
                    className="iconButton"
                    type="button"
                    style={{ color: "var(--red)" }}
                    onClick={() => handlePromptDelete(prompt)}
                    title="删除"
                  >
                    <Trash2 size={13} />
                  </button>
                </div>
              </div>
            ))}
          </div>
          <div className="aiColAddBtn">
            <button
              className="secondaryButton"
              type="button"
              style={{ width: "100%" }}
              onClick={() => setPromptModal({})}
            >
              <Plus size={14} />
              新建提示词
            </button>
          </div>
        </div>
      </div>

      {skillModal !== null && (
        <SkillModal
          initial={skillModal.editing}
          onSave={handleSkillSave}
          onClose={() => setSkillModal(null)}
        />
      )}
      {promptModal !== null && (
        <PromptModal
          initial={promptModal.editing}
          onSave={handlePromptSave}
          onClose={() => setPromptModal(null)}
        />
      )}
    </>
  );
}
