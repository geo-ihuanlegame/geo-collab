import { useState } from "react";
import { useAuth } from "./AuthContext";

export function LoginPage() {
  const { login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setSubmitting(true);
    try {
      await login(username, password);
    } catch (err) {
      if (err instanceof TypeError) {
        setError("网络错误");
      } else if (err instanceof Error) {
        const msg = err.message.toLowerCase();
        if (msg.includes("invalid credentials")) {
          setError("用户名或密码错误");
        } else if (msg.includes("disabled") || msg.includes("account disabled")) {
          setError("账号已被禁用");
        } else {
          setError(err.message);
        }
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
            <div className="authBrandSub">协同发布平台</div>
          </div>
        </div>
        <form className="authForm" onSubmit={handleSubmit}>
          <input
            className="authInput"
            type="text"
            placeholder="用户名"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
            required
          />
          <input
            className="authInput"
            type="password"
            placeholder="密码"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
          {error && <div className="authError">{error}</div>}
          <button className="authSubmit" type="submit" disabled={submitting}>
            {submitting ? "登录中..." : "登录"}
          </button>
        </form>
      </div>
    </div>
  );
}
