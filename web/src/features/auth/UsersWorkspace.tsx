import { useEffect, useState } from "react";
import { RefreshCw, UserPlus } from "lucide-react";
import { api } from "../../api/client";
import { useToast } from "../../components/Toast";
import { Modal } from "../../components/Modal";
import { useAuth } from "./AuthContext";
import { formatDateTime } from "../../utils/dateFormat";
import type { UserRecord } from "../../types";

export function UsersWorkspace() {
  const { user: currentUser } = useAuth();
  const { toast } = useToast();
  const [users, setUsers] = useState<UserRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [resetTarget, setResetTarget] = useState<UserRecord | null>(null);

  async function loadUsers() {
    setLoading(true);
    try {
      const data = await api<UserRecord[]>("/api/auth/users");
      setUsers(data);
    } catch (err) {
      toast(err instanceof Error ? err.message : "加载用户失败", "error");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadUsers();
  }, []);

  async function toggleActive(u: UserRecord) {
    try {
      await api(`/api/auth/users/${u.id}`, {
        method: "PATCH",
        body: JSON.stringify({ is_active: !u.is_active }),
      });
      toast(u.is_active ? `已禁用 ${u.username}` : `已启用 ${u.username}`, "success");
      void loadUsers();
    } catch (err) {
      toast(err instanceof Error ? err.message : "操作失败", "error");
    }
  }

  return (
    <>
      <header className="topbar">
        <div>
          <p className="eyebrow">系统管理</p>
          <h1>用户管理</h1>
        </div>
        <div className="topActions">
          <button className="secondaryButton" type="button" disabled={loading} onClick={() => void loadUsers()}>
            <RefreshCw size={15} />
            刷新
          </button>
          <button className="primaryButton" type="button" onClick={() => setShowCreateModal(true)}>
            <UserPlus size={15} />
            新建用户
          </button>
        </div>
      </header>

      <div className="panel" style={{ padding: 0, overflow: "hidden" }}>
        {loading ? (
          <p style={{ padding: 24, color: "#64748b" }}>加载中…</p>
        ) : users.length === 0 ? (
          <p style={{ padding: 24, color: "#64748b" }}>暂无用户</p>
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ borderBottom: "1px solid var(--border)", background: "var(--bg-alt)" }}>
                <th style={thStyle}>用户名</th>
                <th style={thStyle}>角色</th>
                <th style={thStyle}>状态</th>
                <th style={thStyle}>最后登录</th>
                <th style={thStyle}>操作</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => {
                const isSelf = currentUser?.id === u.id;
                return (
                  <tr key={u.id} style={{ borderBottom: "1px solid var(--border)" }}>
                    <td style={tdStyle}>
                      <span style={{ fontWeight: 500 }}>{u.username}</span>
                      {u.must_change_password && (
                        <span className="badge failed" style={{ marginLeft: 6, fontSize: 11 }}>需改密</span>
                      )}
                    </td>
                    <td style={tdStyle}>
                      <span className={`badge ${u.role === "admin" ? "running" : "pending"}`}>
                        {u.role === "admin" ? "管理员" : "操作员"}
                      </span>
                    </td>
                    <td style={tdStyle}>
                      <span className={`badge ${u.is_active ? "succeeded" : "failed"}`}>
                        {u.is_active ? "启用" : "禁用"}
                      </span>
                    </td>
                    <td style={tdStyle}>
                      {formatDateTime(u.last_login_at)}
                    </td>
                    <td style={tdStyle}>
                      {isSelf ? (
                        <span style={{ color: "#94a3b8", fontSize: 12 }}>（当前账号）</span>
                      ) : (
                        <div style={{ display: "flex", gap: 8 }}>
                          <button
                            className="secondaryButton"
                            type="button"
                            style={{ fontSize: 12, padding: "3px 10px" }}
                            onClick={() => void toggleActive(u)}
                          >
                            {u.is_active ? "禁用" : "启用"}
                          </button>
                          <button
                            className="secondaryButton"
                            type="button"
                            style={{ fontSize: 12, padding: "3px 10px" }}
                            onClick={() => setResetTarget(u)}
                          >
                            重置密码
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {showCreateModal && (
        <CreateUserModal
          onClose={() => setShowCreateModal(false)}
          onCreated={() => {
            setShowCreateModal(false);
            void loadUsers();
          }}
        />
      )}

      {resetTarget && (
        <ResetPasswordModal
          user={resetTarget}
          onClose={() => setResetTarget(null)}
          onReset={() => {
            setResetTarget(null);
            void loadUsers();
          }}
        />
      )}
    </>
  );
}

const thStyle: React.CSSProperties = {
  padding: "10px 16px",
  textAlign: "left",
  fontWeight: 600,
  color: "#64748b",
  fontSize: 12,
  textTransform: "uppercase",
  letterSpacing: "0.04em",
};

const tdStyle: React.CSSProperties = {
  padding: "12px 16px",
  verticalAlign: "middle",
};

function CreateUserModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const { toast } = useToast();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<"operator" | "admin">("operator");
  const [saving, setSaving] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!username.trim() || !password.trim()) {
      toast("用户名和密码不能为空", "error");
      return;
    }
    if (password.length < 8) {
      toast("初始密码长度至少 8 位", "error");
      return;
    }
    setSaving(true);
    try {
      await api("/api/auth/users", {
        method: "POST",
        body: JSON.stringify({ username: username.trim(), password, role }),
      });
      toast(`用户 ${username.trim()} 已创建`, "success");
      onCreated();
    } catch (err) {
      toast(err instanceof Error ? err.message : "创建失败", "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      title="新建用户"
      onClose={onClose}
      footer={
        <>
          <button className="secondaryButton" type="button" onClick={onClose}>取消</button>
          <button className="primaryButton" type="submit" form="create-user-form" disabled={saving}>
            {saving ? "创建中…" : "创建"}
          </button>
        </>
      }
    >
      <form id="create-user-form" onSubmit={(e) => void handleSubmit(e)} style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <label style={labelStyle}>
          用户名
          <input
            className="input"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="输入用户名"
            autoFocus
          />
        </label>
        <label style={labelStyle}>
          初始密码
          <input
            className="input"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="输入初始密码 (至少8位)"
          />
        </label>
        <label style={labelStyle}>
          角色
          <select className="input" value={role} onChange={(e) => setRole(e.target.value as "operator" | "admin")}>
            <option value="operator">操作员</option>
            <option value="admin">管理员</option>
          </select>
        </label>
        <p style={{ fontSize: 12, color: "#64748b", margin: 0 }}>创建后用户需在首次登录时修改密码</p>
      </form>
    </Modal>
  );
}

function ResetPasswordModal({ user, onClose, onReset }: { user: UserRecord; onClose: () => void; onReset: () => void }) {
  const { toast } = useToast();
  const [newPassword, setNewPassword] = useState("");
  const [saving, setSaving] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!newPassword.trim()) {
      toast("新密码不能为空", "error");
      return;
    }
    if (newPassword.length < 8) {
      toast("新密码长度至少 8 位", "error");
      return;
    }
    setSaving(true);
    try {
      await api(`/api/auth/users/${user.id}/reset-password`, {
        method: "POST",
        body: JSON.stringify({ new_password: newPassword }),
      });
      toast(`${user.username} 的密码已重置`, "success");
      onReset();
    } catch (err) {
      toast(err instanceof Error ? err.message : "重置失败", "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      title={`重置密码 — ${user.username}`}
      onClose={onClose}
      footer={
        <>
          <button className="secondaryButton" type="button" onClick={onClose}>取消</button>
          <button className="primaryButton" type="submit" form="reset-password-form" disabled={saving}>
            {saving ? "重置中…" : "重置密码"}
          </button>
        </>
      }
    >
      <form id="reset-password-form" onSubmit={(e) => void handleSubmit(e)} style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <label style={labelStyle}>
          新密码
          <input
            className="input"
            type="password"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            placeholder="输入新密码 (至少8位)"
            autoFocus
          />
        </label>
        <p style={{ fontSize: 12, color: "#64748b", margin: 0 }}>重置后用户需在下次登录时修改密码</p>
      </form>
    </Modal>
  );
}

const labelStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 6,
  fontSize: 13,
  fontWeight: 500,
};
