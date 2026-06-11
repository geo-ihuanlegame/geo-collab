import { useEffect, useRef, useState } from "react";
import { AppWindow, ArrowRight, Check, ExternalLink, LoaderCircle, X } from "lucide-react";
import type { Account } from "../../types";
import {
  finishAccountLoginSession,
  pollLoginSessionUntilActive,
  startAccountLoginSession,
  updateAccount,
  verifyCredentials,
} from "../../api/accounts";
import { useToast } from "../../components/Toast";
import { openRemoteBrowser } from "../../utils/remoteBrowser";

export function ReauthorizeDialog({
  account,
  mode,
  onClose,
  onReauthorized,
}: {
  account: Account;
  mode: "api" | "browser" | undefined;
  onClose: () => void;
  onReauthorized: () => void;
}) {
  const { toast } = useToast();
  // mode 未知（平台元数据缺失）时按浏览器登录处理，与 AddAuthorizationDialog 一致。
  const isApi = mode === "api";

  if (isApi) {
    return (
      <ApiReauthorize account={account} onClose={onClose} onReauthorized={onReauthorized} toast={toast} />
    );
  }
  return (
    <BrowserReauthorize account={account} onClose={onClose} onReauthorized={onReauthorized} />
  );
}

// ---- 浏览器平台：重新拉起扫码登录会话 ----
function BrowserReauthorize({
  account,
  onClose,
  onReauthorized,
}: {
  account: Account;
  onClose: () => void;
  onReauthorized: () => void;
}) {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [novncUrl, setNovncUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [finishing, setFinishing] = useState(false);

  const pollingActiveRef = useRef(false);
  const finishRequestedRef = useRef(false);

  // 进入即建会话 → 轮询到 active → 打开 noVNC 窗口；之后等用户点「我已完成登录」收尾。
  useEffect(() => {
    let cancelled = false;
    pollingActiveRef.current = true;

    async function init() {
      try {
        const session = await startAccountLoginSession(account.id, {});
        if (cancelled) return;
        setSessionId(session.session_id);
        try {
          const active = await pollLoginSessionUntilActive(account.id, session.session_id);
          if (cancelled || !pollingActiveRef.current) return;
          if (active.novnc_url) {
            setNovncUrl(active.novnc_url);
            openRemoteBrowser(active.novnc_url);
          }
        } catch (err) {
          if (cancelled) return;
          setError(err instanceof Error ? err.message : "启动登录会话失败");
        }
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "启动登录会话失败");
      }
    }

    init();

    return () => {
      cancelled = true;
      pollingActiveRef.current = false;
    };
    // 仅在打开弹窗时跑一次。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function handleClose() {
    pollingActiveRef.current = false;
    onClose();
  }

  function handleReopen() {
    if (novncUrl) {
      openRemoteBrowser(novncUrl);
      return;
    }
    (async () => {
      try {
        const session = await startAccountLoginSession(account.id, {});
        setSessionId(session.session_id);
        setNovncUrl(session.novnc_url);
        if (session.novnc_url) openRemoteBrowser(session.novnc_url);
        setError(null);
      } catch (err) {
        setError(err instanceof Error ? err.message : "启动登录会话失败");
      }
    })();
  }

  async function handleFinishLogin() {
    if (!sessionId) return;
    if (finishRequestedRef.current) return;
    finishRequestedRef.current = true;
    pollingActiveRef.current = false;
    setFinishing(true);
    try {
      try {
        await pollLoginSessionUntilActive(account.id, sessionId, 10_000);
      } catch {
        // 超时也尝试 finish。
      }
      const result = await finishAccountLoginSession(account.id, sessionId);
      if (result.logged_in) {
        onReauthorized();
        handleClose();
      } else {
        setError("登录未完成，请重试");
        finishRequestedRef.current = false;
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "确认登录失败");
      finishRequestedRef.current = false;
    } finally {
      setFinishing(false);
    }
  }

  return (
    <div className="modalBackdrop" role="presentation" onClick={handleClose}>
      <div className="addAuthDialog" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <div className="addAuthHeader">
          <span className="addAuthTitle">重新授权</span>
          <button type="button" className="addAuthClose" onClick={handleClose}>
            <X size={20} />
          </button>
        </div>
        <div className="addAuthStep">{account.platform_name} · 重新登录授权</div>
        <div className="addAuthBody">
          <div className="addAuthContext">正在重新授权 · {account.platform_name} · {account.display_name}</div>
          <div className="addAuthCenter">
            <div className="addAuthScanIcon">
              <AppWindow size={32} />
            </div>
            <div className="addAuthScanTitle">
              已打开「{account.platform_name} · {account.display_name}」登录窗口
            </div>
            <div className="addAuthScanDesc">
              请在新打开的窗口中完成扫码 / 登录，完成后点下方按钮确认。
            </div>
            <div className="addAuthWait">
              <LoaderCircle size={16} className="spin" />
              <span>等待登录完成…</span>
            </div>
            {error && (
              <div style={{ color: "var(--red)", fontSize: 12.5, marginTop: -8 }}>{error}</div>
            )}
            <button type="button" className="addAuthReopen" onClick={handleReopen}>
              <ExternalLink size={14} />
              <span>没有弹出窗口？点此重新打开</span>
            </button>
          </div>
        </div>
        <div className="addAuthFooter">
          <button type="button" className="secondaryButton" onClick={handleClose}>取消</button>
          <button
            type="button"
            className="primaryButton"
            disabled={finishing}
            style={{
              background: "#4C6EF5",
              fontSize: 13.5,
              fontWeight: 600,
              borderRadius: 9,
              padding: "10px 20px",
              color: "#fff",
              border: "none",
              cursor: finishing ? "not-allowed" : "pointer",
              opacity: finishing ? 0.6 : 1,
            }}
            onClick={() => void handleFinishLogin()}
          >
            {finishing ? <LoaderCircle size={15} className="spin" /> : "我已完成登录"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---- API 平台（微信公众号等）：重填 AppID/AppSecret 并验证 ----
function ApiReauthorize({
  account,
  onClose,
  onReauthorized,
  toast,
}: {
  account: Account;
  onClose: () => void;
  onReauthorized: () => void;
  toast: (message: string, kind?: "success" | "error" | "info") => void;
}) {
  const [appId, setAppId] = useState(account.app_id ?? "");
  const [appSecret, setAppSecret] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<{ status: "success" | "error"; message: string } | null>(null);

  async function handleSubmit() {
    if (!appId.trim() || !appSecret.trim()) {
      toast("请填写 AppID 和 AppSecret", "error");
      return;
    }
    setSubmitting(true);
    try {
      await updateAccount(account.id, {
        api_credentials: { app_id: appId.trim(), app_secret: appSecret.trim() },
      });
      try {
        await verifyCredentials(account.id);
        onReauthorized();
        setResult({ status: "success", message: `${account.platform_name} · ${account.display_name} 已重新授权` });
      } catch (err) {
        onReauthorized();
        setResult({ status: "error", message: err instanceof Error ? err.message : "凭据验证失败" });
      }
    } catch (err) {
      toast(err instanceof Error ? err.message : "保存凭据失败", "error");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="modalBackdrop" role="presentation" onClick={onClose}>
      <div className="addAuthDialog" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <div className="addAuthHeader">
          <span className="addAuthTitle">{result ? (result.status === "success" ? "授权成功" : "授权失败") : "重新授权"}</span>
          <button type="button" className="addAuthClose" onClick={onClose}>
            <X size={20} />
          </button>
        </div>

        {!result ? (
          <>
            <div className="addAuthStep">{account.platform_name} · 重新填写凭据</div>
            <div className="addAuthBody">
              <div className="addAuthField">
                <div className="addAuthLabel">AppID</div>
                <input
                  className="addAuthInput"
                  placeholder="填写公众号 AppID"
                  value={appId}
                  onChange={(e) => setAppId(e.target.value)}
                />
              </div>
              <div className="addAuthField">
                <div className="addAuthLabel">AppSecret</div>
                <input
                  className="addAuthInput"
                  placeholder="填写公众号 AppSecret"
                  type="password"
                  value={appSecret}
                  onChange={(e) => setAppSecret(e.target.value)}
                />
              </div>
            </div>
            <div className="addAuthFooter">
              <button type="button" className="secondaryButton" onClick={onClose}>取消</button>
              <button
                type="button"
                className="primaryButton"
                disabled={submitting || !appId.trim() || !appSecret.trim()}
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
                  cursor: submitting ? "not-allowed" : "pointer",
                  opacity: submitting ? 0.6 : 1,
                }}
                onClick={() => void handleSubmit()}
              >
                {submitting ? (
                  <LoaderCircle size={15} className="spin" />
                ) : (
                  <>
                    验证并保存
                    <ArrowRight size={16} />
                  </>
                )}
              </button>
            </div>
          </>
        ) : (
          <>
            <div className="addAuthBody addAuthSuccessBody">
              <div
                style={{
                  width: 64,
                  height: 64,
                  borderRadius: "50%",
                  background: result.status === "success" ? "var(--green-soft)" : "var(--red-soft)",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                }}
              >
                {result.status === "success" ? (
                  <Check size={32} style={{ color: "var(--green)" }} />
                ) : (
                  <X size={32} style={{ color: "var(--red)" }} />
                )}
              </div>
              <div style={{ fontSize: 16, fontWeight: 600, color: "var(--fg)" }}>
                {result.status === "success" ? "授权成功" : "授权失败"}
              </div>
              <div style={{ fontSize: 13, color: "var(--fg-2)", textAlign: "center" }}>{result.message}</div>
            </div>
            <div className="addAuthFooter">
              <button type="button" className="secondaryButton" onClick={onClose}>关闭</button>
              {result.status === "error" && (
                <button
                  type="button"
                  className="primaryButton"
                  onClick={() => setResult(null)}
                  style={{
                    background: "#4C6EF5",
                    fontSize: 13.5,
                    fontWeight: 600,
                    borderRadius: 9,
                    padding: "10px 20px",
                    color: "#fff",
                    border: "none",
                  }}
                >
                  重试
                </button>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
