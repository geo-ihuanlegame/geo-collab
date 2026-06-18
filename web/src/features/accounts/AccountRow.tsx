import type { Account } from "../../types";
import { assetSrc } from "../../api/core";
import { Phone, Users } from "lucide-react";

type StatusMeta = { label: string; pill: "active" | "inactive" | "disabled"; dot: string };

// 媒体矩阵 badge 派生（失效优先 > 已停用 > 启用中）：
//  · status 非 valid（expired/unknown）→「已失效」，需重新授权（系统检测产出，见 auth.py）。
//  · status 正常但 distribution_enabled=false →「已停用」（运营主动关分发，派生展示、无独立 status 值）。
//  · 否则「启用中」。
// 后端状态枚举仅 valid|expired|unknown（无 disabled），见 server/app/modules/accounts/models.py。
const EXPIRED_META: StatusMeta = { label: "已失效", pill: "inactive", dot: "var(--red)" };
const DISABLED_META: StatusMeta = { label: "已停用", pill: "disabled", dot: "var(--fg-3)" };
const ACTIVE_META: StatusMeta = { label: "启用中", pill: "active", dot: "var(--green)" };

function accountStatusMeta(account: Account): StatusMeta {
  if (account.status !== "valid") return EXPIRED_META;
  if (!account.distribution_enabled) return DISABLED_META;
  return ACTIVE_META;
}

export function AccountRow({
  account,
  onAuthorize,
  onCheck,
  onEdit,
  onDelete,
  onManageMembers,
}: {
  account: Account;
  onAuthorize: () => void;
  onCheck: () => void;
  onEdit: () => void;
  onDelete: () => void;
  onManageMembers?: () => void;
}) {
  const meta = accountStatusMeta(account);
  // 列表行展示账号手机号（联系方式）；平台 ID 移到编辑弹窗里只读展示。
  const phone = account.contact?.trim() || "—";
  const isShared = account.member_count > 0;

  return (
    <div className="accountRow">
      <div className="accountRowCell accountRowCellStatus">
        <span className={`statusPill ${meta.pill}`}>{meta.label}</span>
      </div>
      <div className="accountRowCell accountRowCellAccount">
        <div className="accountRowAvatar">
          <span className="accountRowAvatarCircle">
            {account.avatar_asset_id ? (
              <img
                className="accountRowAvatarImg"
                src={assetSrc(account.avatar_asset_id) ?? undefined}
                alt=""
              />
            ) : (
              account.display_name.slice(0, 1)
            )}
          </span>
          <span
            className="accountRowStatusDot"
            style={{ background: meta.dot }}
          />
        </div>
        <div className="accountRowNameWrap">
          <div className="accountRowNameLine">
            <span className="accountRowName">{account.display_name}</span>
            {!account.identity_known && (
              <span className="accountBadge accountBadgeUnknown">身份未知</span>
            )}
            {isShared && (
              <span className="accountBadge accountBadgeShared">
                <Users size={10} />
                共享 {account.member_count}
              </span>
            )}
          </div>
          <div className="accountRowMetaLine">
            <span className="accountRowId">
              <Phone size={11} className="accountRowPhoneIcon" />
              {phone}
            </span>
            {account.owner_name && (
              <span className="accountRowOwner">归属：{account.owner_name}</span>
            )}
          </div>
        </div>
      </div>
      <div className="accountRowCell accountRowCellPlatform">
        <span>{account.platform_name}</span>
      </div>
      <div className="accountRowCell accountRowCellRemark">
        <span className="accountRowRemark">{account.note || "—"}</span>
      </div>
      <div className="accountRowCell accountRowCellActions">
        {account.can_manage ? (
          <>
            <button type="button" className="accountRowAction" onClick={onAuthorize}>
              授权
            </button>
            <button type="button" className="accountRowAction" onClick={onCheck}>
              检测
            </button>
            <button type="button" className="accountRowAction" onClick={onEdit}>
              编辑
            </button>
            {onManageMembers && isShared && (
              <button type="button" className="accountRowAction" onClick={onManageMembers}>
                成员
              </button>
            )}
            <button type="button" className="accountRowAction accountRowActionDelete" onClick={onDelete}>
              删除
            </button>
          </>
        ) : (
          <span className="accountRowMemberLabel">仅使用</span>
        )}
      </div>
    </div>
  );
}

export function AccountRowHeader() {
  return (
    <div className="accountRow accountRowHeader">
      <div className="accountRowCell accountRowCellStatus">状态</div>
      <div className="accountRowCell accountRowCellAccount">账号</div>
      <div className="accountRowCell accountRowCellPlatform">平台</div>
      <div className="accountRowCell accountRowCellRemark">备注</div>
      <div className="accountRowCell accountRowCellActions">操作</div>
    </div>
  );
}
