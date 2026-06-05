import { useRef, useState } from "react";
import { navItems } from "./types";
import type { NavKey } from "./types";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { ToastProvider } from "./components/Toast";
import { AgentManagementWorkspace } from "./features/pipelines/AgentManagementWorkspace";
import { AiGenerationWorkspace } from "./features/ai-generation/AiGenerationWorkspace";
import { ImageLibraryWorkspace } from "./features/image-library/ImageLibraryWorkspace";
import { ContentWorkspace } from "./features/content/ContentWorkspace";
import { PromptsWorkspace } from "./features/prompt-templates/PromptsWorkspace";
import { AccountsWorkspace } from "./features/accounts/AccountsWorkspace";
import { TasksWorkspace } from "./features/tasks/TasksWorkspace";
import { SystemWorkspace } from "./features/system/SystemWorkspace";
import { AuthProvider, useAuth } from "./features/auth/AuthContext";
import { LoginPage } from "./features/auth/LoginPage";
import { ChangePasswordPage } from "./features/auth/ChangePasswordPage";
import { UsersWorkspace } from "./features/auth/UsersWorkspace";
import { AuditLogsWorkspace } from "./features/system/AuditLogsWorkspace";
import { LogOut, ScrollText, Users } from "lucide-react";
import "./styles.css";

function AppShell() {
  const { user, loading, logout } = useAuth();
  const [activeNav, setActiveNav] = useState<NavKey>("content");
  const [visitedTabs, setVisitedTabs] = useState<Set<NavKey>>(new Set(["content"]));
  const contentDirtyRef = useRef<() => boolean>(() => false);

  function handleNavClick(key: NavKey) {
    if (activeNav === "content" && key !== "content" && contentDirtyRef.current()) {
      if (!window.confirm("当前文章有未保存内容，确定要切换页面吗？未保存的修改将丢失。")) return;
    }
    setVisitedTabs((prev) => (prev.has(key) ? prev : new Set(prev).add(key)));
    setActiveNav(key);
  }

  if (loading) {
    return (
      <div className="authShell">
        <div className="authCard">
          <p className="authLoading">加载中...</p>
        </div>
      </div>
    );
  }

  if (!user) {
    return <LoginPage />;
  }

  if (user.must_change_password) {
    return <ChangePasswordPage />;
  }

  return (
    <ToastProvider>
      <main className="shell">
        <aside className="sidebar">
          <div className="brand">
            <div className="brandMark" />
            <div className="brandBody">
              <span className="brandName">Geo</span>
              <span className="brandSub">协作平台</span>
            </div>
          </div>
          <nav className="nav">
            {navItems.map((item) => {
              const Icon = item.icon;
              return (
                <button
                  className={`navItem ${activeNav === item.key ? "active" : ""}`}
                  key={item.key}
                  type="button"
                  onClick={() => handleNavClick(item.key)}
                >
                  <Icon size={17} />
                  <span>{item.label}</span>
                  <span className="navDot" />
                </button>
              );
            })}
            {user.role === "admin" && (
              <button
                className={`navItem ${activeNav === "admin" ? "active" : ""}`}
                type="button"
                onClick={() => handleNavClick("admin")}
              >
                <Users size={17} />
                <span>用户管理</span>
                <span className="navDot" />
              </button>
            )}
            {user.role === "admin" && (
              <button
                className={`navItem ${activeNav === "audit-logs" ? "active" : ""}`}
                type="button"
                onClick={() => handleNavClick("audit-logs")}
              >
                <ScrollText size={17} />
                <span>审计日志</span>
                <span className="navDot" />
              </button>
            )}
          </nav>
          <div className="sidebarUser">
            <span className="sidebarUsername">{user.username}</span>
            <button className="sidebarLogoutBtn" type="button" onClick={logout} title="退出登录">
              <LogOut size={15} />
            </button>
          </div>
        </aside>
        <section className="workspace">
          <div className="workspaceInner">
            {visitedTabs.has("agents") && (
              <div style={{ display: activeNav === "agents" ? undefined : "none" }}>
                <ErrorBoundary fallback={<p role="alert">智能体管理出错，请刷新重试</p>}>
                  <AgentManagementWorkspace />
                </ErrorBoundary>
              </div>
            )}
            {visitedTabs.has("ai") && (
              <div style={{ display: activeNav === "ai" ? undefined : "none" }}>
                <ErrorBoundary fallback={<p role="alert">AI 生文出错，请刷新重试</p>}>
                  <AiGenerationWorkspace onNavigateToContent={() => handleNavClick("content")} />
                </ErrorBoundary>
              </div>
            )}
            <div style={{ display: activeNav === "content" ? undefined : "none" }}>
              <ErrorBoundary fallback={<p role="alert">内容管理出错，请刷新重试</p>}>
                <ContentWorkspace dirtyCheckRef={contentDirtyRef} isActive={activeNav === "content"} />
              </ErrorBoundary>
            </div>
            {visitedTabs.has("prompts") && (
              <div style={{ display: activeNav === "prompts" ? undefined : "none" }}>
                <ErrorBoundary fallback={<p role="alert">提示词管理出错，请刷新重试</p>}>
                  <PromptsWorkspace />
                </ErrorBoundary>
              </div>
            )}
            {visitedTabs.has("image-library") && (
              <div style={{ display: activeNav === "image-library" ? undefined : "none" }}>
                <ErrorBoundary fallback={<p role="alert">图片库出错，请刷新重试</p>}>
                  <ImageLibraryWorkspace />
                </ErrorBoundary>
              </div>
            )}
            {visitedTabs.has("media") && (
              <div style={{ display: activeNav === "media" ? undefined : "none" }}>
                <ErrorBoundary fallback={<p role="alert">媒体矩阵出错，请刷新重试</p>}>
                  <AccountsWorkspace isActive={activeNav === "media"} />
                </ErrorBoundary>
              </div>
            )}
            {visitedTabs.has("tasks") && (
              <div style={{ display: activeNav === "tasks" ? undefined : "none" }}>
                <ErrorBoundary fallback={<p role="alert">分发引擎出错，请刷新重试</p>}>
                  <TasksWorkspace isActive={activeNav === "tasks"} />
                </ErrorBoundary>
              </div>
            )}
            {visitedTabs.has("system") && (
              <div style={{ display: activeNav === "system" ? undefined : "none" }}>
                <ErrorBoundary fallback={<p role="alert">系统状态出错，请刷新重试</p>}>
                  <SystemWorkspace />
                </ErrorBoundary>
              </div>
            )}
            {user.role === "admin" && visitedTabs.has("admin") && (
              <div style={{ display: activeNav === "admin" ? undefined : "none" }}>
                <ErrorBoundary fallback={<p role="alert">用户管理出错，请刷新重试</p>}>
                  <UsersWorkspace />
                </ErrorBoundary>
              </div>
            )}
            {user.role === "admin" && visitedTabs.has("audit-logs") && (
              <div style={{ display: activeNav === "audit-logs" ? undefined : "none" }}>
                <ErrorBoundary fallback={<p role="alert">审计日志出错，请刷新重试</p>}>
                  <AuditLogsWorkspace />
                </ErrorBoundary>
              </div>
            )}
          </div>
        </section>
      </main>
    </ToastProvider>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <AppShell />
    </AuthProvider>
  );
}
