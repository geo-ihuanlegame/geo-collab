import type { Account } from "../../types";
import { assetSrc } from "../../api/core";

export function AccountRow({
  account,
  onAuthorize,
  onCheck,
  onEdit,
  onDelete,
}: {
  account: Account;
  onAuthorize: () => void;
  onCheck: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const isActive = account.status === "valid";
  const accountId = account.platform_user_id ?? account.app_id ?? account.contact ?? "—";

  return (
    <div className="accountRow">
      <div className="accountRowCell accountRowCellStatus">
        <span className={`statusPill ${isActive ? "active" : "inactive"}`}>
          {isActive ? "启用中" : "已失效"}
        </span>
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
            style={{ background: isActive ? "#3C9A66" : "#D45550" }}
          />
        </div>
        <div className="accountRowNameWrap">
          <span className="accountRowName">{account.display_name}</span>
          <span className="accountRowId">{accountId}</span>
        </div>
      </div>
      <div className="accountRowCell accountRowCellPlatform">
        <span>{account.platform_name}</span>
      </div>
      <div className="accountRowCell accountRowCellRemark">
        <span className="accountRowRemark">{account.note || "—"}</span>
      </div>
      <div className="accountRowCell accountRowCellActions">
        <button type="button" className="accountRowAction" onClick={onAuthorize}>
          授权
        </button>
        <button type="button" className="accountRowAction" onClick={onCheck}>
          检测
        </button>
        <button type="button" className="accountRowAction" onClick={onEdit}>
          编辑
        </button>
        <button type="button" className="accountRowAction accountRowActionDelete" onClick={onDelete}>
          删除
        </button>
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
