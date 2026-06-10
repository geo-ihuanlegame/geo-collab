import { useEffect, useMemo, useRef, useState } from "react";
import {
  deleteAccount,
  listAccounts,
  listPlatforms,
  updateAccount,
} from "../../api/accounts";
import type { Account, PlatformOption } from "../../types";
import { Plus, Search } from "lucide-react";
import { useToast } from "../../components/Toast";
import { AccountRow, AccountRowHeader } from "./AccountRow";
import { AddAuthorizationDialog } from "./AddAuthorizationDialog";

export function AccountsWorkspace({ isActive }: { isActive?: boolean } = {}) {
  const { toast } = useToast();
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [platforms, setPlatforms] = useState<PlatformOption[]>([]);
  const [loading, setLoading] = useState(false);
  const [showAddDialog, setShowAddDialog] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [filterPlatform, setFilterPlatform] = useState<string>("");
  const [filterStatus, setFilterStatus] = useState<string>("");
  const [confirmDelete, setConfirmDelete] = useState<Account | null>(null);

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

  const expiredAccounts = useMemo(
    () => accounts.filter((a) => a.status === "expired"),
    [accounts],
  );

  const filteredAccounts = useMemo(() => {
    return accounts.filter((a) => {
      if (filterPlatform && a.platform_code !== filterPlatform) return false;
      if (filterStatus === "valid" && a.status !== "valid") return false;
      if (filterStatus === "expired" && a.status !== "expired") return false;
      if (searchQuery && !a.display_name.toLowerCase().includes(searchQuery.toLowerCase())) return false;
      return true;
    });
  }, [accounts, filterPlatform, filterStatus, searchQuery]);

  async function handleToggleDistribution(account: Account) {
    setLoading(true);
    try {
      await updateAccount(account.id, { distribution_enabled: !account.distribution_enabled });
      await refreshAccounts();
    } catch (error) {
      toast(error instanceof Error ? error.message : "操作失败", "error");
    } finally {
      setLoading(false);
    }
  }

  async function handleVerify(account: Account) {
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

  const selectedPlatformName = platforms.find((p) => p.code === filterPlatform)?.name ?? "";

  return (
    <>
      <header className="topbar">
        <div>
          <p className="eyebrow">媒体矩阵</p>
          <h1>平台账号授权</h1>
        </div>
        <div className="topActions">
          <button
            className="primaryButton"
            type="button"
            onClick={() => setShowAddDialog(true)}
          >
            <Plus size={16} />
            添加账号
          </button>
        </div>
      </header>

      {/* 待处理 Section */}
      {expiredAccounts.length > 0 && (
        <section style={{ marginBottom: 24 }}>
          <div className="pendingTitle">
            <span className="pendingChevron">▾</span>
            <span className="pendingLabel">待处理</span>
            <span className="pendingCount">· 授权已失效 ({expiredAccounts.length})</span>
          </div>
          <div className="accountTable accountTablePending">
            <AccountRowHeader />
            {expiredAccounts.map((account) => (
              <AccountRow
                key={account.id}
                account={account}
                onToggleDistribution={() => void handleToggleDistribution(account)}
                onVerify={() => void handleVerify(account)}
                onEdit={() => {}}
                onDelete={() => setConfirmDelete(account)}
                showMenu={false}
                onToggleMenu={() => {}}
              />
            ))}
          </div>
        </section>
      )}

      {/* Filter + Account List */}
      <section className="accountListSection">
        <div className="accountFilterBar">
          <div className="accountFilterRow">
            <div className="accountSearchBox">
              <Search size={15} />
              <input
                placeholder="搜索账号…"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
              />
            </div>
            <select
              className="accountFilterSelect"
              value={filterPlatform}
              onChange={(e) => setFilterPlatform(e.target.value)}
            >
              <option value="">全部平台</option>
              {platforms.map((p) => (
                <option key={p.code} value={p.code}>{p.name}</option>
              ))}
            </select>
            <select
              className="accountFilterSelect"
              value={filterStatus}
              onChange={(e) => setFilterStatus(e.target.value)}
            >
              <option value="">全部状态</option>
              <option value="valid">启用中</option>
              <option value="expired">已失效</option>
            </select>
          </div>
          <div className="accountFilterInfo">
            {filteredAccounts.length} 个账号{filterPlatform ? `（${selectedPlatformName}）` : ""}
            {searchQuery ? ` · 搜索 "${searchQuery}"` : ""}
          </div>
        </div>

        <div className="accountTable">
          <AccountRowHeader />
          {filteredAccounts.map((account) => (
            <AccountRow
              key={account.id}
              account={account}
              onToggleDistribution={() => void handleToggleDistribution(account)}
              onVerify={() => void handleVerify(account)}
              onEdit={() => {}}
              onDelete={() => setConfirmDelete(account)}
              showMenu={false}
              onToggleMenu={() => {}}
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

      {/* Delete Confirm Dialog */}
      {confirmDelete && (
        <div className="modalBackdrop" role="presentation" onMouseDown={() => setConfirmDelete(null)}>
          <section className="groupPickerModal" role="dialog" aria-modal="true" onMouseDown={(event) => event.stopPropagation()}>
            <header className="modalHeader">
              <div>
                <h2>确认删除账号？</h2>
                <p>将同时清除该账号的授权信息，需要重新授权</p>
              </div>
              <button type="button" aria-label="关闭" onClick={() => setConfirmDelete(null)}>
                <span style={{ fontSize: 18 }}>×</span>
              </button>
            </header>
            <footer className="modalActions">
              <button type="button" onClick={() => setConfirmDelete(null)}>取消</button>
              <button type="button" className="dangerButton" disabled={loading} onClick={() => void handleDelete(confirmDelete)}>确认删除</button>
            </footer>
          </section>
        </div>
      )}
    </>
  );
}
