import { useState } from "react";
import { Plus, RefreshCw } from "lucide-react";
import { createQuestionPool, syncQuestionPool } from "../../api/ai-generation";
import { useToast } from "../../components/Toast";
import type { QuestionPool } from "../../types";

export function PoolManagerModal({
  pools,
  onClose,
  onChanged,
}: {
  pools: QuestionPool[];
  onClose: () => void;
  onChanged: () => void;
}) {
  const { toast } = useToast();
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ name: "", feishu_app_token: "", feishu_table_id: "" });
  const [syncingId, setSyncingId] = useState<number | null>(null);

  async function handleCreate() {
    if (!form.name.trim()) {
      toast("请填写问题池名称", "error");
      return;
    }
    try {
      await createQuestionPool({
        name: form.name.trim(),
        feishu_app_token: form.feishu_app_token.trim() || undefined,
        feishu_table_id: form.feishu_table_id.trim() || undefined,
      });
      setForm({ name: "", feishu_app_token: "", feishu_table_id: "" });
      setCreating(false);
      toast("已创建问题池", "success");
      onChanged();
    } catch (e) {
      toast(e instanceof Error ? e.message : "创建失败", "error");
    }
  }

  async function handleSync(poolId: number) {
    setSyncingId(poolId);
    try {
      const r = await syncQuestionPool(poolId);
      toast(
        `同步完成：新增 ${r.added}、更新 ${r.updated}、恢复 ${r.reactivated}、失效 ${r.deactivated}`,
        "success",
      );
      onChanged();
    } catch (e) {
      toast(e instanceof Error ? e.message : "同步失败", "error");
    } finally {
      setSyncingId(null);
    }
  }

  return (
    <div className="modalBackdrop" role="dialog" aria-modal="true" onClick={onClose}>
      <div
        className="schemePanel"
        style={{ width: "min(560px, calc(100vw - 48px))" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="schemePanelHead">
          <div>
            <h3>问题池</h3>
            <p className="schemePanelHint">问题池是飞书多维表的本地镜像，方案从中选问题</p>
          </div>
          <button className="iconButton" type="button" onClick={onClose}>
            ×
          </button>
        </div>

        <div className="schemePanelBody">
          {pools.length === 0 && !creating && (
            <div className="schemeEmpty">还没有问题池，点下方「新建问题池」开始</div>
          )}
          {pools.map((p) => (
            <div className="schemeCard" key={p.id} style={{ padding: "12px 14px" }}>
              <div className="schemeCardInfo">
                <span style={{ fontSize: 14, color: "var(--fg)" }}>{p.name}</span>
                <span style={{ fontSize: 12, color: "var(--fg-3)" }}>
                  {p.last_synced_at
                    ? `上次同步 ${new Date(p.last_synced_at).toLocaleString()}`
                    : "尚未同步"}
                  {p.feishu_table_id ? "" : " · 未绑定飞书表"}
                </span>
              </div>
              <button
                className="secondaryButton"
                type="button"
                disabled={syncingId === p.id || !p.feishu_table_id}
                title={p.feishu_table_id ? "从飞书多维表同步" : "未绑定飞书表，无法同步"}
                onClick={() => handleSync(p.id)}
              >
                <RefreshCw size={14} />
                {syncingId === p.id ? "同步中…" : "同步"}
              </button>
            </div>
          ))}

          {creating ? (
            <div className="schemeLineCard" style={{ gap: 8 }}>
              <input
                className="aiSelect"
                placeholder="问题池名称（必填）"
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
              />
              <input
                className="aiSelect"
                placeholder="飞书多维表 app_token（可选）"
                value={form.feishu_app_token}
                onChange={(e) => setForm({ ...form, feishu_app_token: e.target.value })}
              />
              <input
                className="aiSelect"
                placeholder="飞书表 table_id（可选）"
                value={form.feishu_table_id}
                onChange={(e) => setForm({ ...form, feishu_table_id: e.target.value })}
              />
              <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                <button className="secondaryButton" type="button" onClick={() => setCreating(false)}>
                  取消
                </button>
                <button className="primaryButton" type="button" onClick={handleCreate}>
                  创建
                </button>
              </div>
            </div>
          ) : (
            <button
              className="secondaryButton"
              type="button"
              style={{ alignSelf: "flex-start" }}
              onClick={() => setCreating(true)}
            >
              <Plus size={14} />
              新建问题池
            </button>
          )}
        </div>

        <div className="schemePanelFoot">
          <button className="secondaryButton" type="button" onClick={onClose}>
            关闭
          </button>
        </div>
      </div>
    </div>
  );
}
