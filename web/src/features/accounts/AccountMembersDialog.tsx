import { useEffect, useState } from "react";
import { Crown, LoaderCircle, Trash2, X } from "lucide-react";
import type { Account, AccountMember } from "../../types";
import { listAccountMembers, removeAccountMember } from "../../api/accounts";
import { useToast } from "../../components/Toast";

export function AccountMembersDialog({
  account,
  onClose,
  onChanged,
}: {
  account: Account;
  onClose: () => void;
  onChanged: () => void;
}) {
  const { toast } = useToast();
  const [members, setMembers] = useState<AccountMember[]>([]);
  const [loading, setLoading] = useState(true);
  const [removing, setRemoving] = useState<number | null>(null);

  async function load() {
    setLoading(true);
    try {
      const data = await listAccountMembers(account.id);
      setMembers(data);
    } catch (err) {
      toast(err instanceof Error ? err.message : "加载成员失败", "error");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleRemove(member: AccountMember) {
    if (member.is_owner) return;
    setRemoving(member.user_id);
    try {
      await removeAccountMember(account.id, member.user_id);
      onChanged();
      await load();
      toast("已移除成员", "success");
    } catch (err) {
      toast(err instanceof Error ? err.message : "移除失败", "error");
    } finally {
      setRemoving(null);
    }
  }

  const grantedViaLabel: Record<string, string> = {
    login_dedup: "登录查重",
    backfill_merge: "历史回填",
    manual: "手动添加",
  };

  return (
    <div className="modalBackdrop" role="presentation" onMouseDown={onClose}>
      <div
        className="addAuthDialog accountMembersDialog"
        role="dialog"
        aria-modal="true"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="addAuthHeader">
          <span className="addAuthTitle">共享成员</span>
          <button type="button" className="addAuthClose" onClick={onClose}>
            <X size={20} />
          </button>
        </div>
        <div className="addAuthStep">{account.platform_name} · {account.display_name}</div>
        <div className="addAuthBody accountMembersBody">
          {loading ? (
            <div className="accountMembersLoading">
              <LoaderCircle size={20} className="spin" />
              <span>加载中…</span>
            </div>
          ) : members.length === 0 ? (
            <p className="emptyText" style={{ padding: "16px 0", margin: 0 }}>暂无共享成员</p>
          ) : (
            <div className="accountMembersList">
              {members.map((m) => (
                <div key={m.user_id} className="accountMemberRow">
                  <div className="accountMemberAvatar">
                    {(m.display_name ?? m.username ?? "?").slice(0, 1).toUpperCase()}
                  </div>
                  <div className="accountMemberInfo">
                    <div className="accountMemberName">
                      {m.display_name ?? m.username ?? "未知用户"}
                      {m.is_owner && (
                        <span className="accountMemberOwnerBadge">
                          <Crown size={10} />
                          所有者
                        </span>
                      )}
                    </div>
                    <div className="accountMemberMeta">
                      {grantedViaLabel[m.granted_via] ?? m.granted_via}
                    </div>
                  </div>
                  {!m.is_owner && (
                    <button
                      type="button"
                      className="accountMemberRemoveBtn"
                      disabled={removing === m.user_id}
                      onClick={() => void handleRemove(m)}
                      title="移除成员"
                    >
                      {removing === m.user_id ? (
                        <LoaderCircle size={14} className="spin" />
                      ) : (
                        <Trash2 size={14} />
                      )}
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
        <div className="addAuthFooter">
          <button type="button" className="secondaryButton" onClick={onClose}>关闭</button>
        </div>
      </div>
    </div>
  );
}
