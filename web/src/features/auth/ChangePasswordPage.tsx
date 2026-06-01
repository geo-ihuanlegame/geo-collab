import { useState } from "react";
import { useAuth } from "./AuthContext";

export function ChangePasswordPage() {
  const { changePassword, logout } = useAuth();
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");

    if (newPassword.length < 8) {
      setError("新密码长度至少 8 位");
      return;
    }
    if (newPassword !== confirmPassword) {
      setError("两次输入的密码不一致");
      return;
    }

    setSubmitting(true);
    try {
      await changePassword(oldPassword, newPassword);
    } catch (err) {
      if (err instanceof TypeError) {
        setError("网络错误");
      } else if (err instanceof Error) {
        setError(err.message);
      } else {
        setError("网络错误");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="authShell">
      <div className="authCard">
        <div className="authBrand">
          <div className="authBrandMark">G</div>
          <div>
            <div className="authBrandName">GeoCollab</div>
            <div className="authBrandSub">修改密码</div>
          </div>
        </div>
        <form className="authForm" onSubmit={handleSubmit}>
          <input
            className="authInput"
            type="password"
            placeholder="旧密码"
            value={oldPassword}
            onChange={(e) => setOldPassword(e.target.value)}
            autoFocus
            required
          />
          <input
            className="authInput"
            type="password"
            placeholder="新密码 (至少8位)"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            required
          />
          <input
            className="authInput"
            type="password"
            placeholder="确认新密码"
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            required
          />
          {error && <div className="authError">{error}</div>}
          <button className="authSubmit" type="submit" disabled={submitting}>
            {submitting ? "提交中..." : "修改密码"}
          </button>
          <button type="button" className="authAltButton" onClick={logout}>
            退出登录
          </button>
        </form>
      </div>
    </div>
  );
}
