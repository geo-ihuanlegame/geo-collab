import { useEffect, useRef, useState } from "react";
import { Pencil, Plus, Trash2, Upload } from "lucide-react";
import {
  listSkills,
  uploadSkill,
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
          style={{ minHeight: 160 }}
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
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [skills, setSkills] = useState<Skill[]>([]);
  const [prompts, setPrompts] = useState<PromptTemplate[]>([]);
  const [skillSearch, setSkillSearch] = useState("");
  const [promptSearch, setPromptSearch] = useState("");
  const [uploading, setUploading] = useState(false);
  const [promptModal, setPromptModal] = useState<{ editing?: PromptTemplate } | null>(null);

  async function reload() {
    const [s, p] = await Promise.all([listSkills(), listPromptTemplates("generation")]);
    setSkills(s);
    setPrompts(p);
  }

  useEffect(() => { reload(); }, []);

  // ── Skill actions ──

  async function handleSkillUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    e.target.value = "";
    setUploading(true);
    try {
      await uploadSkill(file);
      toast("技能导入成功", "success");
      reload();
    } catch (err) {
      toast(err instanceof Error ? err.message : "导入失败", "error");
    } finally {
      setUploading(false);
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
      (s.description ?? "").toLowerCase().includes(skillSearch.toLowerCase()),
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
                {skills.length === 0 ? "暂无技能，请先导入" : "没有匹配的技能"}
              </p>
            )}
            {filteredSkills.map((skill) => (
              <div key={skill.id} className={`aiSkillCard${skill.is_enabled ? "" : " disabled"}`}>
                <div className="aiCardName">{skill.name}</div>
                {skill.description && <div className="aiCardDesc">{skill.description}</div>}
                <div className="aiCardStats">
                  <span>参考 {skill.file_stats.references}</span>
                  <span>骨架 {skill.file_stats.skeletons}</span>
                  <span>资产 {skill.file_stats.assets}</span>
                </div>
                <div className="aiCardActions">
                  <Toggle on={skill.is_enabled} onChange={() => handleSkillToggle(skill)} />
                  <span style={{ fontSize: 12, color: "var(--fg-3)" }}>
                    {skill.is_enabled ? "已启用" : "已停用"}
                  </span>
                  <button
                    className="iconButton"
                    type="button"
                    style={{ marginLeft: "auto", color: "var(--red)" }}
                    onClick={() => handleSkillDelete(skill)}
                    title="删除"
                  >
                    <Trash2 size={14} />
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
              disabled={uploading}
              onClick={() => fileInputRef.current?.click()}
            >
              <Upload size={14} />
              {uploading ? "导入中…" : "导入 Skill ZIP"}
            </button>
            <input
              ref={fileInputRef}
              type="file"
              accept=".zip"
              style={{ display: "none" }}
              onChange={handleSkillUpload}
            />
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
