import { useEffect, useMemo, useRef, useState } from "react";
import { deleteAccount, listAccounts, listPlatforms } from "../../api/accounts";
import type { Account, PlatformOption } from "../../types";
import { ChevronDown, ChevronRight, Plus, Search, Trash2, X } from "lucide-react";
import { useToast } from "../../components/Toast";
import { useAuth } from "../auth/AuthContext";
import { AccountRow, AccountRowHeader } from "./AccountRow";
import { AddAuthorizationDialog } from "./AddAuthorizationDialog";
import { EditAccountDialog } from "./EditAccountDialog";
import { ReauthorizeDialog } from "./ReauthorizeDialog";

export function AccountsWorkspace({ isActive }: { isActive?: boolean } = {}) {
  const { toast } = useToast();
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";

  // 媒体矩阵账号删除收归管理员。普通账号点删除不发请求、直接给出清楚的原因，
  // 既不出现裸 403，也明确「为什么不给删」。后端 delete 端点同样做了兜底拦截。
  function requestDelete(account: Account) {
    if (!isAdmin) {
      toast(
        "仅管理员可删除媒体矩阵账号。普通账号无删除权限——删除会一并清除登录授权与历史发文记录，为防误删已锁定，如确需删除请联系管理员。",
        "error",
      );
      return;
    }
    setConfirmDelete(account);
  }

  const [accounts, setAccounts] = useState<Account[]>([]);
  const [platforms, setPlatforms] = useState<PlatformOption[]>([]);
  const [loading, setLoading] = useState(false);
  const [showAddDialog, setShowAddDialog] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [filterPlatform, setFilterPlatform] = useState<string>("");
  const [filterStatus, setFilterStatus] = useState<string>("");
  const [confirmDelete, setConfirmDelete] = useState<Account | null>(null);
  const [editTarget, setEditTarget] = useState<Account | null>(null);
  const [reauthTarget, setReauthTarget] = useState<Account | null>(null);
  const [pendingExpanded, setPendingExpanded] = useState(true);

  const isInitialMountRef = useRef(true);

  async function refreshAccounts() {
    const data = await listAccounts();
    setAccounts(data);
  }

  async function loadInitial() {
    const [platformData, accountData] = await Promise.all([
      listPlatforms(),
      listAccounts(),
    ]);
    setPlatforms(platformData);
    setAccounts(accountData);
  }

  useEffect(() => {
    void loadInitial();
  }, []);

  useEffect(() => {
    if (isInitialMountRef.current) {
      isInitialMountRef.current = false;
      return;
    }
    if (!isActive) return;
    void loadInitial();
  }, [isActive]);

  const pendingAccounts = useMemo(
    () => accounts.filter((a) => a.status !== "valid"),
    [accounts],
  );

  const normalAccounts = useMemo(
    () => accounts.filter((a) => a.status === "valid"),
    [accounts],
  );

  const filteredAccounts = useMemo(() => {
    return normalAccounts.filter((a) => {
      if (filterPlatform && a.platform_code !== filterPlatform) return false;
      if (filterStatus === "valid" && a.status !== "valid") return false;
      if (filterStatus === "expired" && a.status !== "expired") return false;
      if (searchQuery && !a.display_name.toLowerCase().includes(searchQuery.toLowerCase())) return false;
      return true;
    });
  }, [normalAccounts, filterPlatform, filterStatus, searchQuery]);

  async function handleCheck(account: Account) {
    setLoading(true);
    try {
      const { verifyCredentials } = await import("../../api/accounts");
      await verifyCredentials(account.id);
      await refreshAccounts();
      toast("凭据验证通过", "success");
    } catch (error) {
      toast(error instanceof Error ? error.message : "验证失败", "error");
    } finally {
      setLoading(false);
    }
  }

  async function handleDelete(account: Account) {
    setLoading(true);
    try {
      await deleteAccount(account.id);
      await refreshAccounts();
      setConfirmDelete(null);
      toast("账号已删除", "success");
    } catch (error) {
      toast(error instanceof Error ? error.message : "删除失败", "error");
    } finally {
      setLoading(false);
    }
  }

  const allPlatforms = useMemo(
    () => platforms.filter((p) => accounts.some((a) => a.platform_code === p.code)),
    [platforms, accounts],
  );

  return (
    <>
      <header className="mediaMatrixHeader">
        <span className="mediaMatrixBreadcrumb">——  媒体矩阵</span>
        <div className="mediaMatrixTitleRow">
          <h1 className="mediaMatrixTitle">平台账号授权</h1>
          <button
            type="button"
            className="mediaMatrixAddBtn"
            onClick={() => setShowAddDialog(true)}
          >
            <Plus size={17} />
            添加账号
          </button>
        </div>
      </header>

      {pendingAccounts.length > 0 && (
        <section className="mediaMatrixSection">
          <div
            className="mediaMatrixSectionTitle"
            onClick={() => setPendingExpanded(!pendingExpanded)}
          >
            {pendingExpanded ? <ChevronDown size={17} /> : <ChevronRight size={17} />}
            <span className="mediaMatrixSectionTitleText">待处理</span>
            <span className="mediaMatrixSectionCount">· 授权已失效 ({pendingAccounts.length})</span>
          </div>
          {pendingExpanded && (
            <div className="mediaMatrixTable">
              <AccountRowHeader />
              {pendingAccounts.map((account) => (
                <AccountRow
                  key={account.id}
                  account={account}
                  onAuthorize={() => setReauthTarget(account)}
                  onCheck={() => void handleCheck(account)}
                  onEdit={() => setEditTarget(account)}
                  onDelete={() => requestDelete(account)}
                />
              ))}
            </div>
          )}
        </section>
      )}

      <section className="mediaMatrixSection">
        <span className="mediaMatrixSectionTitleText">账号清单</span>

        <div className="mediaMatrixFilterBar">
          <div className="mediaMatrixFilterChips">
            <button
              type="button"
              className={`mediaMatrixFilterChip${!filterPlatform ? " active" : ""}`}
              onClick={() => setFilterPlatform("")}
            >全部</button>
            {allPlatforms.map((p) => (
              <button
                key={p.code}
                type="button"
                className={`mediaMatrixFilterChip${filterPlatform === p.code ? " active" : ""}`}
                onClick={() => setFilterPlatform(filterPlatform === p.code ? "" : p.code)}
              >{p.name}</button>
            ))}
          </div>

          <div className="mediaMatrixFilterChips">
            <button
              type="button"
              className={`mediaMatrixFilterChip${!filterStatus ? " active" : ""}`}
              onClick={() => setFilterStatus("")}
            >全部</button>
            <button
              type="button"
              className={`mediaMatrixFilterChip${filterStatus === "valid" ? " active" : ""}`}
              onClick={() => setFilterStatus(filterStatus === "valid" ? "" : "valid")}
            >启用中</button>
            <button
              type="button"
              className={`mediaMatrixFilterChip${filterStatus === "expired" ? " active" : ""}`}
              onClick={() => setFilterStatus(filterStatus === "expired" ? "" : "expired")}
            >已失效</button>
          </div>

          <div className="mediaMatrixSearchBox">
            <Search size={15} />
            <input
              placeholder="搜索账号…"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
            />
          </div>
        </div>

        <div className="mediaMatrixTable">
          <AccountRowHeader />
          {filteredAccounts.map((account) => (
            <AccountRow
              key={account.id}
              account={account}
              onAuthorize={() => setReauthTarget(account)}
              onCheck={() => void handleCheck(account)}
              onEdit={() => setEditTarget(account)}
              onDelete={() => requestDelete(account)}
            />
          ))}
          {filteredAccounts.length === 0 && (
            <p className="emptyText" style={{ padding: "24px 18px", margin: 0 }}>暂无账号</p>
          )}
        </div>
      </section>

      {showAddDialog && (
        <AddAuthorizationDialog
          platforms={platforms}
          onClose={() => setShowAddDialog(false)}
          onCreated={() => void refreshAccounts()}
        />
      )}

      {editTarget && (
        <EditAccountDialog
          account={editTarget}
          onClose={() => setEditTarget(null)}
          onSaved={() => void refreshAccounts()}
        />
      )}

      {reauthTarget && (
        <ReauthorizeDialog
          account={reauthTarget}
          mode={platforms.find((p) => p.code === reauthTarget.platform_code)?.mode}
          onClose={() => setReauthTarget(null)}
          onReauthorized={() => void refreshAccounts()}
        />
      )}

      {confirmDelete && (
        <div className="modalBackdrop" role="presentation" onMouseDown={() => setConfirmDelete(null)}>
          <div
            className="mediaMatrixDeleteDialog"
            role="dialog"
            aria-modal="true"
            onMouseDown={(e) => e.stopPropagation()}
          >
            <div className="mediaMatrixDeleteHeader">
              <div className="mediaMatrixDeleteIconWrap">
                <Trash2 size={20} />
              </div>
              <div className="mediaMatrixDeleteTitleRow">
                <span className="mediaMatrixDeleteTitle">删除账号</span>
                <span className="mediaMatrixDeleteSub">此操作不可撤销</span>
              </div>
              <button
                type="button"
                className="mediaMatrixDeleteClose"
                onClick={() => setConfirmDelete(null)}
              >
                <X size={18} />
              </button>
            </div>
            <div className="mediaMatrixDeleteBody">
              <p>确定要删除以下账号吗？删除后将清除其授权信息，需重新授权才能恢复自动发文。</p>
              <div className="mediaMatrixDeletePreview">
                <div className="mediaMatrixDeletePreviewAvatar">
                  {confirmDelete.display_name.slice(0, 1)}
                </div>
                <span>{confirmDelete.display_name}</span>
                <span className="mediaMatrixDeletePreviewTag">{confirmDelete.platform_name}</span>
              </div>
            </div>
            <div className="mediaMatrixDeleteFooter">
              <button
                type="button"
                className="secondaryButton"
                onClick={() => setConfirmDelete(null)}
              >取消</button>
              <button
                type="button"
                className="deleteConfirmBtn"
                disabled={loading}
                onClick={() => void handleDelete(confirmDelete)}
              >确认删除</button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
