import { useEffect, useMemo, useState } from "react";
import { Copy, Pencil, Plus, RefreshCw, Star, Trash2, X } from "lucide-react";
import { createAiModel, deleteAiModel, listAiModels, updateAiModel } from "../../api/ai-models";
import { useToast } from "../../components/Toast";
import type { AiModel, AiModelPayload } from "../../types";

type ScopeFilter = "all" | "generation" | "ai_format";

const EMPTY_DRAFT: AiModelPayload = {
  label: "",
  model: "",
  scope: "generation",
  base_url: null,
  api_key_env: null,
  is_enabled: true,
  is_default: false,
  sort_order: 0,
};

function scopeLabel(s: string): string {
  return s === "ai_format" ? "格式·配图" : "写作";
}

export function AiModelsWorkspace() {
  const { toast } = useToast();
  const [models, setModels] = useState<AiModel[]>([]);
  const [loading, setLoading] = useState(false);
  const [scopeFilter, setScopeFilter] = useState<ScopeFilter>("all");
  // 弹窗草稿：editId=null 表示新增（含复制）；非空表示编辑该 id
  const [draft, setDraft] = useState<AiModelPayload | null>(null);
  const [editId, setEditId] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);

  async function load() {
    setLoading(true);
    try {
      setModels(await listAiModels());
    } catch (err) {
      toast(err instanceof Error ? err.message : "加载失败", "error");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const visible = useMemo(
    () => (scopeFilter === "all" ? models : models.filter((m) => m.scope === scopeFilter)),
    [models, scopeFilter],
  );

  function openCreate() {
    setEditId(null);
    setDraft({ ...EMPTY_DRAFT });
  }
  function openEdit(m: AiModel) {
    setEditId(m.id);
    setDraft({
      label: m.label,
      model: m.model,
      scope: m.scope,
      base_url: m.base_url,
      api_key_env: m.api_key_env,
      is_enabled: m.is_enabled,
      is_default: m.is_default,
      sort_order: m.sort_order,
    });
  }
  function openClone(m: AiModel) {
    // 复制 = "带初值新增"：沿用 base_url / api_key_env 等，仅名称加后缀提示改名、默认置否
    setEditId(null);
    setDraft({
      label: `${m.label} 副本`,
      model: m.model,
      scope: m.scope,
      base_url: m.base_url,
      api_key_env: m.api_key_env,
      is_enabled: m.is_enabled,
      is_default: false,
      sort_order: m.sort_order,
    });
  }
  function closeModal() {
    setDraft(null);
    setEditId(null);
  }

  async function save() {
    if (!draft) return;
    if (!draft.label.trim()) {
      toast("名称不能为空", "error");
      return;
    }
    setSaving(true);
    try {
      if (editId === null) await createAiModel(draft);
      else await updateAiModel(editId, draft);
      toast(editId === null ? "已新增" : "已保存", "success");
      closeModal();
      await load();
    } catch (err) {
      toast(err instanceof Error ? err.message : "保存失败", "error");
    } finally {
      setSaving(false);
    }
  }

  async function remove(m: AiModel) {
    if (!window.confirm(`删除模型「${m.label}」？`)) return;
    try {
      await deleteAiModel(m.id);
      toast("已删除", "success");
      await load();
    } catch (err) {
      toast(err instanceof Error ? err.message : "删除失败", "error");
    }
  }

  async function quickPatch(m: AiModel, patch: Partial<AiModelPayload>) {
    try {
      await updateAiModel(m.id, patch);
      await load();
    } catch (err) {
      toast(err instanceof Error ? err.message : "更新失败", "error");
    }
  }

  return (
    <>
      <header className="topbar">
        <div>
          <p className="eyebrow">系统管理</p>
          <h1>AI 模型管理</h1>
        </div>
        <div className="topActions">
          <button
            className="secondaryButton"
            type="button"
            disabled={loading}
            onClick={() => void load()}
          >
            <RefreshCw size={15} /> 刷新
          </button>
          <button className="primaryButton" type="button" onClick={openCreate}>
            <Plus size={15} /> 新增模型
          </button>
        </div>
      </header>

      <div
        className="panel"
        style={{ marginBottom: 14, display: "flex", gap: 8, alignItems: "center" }}
      >
        {(["all", "generation", "ai_format"] as ScopeFilter[]).map((s) => (
          <button
            key={s}
            type="button"
            className={scopeFilter === s ? "primaryButton" : "secondaryButton"}
            onClick={() => setScopeFilter(s)}
          >
            {s === "all" ? "全部" : scopeLabel(s)}
          </button>
        ))}
        <span style={{ marginLeft: "auto", color: "var(--fg-3)", fontSize: 12 }}>
          密钥永不入库——行只引用「密钥环境变量」名
        </span>
      </div>

      <div className="panel" style={{ padding: 0, overflow: "hidden" }}>
        {loading && models.length === 0 ? (
          <p style={{ padding: 24, color: "var(--fg-3)" }}>加载中…</p>
        ) : visible.length === 0 ? (
          <p style={{ padding: 24, color: "var(--fg-3)" }}>暂无模型，点击「新增模型」添加</p>
        ) : (
          <div style={{ overflow: "auto", maxHeight: "64vh" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr>
                  <th style={thStyle}>名称</th>
                  <th style={thStyle}>模型串</th>
                  <th style={thStyle}>用途</th>
                  <th style={thStyle}>中转地址</th>
                  <th style={thStyle}>密钥环境变量</th>
                  <th style={thStyle}>状态</th>
                  <th style={thStyle}>默认</th>
                  <th style={thStyle}>操作</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((m) => (
                  <tr key={m.id} style={{ borderBottom: "1px solid var(--hair)" }}>
                    <td style={tdStyle}>
                      <span style={{ fontWeight: 600 }}>{m.label}</span>
                    </td>
                    <td style={tdStyle}>
                      <span style={monoStyle}>{m.model || "（默认）"}</span>
                    </td>
                    <td style={tdStyle}>
                      <span
                        className={`badge ${m.scope === "ai_format" ? "succeeded" : "running"}`}
                      >
                        {scopeLabel(m.scope)}
                      </span>
                    </td>
                    <td style={tdStyle}>
                      <span style={m.base_url ? monoStyle : mutedStyle}>{m.base_url || "—"}</span>
                    </td>
                    <td style={tdStyle}>
                      <span style={m.api_key_env ? monoStyle : mutedStyle}>
                        {m.api_key_env || "（全局）"}
                      </span>
                    </td>
                    <td style={tdStyle}>
                      <button
                        type="button"
                        className="secondaryButton"
                        style={pillBtn}
                        onClick={() => void quickPatch(m, { is_enabled: !m.is_enabled })}
                      >
                        {m.is_enabled ? "启用" : "停用"}
                      </button>
                    </td>
                    <td style={tdStyle}>
                      {m.is_default ? (
                        <span className="badge running">
                          <Star size={11} /> 默认
                        </span>
                      ) : (
                        <button
                          type="button"
                          className="secondaryButton"
                          style={pillBtn}
                          onClick={() => void quickPatch(m, { is_default: true })}
                        >
                          设为默认
                        </button>
                      )}
                    </td>
                    <td style={tdStyle}>
                      <div style={{ display: "flex", gap: 6 }}>
                        <button
                          type="button"
                          className="secondaryButton"
                          style={iconBtn}
                          title="复制为新模型"
                          onClick={() => openClone(m)}
                        >
                          <Copy size={14} />
                        </button>
                        <button
                          type="button"
                          className="secondaryButton"
                          style={iconBtn}
                          title="编辑"
                          onClick={() => openEdit(m)}
                        >
                          <Pencil size={14} />
                        </button>
                        <button
                          type="button"
                          className="dangerButton"
                          style={iconBtn}
                          title="删除"
                          onClick={() => void remove(m)}
                        >
                          <Trash2 size={14} />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {draft && (
        <AiModelFormModal
          draft={draft}
          isEdit={editId !== null}
          saving={saving}
          onChange={setDraft}
          onCancel={closeModal}
          onSave={() => void save()}
        />
      )}
    </>
  );
}

function AiModelFormModal({
  draft,
  isEdit,
  saving,
  onChange,
  onCancel,
  onSave,
}: {
  draft: AiModelPayload;
  isEdit: boolean;
  saving: boolean;
  onChange: (d: AiModelPayload) => void;
  onCancel: () => void;
  onSave: () => void;
}) {
  const set = (patch: Partial<AiModelPayload>) => onChange({ ...draft, ...patch });
  return (
    <div style={backdrop} onClick={onCancel}>
      <div style={modalCard} onClick={(e) => e.stopPropagation()}>
        <div style={modalHead}>
          <div>
            <div style={{ fontSize: 16, fontWeight: 650, color: "var(--fg)" }}>
              {isEdit ? "编辑模型" : "新增模型"}
            </div>
            <div style={{ fontSize: 11.5, color: "var(--fg-3)", marginTop: 2 }}>
              密钥本体存环境变量，从不在此输入
            </div>
          </div>
          <button type="button" className="secondaryButton" style={iconBtn} onClick={onCancel}>
            <X size={16} />
          </button>
        </div>
        <div style={modalBody}>
          <Field label="名称">
            <input
              style={field}
              value={draft.label}
              onChange={(e) => set({ label: e.target.value })}
              placeholder="如 Claude Opus 4.8"
            />
          </Field>
          <Field
            label="模型串 (LiteLLM)"
            hint="OpenAI 兼容中转用 openai/ 前缀；Anthropic 原生中转(CRS)用 anthropic/ 前缀。留空=该用途默认模型。"
          >
            <input
              style={{ ...field, fontFamily: "var(--mono, monospace)" }}
              value={draft.model}
              onChange={(e) => set({ model: e.target.value })}
              placeholder="如 anthropic/claude-opus-4-8"
            />
          </Field>
          <div style={{ display: "flex", gap: 14 }}>
            <Field label="用途">
              <select
                style={field}
                value={draft.scope}
                onChange={(e) => set({ scope: e.target.value as AiModelPayload["scope"] })}
              >
                <option value="generation">写作</option>
                <option value="ai_format">格式·配图</option>
              </select>
            </Field>
            <Field label="排序">
              <input
                type="number"
                style={field}
                value={draft.sort_order}
                onChange={(e) => set({ sort_order: Number(e.target.value) || 0 })}
              />
            </Field>
          </div>
          <Field
            label="中转地址 base_url"
            hint="OpenAI 兼容填 …/v1；Anthropic 中转(CRS)填 ANTHROPIC_BASE_URL 的值。留空=官方端点。"
          >
            <input
              style={{ ...field, fontFamily: "var(--mono, monospace)" }}
              value={draft.base_url ?? ""}
              onChange={(e) => set({ base_url: e.target.value || null })}
              placeholder="如 http://relay:8080/api"
            />
          </Field>
          <Field
            label="密钥环境变量 api_key_env"
            hint="环境变量名（非密钥本体）。留空=回落该用途全局 Key。"
          >
            <input
              style={{ ...field, fontFamily: "var(--mono, monospace)" }}
              value={draft.api_key_env ?? ""}
              onChange={(e) => set({ api_key_env: e.target.value || null })}
              placeholder="如 GEO_CRS_TOKEN"
            />
          </Field>
          <div style={{ display: "flex", gap: 18 }}>
            <label style={checkRow}>
              <input
                type="checkbox"
                checked={draft.is_enabled}
                onChange={(e) => set({ is_enabled: e.target.checked })}
              />{" "}
              启用
            </label>
            <label style={checkRow}>
              <input
                type="checkbox"
                checked={draft.is_default}
                onChange={(e) => set({ is_default: e.target.checked })}
              />{" "}
              设为该用途默认
            </label>
          </div>
        </div>
        <div style={modalFoot}>
          <button type="button" className="secondaryButton" onClick={onCancel} disabled={saving}>
            取消
          </button>
          <button type="button" className="primaryButton" onClick={onSave} disabled={saving}>
            {saving ? "保存中…" : "保存"}
          </button>
        </div>
      </div>
    </div>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 6, flex: 1, minWidth: 0 }}>
      <span style={{ fontSize: 11.5, fontWeight: 500, color: "var(--fg-2)" }}>{label}</span>
      {children}
      {hint && <span style={{ fontSize: 11, color: "var(--fg-3)", lineHeight: 1.5 }}>{hint}</span>}
    </label>
  );
}

const thStyle: React.CSSProperties = {
  padding: "10px 16px",
  textAlign: "left",
  fontWeight: 600,
  color: "var(--fg-3)",
  fontSize: 12,
  whiteSpace: "nowrap",
  position: "sticky",
  top: 0,
  zIndex: 1,
  background: "var(--surface-2)",
  boxShadow: "inset 0 -1px 0 var(--hair)",
};

const tdStyle: React.CSSProperties = {
  padding: "12px 16px",
  verticalAlign: "middle",
};

const monoStyle: React.CSSProperties = {
  fontFamily: "var(--mono, monospace)",
  fontSize: 12,
  color: "var(--fg)",
};

const mutedStyle: React.CSSProperties = { color: "var(--fg-3)" };
const pillBtn: React.CSSProperties = { padding: "4px 10px", fontSize: 12 };
const iconBtn: React.CSSProperties = { padding: "6px 8px" };

const backdrop: React.CSSProperties = {
  position: "fixed",
  inset: 0,
  zIndex: 50,
  background: "rgba(4,6,12,0.62)",
  display: "grid",
  placeItems: "center",
  padding: 24,
};

const modalCard: React.CSSProperties = {
  width: 560,
  maxWidth: "100%",
  maxHeight: "calc(100vh - 48px)",
  display: "flex",
  flexDirection: "column",
  background: "var(--surface-2)",
  border: "1px solid var(--hair)",
  borderRadius: 14,
  overflow: "hidden",
  boxShadow: "0 24px 64px rgba(0,0,0,.55)",
};

const modalHead: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  padding: "16px 20px",
  borderBottom: "1px solid var(--hair)",
};

const modalBody: React.CSSProperties = {
  padding: "20px 24px",
  display: "flex",
  flexDirection: "column",
  gap: 14,
  overflowY: "auto",
};

const modalFoot: React.CSSProperties = {
  display: "flex",
  justifyContent: "flex-end",
  gap: 10,
  padding: "14px 20px",
  borderTop: "1px solid var(--hair)",
};

const field: React.CSSProperties = {
  width: "100%",
  height: 38,
  padding: "0 12px",
  border: "1px solid var(--hair-2, var(--hair))",
  borderRadius: 10,
  background: "var(--paper, var(--glass))",
  color: "var(--fg)",
  fontSize: 13,
  colorScheme: "dark",
  boxSizing: "border-box",
};

const checkRow: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 7,
  fontSize: 13,
  color: "var(--fg)",
};
