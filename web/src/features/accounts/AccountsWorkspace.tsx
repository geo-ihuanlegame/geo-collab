import { useEffect, useRef, useState } from "react";
import {
  deleteAccount,
  exportAccountPackage,
  finishAccountLoginSession,
  importAccountPackage,
  listAccounts,
  listPlatforms,
  pollLoginSessionUntilActive,
  startAccountLoginSession,
  startPlatformLoginSession,
  stopAccountLoginSession,
  updateAccountDisplayName,
} from "../../api/accounts";
import type { Account, AccountBrowserSession, AccountLoginSessionStatusResponse, PlatformLoginPayload, PlatformOption } from "../../types";
import { CheckCircle2, Download, ExternalLink, Plus, RefreshCw, Trash2, Upload, UserPlus, X } from "lucide-react";
import { useToast } from "../../components/Toast";

const DEFAULT_PLATFORM_CODE = "toutiao";

type ActiveLoginSession = {
  sessionId: string;
  novncUrl: string;
  queueReason?: string | null;
  opening?: boolean;
};

export function AccountsWorkspace({ isActive }: { isActive?: boolean } = {}) {
  const { toast } = useToast();
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [platforms, setPlatforms] = useState<PlatformOption[]>([]);
  const [displayName, setDisplayName] = useState("头条号账号");
  const [accountKey, setAccountKey] = useState("");
  const [loading, setLoading] = useState(false);
  const [renamingId, setRenamingId] = useState<number | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [confirmDeleteAccount, setConfirmDeleteAccount] = useState<Account | null>(null);
  const [activeLoginSessions, setActiveLoginSessions] = useState<Record<number, ActiveLoginSession>>({});
  const [selectedPlatform, setSelectedPlatform] = useState("");

  const selectedPlatformCode = selectedPlatform || (platforms[0]?.code ?? DEFAULT_PLATFORM_CODE);
  const selectedPlatformName = platforms.find(p => p.code === selectedPlatformCode)?.name ?? "";

  const filteredAccounts = selectedPlatform
    ? accounts.filter(a => a.platform_code === selectedPlatform)
    : accounts;

  const isInitialMountRef = useRef(true);

  async function refreshAccounts() {
    const data = await listAccounts();
    setAccounts(data);
  }

  function handlePlatformChange(platformCode: string) {
    setSelectedPlatform(platformCode);
    localStorage.setItem('selectedPlatform', platformCode);
  }

  useEffect(() => {
    void (async () => {
      const [platformData, accountData] = await Promise.all([
        listPlatforms(),
        listAccounts(),
      ]);
      setPlatforms(platformData);
      setAccounts(accountData);
    })();

    // 从 localStorage 恢复平台选择
    const savedPlatform = localStorage.getItem('selectedPlatform');
    if (savedPlatform) {
      setSelectedPlatform(savedPlatform);
    }
  }, []);

  useEffect(() => {
    if (isInitialMountRef.current) {
      isInitialMountRef.current = false;
      return;
    }
    if (!isActive) return;
    void (async () => {
      const [platformData, accountData] = await Promise.all([
        listPlatforms(),
        listAccounts(),
      ]);
      setPlatforms(platformData);
      setAccounts(accountData);
    })();
  }, [isActive]);

  async function startNewRemoteLogin() {
    const browserTab = openRemoteBrowserPlaceholder();
    setLoading(true);
    try {
      const payload: PlatformLoginPayload = {
        display_name: displayName,
        account_key: accountKey,
        use_browser: true,
      };
      const result = await startPlatformLoginSession(selectedPlatformCode, { ...payload, channel: "chromium", wait_seconds: 180 });
      rememberLoginSession(result, { opening: true, queueReason: result.queue_reason ?? null });
      await refreshAccounts();
      toast("正在启动远程浏览器…", "info");
      const active = await pollLoginSessionUntilActive(result.account.id, result.session_id, 90_000, (status) =>
        updateLoginSessionStatus(result.account.id, status),
      );
      setActiveLoginSessions((prev) => ({
        ...prev,
        [result.account.id]: {
          sessionId: active.session_id,
          novncUrl: active.novnc_url,
          queueReason: active.queue_reason,
          opening: false,
        },
      }));
      openRemoteBrowser(active.novnc_url, browserTab);
      toast("远程浏览器已打开，登录完成后点击\"完成登录\"", "info");
    } catch (error) {
      if (browserTab && !browserTab.closed) browserTab.close();
      toast(error instanceof Error ? error.message : "打开远程浏览器失败", "error");
    } finally {
      setLoading(false);
    }
  }

  async function startExistingRemoteLogin(account: Account, actionLabel: string) {
    const browserTab = openRemoteBrowserPlaceholder();
    setLoading(true);
    try {
      const result = await startAccountLoginSession(account.id, { channel: "chromium", use_browser: true });
      rememberLoginSession(result, { opening: true, queueReason: result.queue_reason ?? null });
      await refreshAccounts();
      toast(`正在启动远程浏览器…`, "info");
      const active = await pollLoginSessionUntilActive(account.id, result.session_id, 90_000, (status) =>
        updateLoginSessionStatus(account.id, status),
      );
      setActiveLoginSessions((prev) => ({
        ...prev,
        [account.id]: {
          sessionId: active.session_id,
          novncUrl: active.novnc_url,
          queueReason: active.queue_reason,
          opening: false,
        },
      }));
      openRemoteBrowser(active.novnc_url, browserTab);
      toast(`远程浏览器已打开，请完成${actionLabel}后点击"完成登录"`, "info");
    } catch (error) {
      if (browserTab && !browserTab.closed) browserTab.close();
      toast(error instanceof Error ? error.message : `${actionLabel}失败`, "error");
    } finally {
      setLoading(false);
    }
  }

  async function completeLoginSession(account: Account) {
    const active = activeLoginSessions[account.id];
    if (!active) return;
    setLoading(true);
    try {
      const result = await finishAccountLoginSession(account.id, active.sessionId);
      setActiveLoginSessions((prev) => {
        const next = { ...prev };
        delete next[account.id];
        return next;
      });
      await refreshAccounts();
      toast(result.logged_in ? "登录状态已保存" : "未检测到有效登录状态", result.logged_in ? "success" : "error");
    } catch (error) {
      toast(error instanceof Error ? error.message : "完成登录失败", "error");
    } finally {
      setLoading(false);
    }
  }

  async function closeLoginSession(account: Account) {
    const active = activeLoginSessions[account.id];
    if (!active) return;
    setLoading(true);
    try {
      await stopAccountLoginSession(account.id, active.sessionId);
      setActiveLoginSessions((prev) => {
        const next = { ...prev };
        delete next[account.id];
        return next;
      });
      toast("远程浏览器已关闭", "success");
    } catch (error) {
      toast(error instanceof Error ? error.message : "关闭远程浏览器失败", "error");
    } finally {
      setLoading(false);
    }
  }

  function rememberLoginSession(result: AccountBrowserSession, state: Pick<ActiveLoginSession, "opening" | "queueReason"> = {}) {
    setActiveLoginSessions((prev) => ({
      ...prev,
      [result.account.id]: {
        sessionId: result.session_id,
        novncUrl: result.novnc_url ?? "",
        queueReason: state.queueReason ?? result.queue_reason ?? null,
        opening: state.opening ?? !result.novnc_url,
      },
    }));
  }

  function updateLoginSessionStatus(accountId: number, status: AccountLoginSessionStatusResponse) {
    setActiveLoginSessions((prev) => {
      const current = prev[accountId];
      if (!current) return prev;
      return {
        ...prev,
        [accountId]: {
          ...current,
          novncUrl: status.novnc_url ?? current.novncUrl,
          queueReason: status.queue_reason ?? status.error_message ?? current.queueReason ?? null,
          opening: status.status !== "active",
        },
      };
    });
  }

  function openRemoteBrowserPlaceholder() {
    return window.open("about:blank", "_blank");
  }

  function openRemoteBrowser(url: string, targetWindow?: Window | null) {
    if (!url) return;
    const target = normalizeRemoteBrowserUrl(url);
    if (targetWindow && !targetWindow.closed) {
      targetWindow.location.href = target;
      return;
    }
    window.open(target, "_blank", "noopener,noreferrer");
  }

  function normalizeRemoteBrowserUrl(rawUrl: string) {
    const url = new URL(rawUrl, window.location.href);
    const localHosts = new Set(["0.0.0.0", "127.0.0.1", "localhost"]);
    if (localHosts.has(url.hostname)) {
      url.hostname = window.location.hostname;
      url.protocol = window.location.protocol;
      url.port = window.location.port;
      if (url.searchParams.has("host")) {
        url.searchParams.set("host", window.location.hostname);
      }
      if (url.searchParams.has("port")) {
        url.searchParams.set("port", window.location.port || (window.location.protocol === "https:" ? "443" : "80"));
      }
    }
    return url.toString();
  }

  async function remove(account: Account) {
    setLoading(true);
    try {
      await deleteAccount(account.id);
      await refreshAccounts();
      toast("账号已删除", "success");
    } catch (error) {
      toast(error instanceof Error ? error.message : "删除失败", "error");
    } finally {
      setLoading(false);
    }
  }

  async function importAuthPackage(file: File) {
    setLoading(true);
    try {
      const formData = new FormData();
      formData.append("file", file);
      const result = await importAccountPackage(formData);
      await refreshAccounts();
      const importedN = result.imported.length;
      const skippedN = result.skipped.length;
      const msg = `导入成功 ${importedN} 个账号${skippedN ? `，${skippedN} 个已存在跳过` : ""}。账号有效性取决于平台 session 是否仍在线，请点击「校验」确认后再发布。`;
      toast(msg, "success");
    } catch (error) {
      toast(error instanceof Error ? error.message : "导入失败", "error");
    } finally {
      setLoading(false);
    }
  }

  async function renameAccount(accountId: number) {
    if (!renameValue.trim()) return;
    setLoading(true);
    try {
      await updateAccountDisplayName(accountId, renameValue.trim());
      await refreshAccounts();
      setRenamingId(null);
      toast("已重命名", "success");
    } catch (error) {
      toast(error instanceof Error ? error.message : "重命名失败", "error");
    } finally {
      setLoading(false);
    }
  }

  async function exportAuthPackage() {
    setLoading(true);
    try {
      const response = await exportAccountPackage(accounts.map((account) => account.id));
      const exportPath = response.headers.get("x-export-path") ?? "";
      const blob = await response.blob();
      const disposition = response.headers.get("content-disposition") ?? "";
      const match = disposition.match(/filename="?([^"]+)"?/);
      const filename = match?.[1] ?? `geo-auth-export-${Date.now()}.zip`;
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      toast(exportPath ? `已导出：${exportPath}` : "授权包已导出", "success");
    } catch (error) {
      toast(error instanceof Error ? error.message : "导出授权包失败", "error");
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <header className="topbar">
        <div>
          <p className="eyebrow">媒体矩阵</p>
          <h1>平台账号授权</h1>
        </div>
        <div className="topActions">
          <label className="secondaryButton" style={{ cursor: loading ? "not-allowed" : "pointer", opacity: loading ? 0.5 : 1 }}>
            <Upload size={16} />
            导入授权包
            <input
              type="file"
              accept=".zip"
              style={{ display: "none" }}
              disabled={loading}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) void importAuthPackage(file);
                e.target.value = "";
              }}
            />
          </label>
          <button className="secondaryButton" disabled={loading || accounts.length === 0} type="button" onClick={() => void exportAuthPackage()}>
            <Download size={16} />
            导出授权包
          </button>
        </div>
      </header>

      <section className="mediaGrid">
        <section className="accountForm">
          <h2>添加平台账号</h2>
          <label>
            平台
            <select value={selectedPlatform} onChange={(event) => handlePlatformChange(event.target.value)}>
              <option value="">-- 全部 --</option>
              {platforms.map(platform => (
                <option key={platform.code} value={platform.code}>
                  {platform.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            显示名称
            <input value={displayName} onChange={(event) => setDisplayName(event.target.value)} />
          </label>
          <label>
            本地状态目录
            <input value={accountKey} onChange={(event) => setAccountKey(event.target.value)} />
          </label>
          <div className="accountActions">
            <button className="primaryButton" disabled={loading || !selectedPlatform} type="button" onClick={() => void startNewRemoteLogin()}>
              <UserPlus size={16} />
              添加授权
            </button>
          </div>
          <small style={{ color: "var(--fg-3)", fontSize: 11 }}>* 仅选择具体平台后可添加</small>
        </section>

        <div className="accountListPane">
          <div className="listHeader">
            <h3>授权账号 <span style={{ color: "var(--fg)", fontSize: 13, textTransform: "none", letterSpacing: 0 }}>
              {selectedPlatform && selectedPlatformName ? `(${selectedPlatformName})` : "(全部)"}
            </span></h3>
            <button type="button" className="listHeaderButton" disabled={loading} onClick={() => void refreshAccounts()}>
              🔄 刷新
            </button>
          </div>
          <section className="accountList">
            {filteredAccounts.map((account) => (
            <article className="accountCard" key={account.id}>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                {renamingId === account.id ? (
                  <>
                    <input
                      autoFocus
                      value={renameValue}
                      style={{ flex: 1, fontSize: 14, fontWeight: 600 }}
                      onChange={(e) => setRenameValue(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") void renameAccount(account.id);
                        if (e.key === "Escape") setRenamingId(null);
                      }}
                    />
                    <button type="button" disabled={loading} onClick={() => void renameAccount(account.id)}>确定</button>
                    <button type="button" onClick={() => setRenamingId(null)}>取消</button>
                  </>
                ) : (
                  <>
                    <strong style={{ flex: 1 }}>{account.display_name}</strong>
                    <button type="button" style={{ fontSize: 12, padding: "2px 6px" }} onClick={() => { setRenamingId(account.id); setRenameValue(account.display_name); }}>改名</button>
                  </>
                )}
              </div>
              <span>{account.platform_name}</span>
              <span className={`badge ${account.status}`}>{account.status}</span>
              <small>{account.state_path}</small>
              <div className="accountCardActions">
                <button type="button" disabled={loading} onClick={() => void startExistingRemoteLogin(account, "校验")}>
                  <CheckCircle2 size={15} />
                  校验
                </button>
                <button type="button" disabled={loading} onClick={() => void startExistingRemoteLogin(account, "重新登录")}>
                  <RefreshCw size={15} />
                  重登
                </button>
                <button type="button" disabled={loading} onClick={() => setConfirmDeleteAccount(account)}>
                  <Trash2 size={15} />
                  删除
                </button>
              </div>
              {activeLoginSessions[account.id] ? (
                <>
                  <small>
                    {activeLoginSessions[account.id]!.opening ? "远程浏览器排队/启动中" : "远程浏览器已就绪"}
                    {activeLoginSessions[account.id]!.queueReason ? `: ${activeLoginSessions[account.id]!.queueReason}` : ""}
                  </small>
                  <div className="accountCardActions">
                    <button type="button" disabled={loading || !activeLoginSessions[account.id]!.novncUrl} onClick={() => openRemoteBrowser(activeLoginSessions[account.id]!.novncUrl)}>
                      <ExternalLink size={15} />
                      打开远程浏览器
                    </button>
                    <button type="button" disabled={loading} onClick={() => void completeLoginSession(account)}>
                      <CheckCircle2 size={15} />
                      完成登录
                    </button>
                    <button type="button" disabled={loading} onClick={() => void closeLoginSession(account)}>
                      <X size={15} />
                      关闭
                    </button>
                  </div>
                </>
              ) : null}
            </article>
          ))}
            {filteredAccounts.length === 0 ? <p className="emptyText">暂无授权账号</p> : null}
          </section>
        </div>
      </section>

      {confirmDeleteAccount ? (
        <div className="modalBackdrop" role="presentation" onMouseDown={() => setConfirmDeleteAccount(null)}>
          <section className="groupPickerModal" role="dialog" aria-modal="true" onMouseDown={(event) => event.stopPropagation()}>
            <header className="modalHeader">
              <div>
                <h2>确认删除账号？</h2>
                <p>将同时清除该账号的本地授权状态，需要重新登录</p>
              </div>
              <button type="button" aria-label="关闭" onClick={() => setConfirmDeleteAccount(null)}>
                <X size={16} />
              </button>
            </header>
            <footer className="modalActions">
              <button type="button" onClick={() => setConfirmDeleteAccount(null)}>取消</button>
              <button type="button" className="dangerButton" disabled={loading} onClick={() => { const account = confirmDeleteAccount; setConfirmDeleteAccount(null); void remove(account); }}>确认删除</button>
            </footer>
          </section>
        </div>
      ) : null}
    </>
  );
}
