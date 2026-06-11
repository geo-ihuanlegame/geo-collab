import { useState, useEffect, useRef } from "react";
import { Check, ChevronDown, ChevronUp, LoaderCircle, X, Search, ArrowRight, ExternalLink, Camera, AppWindow } from "lucide-react";
import type { PlatformOption } from "../../types";
import { createApiAccount, verifyCredentials, startPlatformLoginSession, startAccountLoginSession, pollLoginSessionUntilActive, finishAccountLoginSession } from "../../api/accounts";
import { uploadAsset } from "../../api/assets";
import { assetSrc } from "../../api/core";
import { useToast } from "../../components/Toast";
import { openRemoteBrowser } from "../../utils/remoteBrowser";

export function AddAuthorizationDialog({
  platforms,
  onClose,
  onCreated,
}: {
  platforms: PlatformOption[];
  onClose: () => void;
  onCreated: () => void;
}) {
  const { toast } = useToast();
  const [step, setStep] = useState<1 | 2 | "result">(1);
  const [selectedPlatform, setSelectedPlatform] = useState<PlatformOption | null>(null);
  const [platformOpen, setPlatformOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");

  const [displayName, setDisplayName] = useState("");
  const [contact, setContact] = useState("");
  const [note, setNote] = useState("");
  const [distributionEnabled, setDistributionEnabled] = useState(true);
  const [appId, setAppId] = useState("");
  const [appSecret, setAppSecret] = useState("");

  const [avatarAssetId, setAvatarAssetId] = useState<string | null>(null);
  const [avatarPreview, setAvatarPreview] = useState<string | null>(null);
  const [avatarUploading, setAvatarUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [verifying, setVerifying] = useState(false);
  const [createdAccountId, setCreatedAccountId] = useState<number | null>(null);
  const [loginSessionId, setLoginSessionId] = useState<string | null>(null);
  const [loginNovncUrl, setLoginNovncUrl] = useState<string | null>(null);
  const [loginSessionError, setLoginSessionError] = useState<string | null>(null);

  const [resultStatus, setResultStatus] = useState<"success" | "error">("success");
  const [resultMessage, setResultMessage] = useState("");

  const pollingActiveRef = useRef(false);
  const finishRequestedRef = useRef(false);

  const filteredPlatforms = platforms.filter(
    (p) => !searchQuery || p.name.includes(searchQuery),
  );

  // API 型平台（如微信公众号）凭据直填；其余（含 browser / 未标记）走浏览器扫码登录。
  // 用后端下发的能力位 mode 判定，避免硬编码具体平台 code。
  const isApiPlatform = selectedPlatform?.mode === "api";

  function reset() {
    setStep(1);
    setSelectedPlatform(null);
    setPlatformOpen(false);
    setSearchQuery("");
    setDisplayName("");
    setContact("");
    setNote("");
    setDistributionEnabled(true);
    setAppId("");
    setAppSecret("");
    setAvatarAssetId(null);
    setAvatarPreview(null);
    setAvatarUploading(false);
    setVerifying(false);
    setCreatedAccountId(null);
    setLoginSessionId(null);
    setLoginNovncUrl(null);
    setLoginSessionError(null);
    setResultStatus("success");
    setResultMessage("");
    pollingActiveRef.current = false;
    finishRequestedRef.current = false;
  }

  function selectPlatform(p: PlatformOption) {
    setSelectedPlatform(p);
    setPlatformOpen(false);
  }

  async function handleAvatarChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = ""; // 允许重选同一文件
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      toast("请选择图片文件", "error");
      return;
    }
    setAvatarUploading(true);
    try {
      const asset = await uploadAsset(file);
      setAvatarAssetId(asset.id);
      setAvatarPreview(URL.createObjectURL(file));
    } catch (err) {
      toast(err instanceof Error ? err.message : "头像上传失败", "error");
    } finally {
      setAvatarUploading(false);
    }
  }

  function handleClose() {
    pollingActiveRef.current = false;
    reset();
    onClose();
  }

  function handleBack() {
    pollingActiveRef.current = false;
    setLoginSessionId(null);
    setLoginNovncUrl(null);
    setLoginSessionError(null);
    setStep(1);
  }

  async function handleSubmit() {
    if (!displayName.trim()) {
      toast("请填写账号名称", "error");
      return;
    }
    if (!selectedPlatform) {
      toast("请选择平台", "error");
      return;
    }
    if (isApiPlatform && (!appId.trim() || !appSecret.trim())) {
      toast("请填写 AppID 和 AppSecret", "error");
      return;
    }

    // API 型平台（如微信公众号）：凭据直填后建号 + 验证凭据，无浏览器登录。
    if (isApiPlatform) {
      setVerifying(true);
      try {
        const account = await createApiAccount({
          platform_code: selectedPlatform.code,
          display_name: displayName.trim(),
          contact: contact.trim() || null,
          note: note.trim() || null,
          distribution_enabled: distributionEnabled,
          avatar_asset_id: avatarAssetId,
          api_credentials: { app_id: appId.trim(), app_secret: appSecret.trim() },
        });
        setCreatedAccountId(account.id);
        try {
          await verifyCredentials(account.id);
          onCreated();
          setResultStatus("success");
          setResultMessage(`${selectedPlatform.name} · ${displayName.trim()} 已加入矩阵`);
        } catch (err) {
          setResultStatus("error");
          setResultMessage(err instanceof Error ? err.message : "凭据验证失败");
        }
        setStep("result");
      } catch (err) {
        toast(err instanceof Error ? err.message : "创建账号失败", "error");
      } finally {
        setVerifying(false);
      }
      return;
    }

    // 浏览器登录平台（头条等）：不走 POST /api/accounts（那个端点强制要 api_credentials），
    // 直接进第 2 步用 /login-session 端点建号 + 起远程浏览器扫码会话。
    setStep(2);
  }

  // 第 2 步：浏览器平台的扫码登录会话。建号 + 起会话 → 轮询到 active 后打开 noVNC 窗口，
  // 之后等用户在窗口里完成登录、手动点「我已完成登录」(handleFinishLogin)，不自动收尾。
  useEffect(() => {
    if (step !== 2 || !selectedPlatform) return;

    let cancelled = false;
    pollingActiveRef.current = true;

    async function init() {
      try {
        const session = await startPlatformLoginSession(selectedPlatform!.code, {
          display_name: displayName.trim(),
          account_key: "",
          use_browser: true,
          contact: contact.trim() || null,
          note: note.trim() || null,
          distribution_enabled: distributionEnabled,
          avatar_asset_id: avatarAssetId,
        });
        if (cancelled) return;
        setCreatedAccountId(session.account.id);
        setLoginSessionId(session.session_id);
        onCreated();

        try {
          const active = await pollLoginSessionUntilActive(session.account.id, session.session_id);
          if (cancelled || !pollingActiveRef.current) return;
          if (active.novnc_url) {
            setLoginNovncUrl(active.novnc_url);
            openRemoteBrowser(active.novnc_url);
          }
          // 不自动 finish：等用户在 noVNC 窗口里登录后点「我已完成登录」。
        } catch (err) {
          if (cancelled) return;
          setLoginSessionError(err instanceof Error ? err.message : "启动登录会话失败");
        }
      } catch (err) {
        if (cancelled) return;
        setLoginSessionError(err instanceof Error ? err.message : "启动登录会话失败");
      }
    }

    init();

    return () => {
      cancelled = true;
      pollingActiveRef.current = false;
    };
    // 仅在进入第 2 步时跑一次：表单字段在第 1 步已定稿，刻意不进依赖，避免按键 / onCreated 引用变化重启会话。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, selectedPlatform]);

  async function handleFinishLogin() {
    if (!createdAccountId || !loginSessionId) return;
    if (finishRequestedRef.current) return;
    finishRequestedRef.current = true;
    pollingActiveRef.current = false;
    setVerifying(true);

    try {
      try {
        await pollLoginSessionUntilActive(createdAccountId, loginSessionId, 10_000);
      } catch {
        // timeout is fine — try to finish anyway
      }

      const result = await finishAccountLoginSession(createdAccountId, loginSessionId);
      if (result.logged_in) {
        onCreated();
        handleClose();
      } else {
        setLoginSessionError("登录未完成，请重试");
        finishRequestedRef.current = false;
      }
    } catch (err) {
      setLoginSessionError(err instanceof Error ? err.message : "确认登录失败");
      finishRequestedRef.current = false;
    } finally {
      setVerifying(false);
    }
  }

  function handleReopen() {
    if (loginNovncUrl) {
      openRemoteBrowser(loginNovncUrl);
    } else if (createdAccountId) {
      (async () => {
        try {
          const session = await startAccountLoginSession(createdAccountId!, {});
          setLoginSessionId(session.session_id);
          setLoginNovncUrl(session.novnc_url);
          if (session.novnc_url) {
            openRemoteBrowser(session.novnc_url);
          }
          setLoginSessionError(null);
        } catch (err) {
          setLoginSessionError(err instanceof Error ? err.message : "启动登录会话失败");
        }
      })();
    }
  }

  return (
    <div className="modalBackdrop" role="presentation" onClick={handleClose}>
      <div
        className="addAuthDialog"
        role="dialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
      >
        {step === 1 && (
          <>
            <div className="addAuthHeader">
              <span className="addAuthTitle">添加账号</span>
              <button type="button" className="addAuthClose" onClick={handleClose}>
                <X size={20} />
              </button>
            </div>
            <div className="addAuthStep">第 1 步 / 共 2 步 · 填写账号信息</div>
            <div className="addAuthBody">
              <div className="addAuthField">
                <div className="addAuthLabel">选择平台</div>
                <div
                  className={`addAuthSelect${platformOpen ? " active" : ""}`}
                  onClick={() => setPlatformOpen(!platformOpen)}
                >
                  <div className="addAuthSelectLeft">
                    {selectedPlatform ? (
                      <>
                        <div className="addAuthSelectAvatar">{selectedPlatform.name.slice(0, 1)}</div>
                        <span>{selectedPlatform.name}</span>
                      </>
                    ) : (
                      <span style={{ color: "var(--fg-3)" }}>请选择平台</span>
                    )}
                  </div>
                  {platformOpen ? <ChevronUp size={18} /> : <ChevronDown size={18} />}
                </div>
                {platformOpen && (
                  <div className="addAuthDropdown">
                    <div className="addAuthDropdownSearch">
                      <Search size={15} />
                      <input
                        placeholder="搜索平台…"
                        value={searchQuery}
                        onChange={(e) => setSearchQuery(e.target.value)}
                        autoFocus
                      />
                    </div>
                    {filteredPlatforms.map((p) => (
                      <div
                        key={p.code}
                        className={`addAuthDropdownItem${selectedPlatform?.code === p.code ? " active" : ""}`}
                        onClick={() => selectPlatform(p)}
                      >
                        <div className="addAuthDropdownLeft">
                          <div className="addAuthSelectAvatar">{p.name.slice(0, 1)}</div>
                          <span>{p.name}</span>
                        </div>
                        {selectedPlatform?.code === p.code && <Check size={16} />}
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <div
                className="addAuthAvatarUpload"
                onClick={() => {
                  if (!avatarUploading) fileInputRef.current?.click();
                }}
              >
                {avatarUploading ? (
                  <LoaderCircle size={20} className="spin" />
                ) : avatarPreview || avatarAssetId ? (
                  <img
                    className="addAuthAvatarPreview"
                    src={avatarPreview ?? assetSrc(avatarAssetId) ?? undefined}
                    alt=""
                  />
                ) : (
                  <>
                    <Camera size={20} />
                    <span>上传</span>
                  </>
                )}
              </div>
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                style={{ display: "none" }}
                onChange={(e) => void handleAvatarChange(e)}
              />

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

              {isApiPlatform && (
                <div className="addAuthWeChatSection">
                  <div className="addAuthWeChatHeader">
                    <span style={{ fontSize: 12.5, fontWeight: 600, color: "var(--fg)" }}>公众号专属配置</span>
                  </div>
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
              )}
            </div>
            <div className="addAuthFooter">
              <button type="button" className="secondaryButton" onClick={handleClose}>取消</button>
              <button
                type="button"
                className="primaryButton"
                disabled={
                  verifying ||
                  !displayName.trim() ||
                  !selectedPlatform ||
                  (isApiPlatform && (!appId.trim() || !appSecret.trim()))
                }
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
                  cursor: verifying ? "not-allowed" : "pointer",
                  opacity: verifying ? 0.6 : 1,
                }}
                onClick={() => void handleSubmit()}
              >
                {verifying ? (
                  <LoaderCircle size={15} className="spin" />
                ) : (
                  <>
                    前往授权
                    <ArrowRight size={16} />
                  </>
                )}
              </button>
            </div>
          </>
        )}

        {step === 2 && (
          <>
            <div className="addAuthHeader">
              <span className="addAuthTitle">完成登录授权</span>
              <button type="button" className="addAuthClose" onClick={handleClose}>
                <X size={20} />
              </button>
            </div>
            <div className="addAuthStep">第 2 步 / 共 2 步 · 等待登录完成</div>
            <div className="addAuthBody">
              {selectedPlatform && createdAccountId && (
                <div className="addAuthContext">
                  正在授权 · {selectedPlatform.name} · {displayName}
                </div>
              )}
              <div className="addAuthCenter">
                <div className="addAuthScanIcon">
                  <AppWindow size={32} />
                </div>
                <div className="addAuthScanTitle">
                  已打开「{selectedPlatform?.name} · {displayName}」登录窗口
                </div>
                <div className="addAuthScanDesc">
                  请在新打开的窗口中完成扫码 / 登录，完成后系统会自动捕获登录态并加入矩阵。
                </div>
                <div className="addAuthWait">
                  <LoaderCircle size={16} className="spin" />
                  <span>等待登录完成…</span>
                </div>
                {loginSessionError && (
                  <div style={{ color: "var(--red)", fontSize: 12.5, marginTop: -8 }}>
                    {loginSessionError}
                  </div>
                )}
                <button type="button" className="addAuthReopen" onClick={handleReopen}>
                  <ExternalLink size={14} />
                  <span>没有弹出窗口？点此重新打开</span>
                </button>
              </div>
            </div>
            <div className="addAuthFooter">
              <button
                type="button"
                className="secondaryButton"
                style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
                onClick={handleBack}
              >
                上一步
              </button>
              <button
                type="button"
                className="primaryButton"
                disabled={verifying}
                style={{
                  background: "#4C6EF5",
                  fontSize: 13.5,
                  fontWeight: 600,
                  borderRadius: 9,
                  padding: "10px 20px",
                  color: "#fff",
                  border: "none",
                  cursor: verifying ? "not-allowed" : "pointer",
                  opacity: verifying ? 0.6 : 1,
                }}
                onClick={() => void handleFinishLogin()}
              >
                {verifying ? (
                  <LoaderCircle size={15} className="spin" />
                ) : (
                  "我已完成登录"
                )}
              </button>
            </div>
          </>
        )}

        {step === "result" && (
          <>
            <div className="addAuthHeader">
              <span className="addAuthTitle">{resultStatus === "success" ? "授权成功" : "授权失败"}</span>
              <button type="button" className="addAuthClose" onClick={handleClose}>
                <X size={20} />
              </button>
            </div>
            <div className="addAuthBody addAuthSuccessBody">
              {resultStatus === "success" ? (
                <>
                  <div
                    style={{
                      width: 64,
                      height: 64,
                      borderRadius: "50%",
                      background: "var(--green-soft)",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                    }}
                  >
                    <Check size={32} style={{ color: "var(--green)" }} />
                  </div>
                  <div style={{ fontSize: 16, fontWeight: 600, color: "var(--fg)" }}>授权成功</div>
                  <div style={{ fontSize: 13, color: "var(--fg-2)", textAlign: "center" }}>{resultMessage}</div>
                </>
              ) : (
                <>
                  <div
                    style={{
                      width: 64,
                      height: 64,
                      borderRadius: "50%",
                      background: "var(--red-soft)",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                    }}
                  >
                    <X size={32} style={{ color: "var(--red)" }} />
                  </div>
                  <div style={{ fontSize: 16, fontWeight: 600, color: "var(--fg)" }}>授权失败</div>
                  <div style={{ fontSize: 13, color: "var(--fg-2)", textAlign: "center" }}>{resultMessage}</div>
                </>
              )}
            </div>
            <div className="addAuthFooter">
              <button type="button" className="secondaryButton" onClick={handleClose}>关闭</button>
              {resultStatus === "error" && (
                <button
                  type="button"
                  className="primaryButton"
                  onClick={() => {
                    setStep(1);
                    setResultStatus("success");
                    setResultMessage("");
                  }}
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
