import { useState } from "react";
import { LoaderCircle, X } from "lucide-react";
import type { Account } from "../../types";
import { updateAccount } from "../../api/accounts";
import { useToast } from "../../components/Toast";

export function EditAccountDialog({
  account,
  onClose,
  onSaved,
}: {
  account: Account;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { toast } = useToast();
  const [displayName, setDisplayName] = useState(account.display_name);
  const [contact, setContact] = useState(account.contact ?? "");
  const [note, setNote] = useState(account.note ?? "");
  const [distributionEnabled, setDistributionEnabled] = useState(account.distribution_enabled);
  const [saving, setSaving] = useState(false);

  async function handleSave() {
    if (!displayName.trim()) {
      toast("请填写账号名称", "error");
      return;
    }
    setSaving(true);
    try {
      // contact / note 传空串即可清空（后端 update_account_fields 对空串 != None 直接落库）。
      await updateAccount(account.id, {
        display_name: displayName.trim(),
        contact: contact.trim(),
        note: note.trim(),
        distribution_enabled: distributionEnabled,
      });
      onSaved();
      toast("已保存", "success");
      onClose();
    } catch (err) {
      toast(err instanceof Error ? err.message : "保存失败", "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="modalBackdrop" role="presentation" onClick={onClose}>
      <div
        className="addAuthDialog"
        role="dialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="addAuthHeader">
          <span className="addAuthTitle">编辑账号</span>
          <button type="button" className="addAuthClose" onClick={onClose}>
            <X size={20} />
          </button>
        </div>
        <div className="addAuthStep">{account.platform_name} · 修改账号信息</div>
        <div className="addAuthBody">
          <div className="addAuthField">
            <div className="addAuthLabel">账号名称 *</div>
            <input
              className="addAuthInput"
              placeholder="例如：纪缘"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
            />
          </div>

          <div className="addAuthField">
            <div className="addAuthLabel">绑定联系方式</div>
            <input
              className="addAuthInput"
              placeholder="手机号 / QQ —— 号失效时凭此联系负责人扫码"
              value={contact}
              onChange={(e) => setContact(e.target.value)}
            />
          </div>

          <div className="addAuthField">
            <div className="addAuthLabel">备注</div>
            <input
              className="addAuthInput"
              placeholder="用途、归属人等(选填)"
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
          </div>

          <div className="addAuthToggleRow">
            <div className="addAuthToggleLeft">
              <span style={{ fontSize: 14, fontWeight: 500, color: "var(--fg)" }}>分发</span>
              <span className="addAuthToggleHint">开启后纳入自动分发</span>
            </div>
            <button
              type="button"
              className={`addAuthToggle${distributionEnabled ? " on" : ""}`}
              onClick={() => setDistributionEnabled(!distributionEnabled)}
            >
              <span className="addAuthToggleKnob" />
            </button>
          </div>
        </div>
        <div className="addAuthFooter">
          <button type="button" className="secondaryButton" onClick={onClose}>取消</button>
          <button
            type="button"
            className="primaryButton"
            disabled={saving || !displayName.trim()}
            style={{
              background: "#4C6EF5",
              fontSize: 13.5,
              fontWeight: 600,
              gap: 6,
              borderRadius: 9,
              padding: "10px 20px",
              color: "#fff",
              border: "none",
              display: "inline-flex",
              alignItems: "center",
              cursor: saving ? "not-allowed" : "pointer",
              opacity: saving ? 0.6 : 1,
            }}
            onClick={() => void handleSave()}
          >
            {saving ? <LoaderCircle size={15} className="spin" /> : "保存"}
          </button>
        </div>
      </div>
    </div>
  );
}
