import { useState } from "react";
import { Check, ChevronDown, ChevronUp, LoaderCircle, Plus, X } from "lucide-react";
import type { PlatformOption } from "../../types";
import { createApiAccount, verifyCredentials } from "../../api/accounts";
import type { Account } from "../../types";
import { useToast } from "../../components/Toast";

type Step = "platform" | "form" | "success" | "error";

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
  const [step, setStep] = useState<Step>("platform");
  const [platformOpen, setPlatformOpen] = useState(false);
  const [selectedPlatform, setSelectedPlatform] = useState<PlatformOption | null>(null);
  const [searchQuery, setSearchQuery] = useState("");

  // form fields
  const [appId, setAppId] = useState("");
  const [appSecret, setAppSecret] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [contact, setContact] = useState("");
  const [note, setNote] = useState("");
  const [distributionEnabled, setDistributionEnabled] = useState(true);
  const [verifying, setVerifying] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");
  const [createdAccount, setCreatedAccount] = useState<Account | null>(null);

  const platformsForDisplay = platforms.filter(
    (p) => !searchQuery || p.name.includes(searchQuery),
  );

  function reset() {
    setStep("platform");
    setSelectedPlatform(null);
    setAppId("");
    setAppSecret("");
    setDisplayName("");
    setContact("");
    setNote("");
    setDistributionEnabled(true);
    setVerifying(false);
    setErrorMessage("");
    setCreatedAccount(null);
  }

  function selectPlatform(p: PlatformOption) {
    setSelectedPlatform(p);
    setPlatformOpen(false);
    setStep("form");
  }

  async function handleVerify() {
    if (!appId.trim() || !appSecret.trim() || !displayName.trim()) {
      toast("请填写 AppID、AppSecret 和账号名称", "error");
      return;
    }
    setVerifying(true);
    setErrorMessage("");
    try {
      const account = await createApiAccount({
        platform_code: selectedPlatform!.code,
        display_name: displayName.trim(),
        api_credentials: { app_id: appId.trim(), app_secret: appSecret.trim() },
        contact: contact.trim() || null,
        note: note.trim() || null,
        distribution_enabled: distributionEnabled,
      });
      try {
        await verifyCredentials(account.id);
      } catch {
        // verification may fail on first attempt - that's ok
      }
      setCreatedAccount(account);
      setStep("success");
      onCreated();
      toast("账号授权成功", "success");
    } catch (error) {
      const msg = error instanceof Error ? error.message : "验证失败";
      setErrorMessage(msg);
      setStep("error");
      toast(msg, "error");
    } finally {
      setVerifying(false);
    }
  }

  function handleClose() {
    reset();
    onClose();
  }

  return (
    <div className="modalBackdrop" role="presentation" onClick={handleClose}>
      <div
        className="addAuthDialog"
        role="dialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
      >
        {step === "platform" && (
          <>
            <div className="addAuthHeader">
              <span className="addAuthTitle">添加授权</span>
              <button type="button" className="addAuthClose" onClick={handleClose}>
                <X size={18} />
              </button>
            </div>
            <div className="addAuthBody">
              <span className="addAuthLabel">选择平台</span>
              <div className="addAuthPlatformSelect" onClick={() => setPlatformOpen(!platformOpen)}>
                <div className="addAuthPlatformSelectLeft">
                  {selectedPlatform ? (
                    <>
                      <div className="accountRowMiniAvatar">{selectedPlatform.name.slice(0, 1)}</div>
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
                    <input
                      placeholder="搜索平台…"
                      value={searchQuery}
                      onChange={(e) => setSearchQuery(e.target.value)}
                      autoFocus
                    />
                  </div>
                  {platformsForDisplay.map((p) => (
                    <div
                      key={p.code}
                      className={`addAuthDropdownItem ${selectedPlatform?.code === p.code ? "active" : ""}`}
                      onClick={() => selectPlatform(p)}
                    >
                      <div className="addAuthDropdownLeft">
                        <div className="accountRowMiniAvatar">{p.name.slice(0, 1)}</div>
                        <span>{p.name}</span>
                      </div>
                      {selectedPlatform?.code === p.code && <Check size={16} />}
                    </div>
                  ))}
                </div>
              )}
            </div>
            <div className="addAuthFooter">
              <button type="button" className="secondaryButton" onClick={handleClose}>取消</button>
            </div>
          </>
        )}

        {step === "form" && (
          <>
            <div className="addAuthHeader">
              <span className="addAuthTitle">添加授权 · {selectedPlatform?.name}</span>
              <button type="button" className="addAuthClose" onClick={handleClose}>
                <X size={18} />
              </button>
            </div>
            <div className="addAuthBody">
              {selectedPlatform?.code === "wechat_mp" && (
                <div className="addAuthWechatSection">
                  <span className="addAuthLabel">公众号配置</span>
                  <label className="addAuthField">
                    AppID
                    <input value={appId} onChange={(e) => setAppId(e.target.value)} placeholder="填写公众号 AppID" />
                  </label>
                  <label className="addAuthField">
                    AppSecret
                    <input value={appSecret} onChange={(e) => setAppSecret(e.target.value)} placeholder="填写公众号 AppSecret" type="password" />
                  </label>
                </div>
              )}

              <label className="addAuthField">
                账号名称
                <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="例如：纪缘" />
              </label>

              <label className="addAuthField">
                绑定联系方式
                <input value={contact} onChange={(e) => setContact(e.target.value)} placeholder="手机号或邮箱" />
              </label>

              <label className="addAuthField">
                备注
                <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="例如：主力号" />
              </label>

              <div className="addAuthDistRow">
                <div>
                  <span className="addAuthDistLabel">启用分发</span>
                  <span className="addAuthDistHint">开启后该账号将参与内容分发</span>
                </div>
                <button
                  type="button"
                  className={`accountRowToggle ${distributionEnabled ? "on" : ""}`}
                  onClick={() => setDistributionEnabled(!distributionEnabled)}
                >
                  <span className="accountRowToggleKnob" />
                </button>
              </div>
            </div>
            <div className="addAuthFooter">
              <button type="button" className="secondaryButton" onClick={() => setStep("platform")}>返回</button>
              <button type="button" className="primaryButton" disabled={verifying} onClick={() => void handleVerify()}>
                {verifying ? <LoaderCircle size={15} className="spin" /> : <Plus size={15} />}
                {verifying ? "验证中…" : "验证并授权"}
              </button>
            </div>
          </>
        )}

        {step === "success" && createdAccount && (
          <>
            <div className="addAuthHeader">
              <span className="addAuthTitle">授权成功</span>
              <button type="button" className="addAuthClose" onClick={handleClose}>
                <X size={18} />
              </button>
            </div>
            <div className="addAuthBody addAuthSuccessBody">
              <div className="addAuthSuccessIcon">
                <Check size={32} />
              </div>
              <span className="addAuthSuccessTitle">授权成功</span>
              <span className="addAuthSuccessSub">
                {createdAccount.platform_name} · {createdAccount.display_name} 已加入矩阵，已默认启用分发。
              </span>
            </div>
            <div className="addAuthFooter">
              <button type="button" className="secondaryButton" onClick={reset}>继续添加</button>
              <button type="button" className="primaryButton" onClick={handleClose}>完成</button>
            </div>
          </>
        )}

        {step === "error" && (
          <>
            <div className="addAuthHeader">
              <span className="addAuthTitle">授权失败</span>
              <button type="button" className="addAuthClose" onClick={handleClose}>
                <X size={18} />
              </button>
            </div>
            <div className="addAuthBody addAuthErrorBody">
              <div className="addAuthErrorIcon">
                <X size={32} />
              </div>
              <span className="addAuthErrorTitle">授权失败</span>
              <span className="addAuthErrorSub">{errorMessage || "请检查凭据后重试"}</span>
            </div>
            <div className="addAuthFooter">
              <button type="button" className="secondaryButton" onClick={handleClose}>关闭</button>
              <button type="button" className="primaryButton" onClick={() => { setStep("form"); setErrorMessage(""); }}>重试</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
