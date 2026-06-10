import type { Account } from "../../types";
import { Check, Pencil, Trash2 } from "lucide-react";

const platformIcons: Record<string, string> = {
  wechat_mp: "微",
  toutiao: "头",
  baijiahao: "百",
  sohu: "狐",
  netease: "易",
  taptap: "TT",
};

const platformColors: Record<string, string> = {
  wechat_mp: "#07C160",
  toutiao: "#FF6B35",
  baijiahao: "#1677FF",
  sohu: "#FFD700",
  netease: "#E60012",
  taptap: "#FF5C5C",
};

export function AccountRow({
  account,
  onToggleDistribution,
  onVerify,
  onEdit,
  onDelete,
  onToggleMenu,
  showMenu,
}: {
  account: Account;
  onToggleDistribution: () => void;
  onVerify: () => void;
  onEdit: () => void;
  onDelete: () => void;
  onToggleMenu: () => void;
  showMenu: boolean;
}) {
  const icon = platformIcons[account.platform_code] ?? account.platform_name.slice(0, 1);
  const color = platformColors[account.platform_code] ?? "#666";
  const initial = account.display_name.slice(0, 1);
  const statusLabel = account.status === "valid" ? "启用中" : account.status === "expired" ? "已失效" : account.status;

  return (
    <div
      className="accountRow"
      onMouseLeave={() => {
        if (showMenu) onToggleMenu();
      }}
    >
      <div className="accountRowCell accountRowCellAccount">
        <div className="accountRowAvatar" style={{ background: `${color}18`, color }}>
          <span>{initial}</span>
          {account.status === "valid" ? (
            <span className="accountRowDot" style={{ background: "#3F8F5C", borderColor: "#fff" }} />
          ) : (
            <span className="accountRowDot" style={{ background: "#C5482E", borderColor: "#fff" }} />
          )}
        </div>
        <div className="accountRowNameWrap">
          <span className="accountRowName">{account.display_name}</span>
          <span className="accountRowId">{account.platform_user_id ?? account.app_id ?? account.contact ?? "—"}</span>
        </div>
      </div>
      <div className="accountRowCell accountRowCellPlatform">
        <div className="accountRowPlatformIcon" style={{ background: `${color}18`, color }}>
          {icon}
        </div>
        <span>{account.platform_name}</span>
      </div>
      <div className="accountRowCell accountRowCellRemark">
        <span className="accountRowRemark">{account.note || "—"}</span>
      </div>
      <div className="accountRowCell accountRowCellStatus">
        <span className={`badge ${account.status}`}>{statusLabel}</span>
      </div>
      <div className="accountRowCell accountRowCellDist">
        <button
          type="button"
          className={`accountRowToggle ${account.distribution_enabled ? "on" : ""}`}
          onClick={onToggleDistribution}
          title={account.distribution_enabled ? "点击停用分发" : "点击启用分发"}
        >
          <span className="accountRowToggleKnob" />
        </button>
      </div>
      <div className="accountRowCell accountRowCellActions">
        <button type="button" className="accountRowAction" onClick={onVerify} title="验证凭据">
          <Check size={14} />
        </button>
        <button type="button" className="accountRowAction" onClick={onEdit} title="编辑">
          <Pencil size={14} />
        </button>
        <button type="button" className="accountRowAction accountRowActionDelete" onClick={onDelete} title="删除">
          <Trash2 size={14} />
        </button>
      </div>
    </div>
  );
}

export function AccountRowHeader() {
  return (
    <div className="accountRow accountRowHeader">
      <div className="accountRowCell accountRowCellAccount">账号</div>
      <div className="accountRowCell accountRowCellPlatform">平台</div>
      <div className="accountRowCell accountRowCellRemark">备注</div>
      <div className="accountRowCell accountRowCellStatus">状态</div>
      <div className="accountRowCell accountRowCellDist">分发</div>
      <div className="accountRowCell accountRowCellActions">操作</div>
    </div>
  );
}
