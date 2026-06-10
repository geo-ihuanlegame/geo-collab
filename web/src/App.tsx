import { lazy, Suspense, useRef, useState } from "react";
import { navItems } from "./types";
import type { NavKey } from "./types";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { ToastProvider } from "./components/Toast";
import { GlobalErrorListener } from "./components/GlobalErrorListener";
import { AuthProvider, useAuth } from "./features/auth/AuthContext";
import { LoginPage } from "./features/auth/LoginPage";
import { ChangePasswordPage } from "./features/auth/ChangePasswordPage";
import { LogOut, ScrollText, Users } from "lucide-react";
import "./styles.css";

const AgentManagementWorkspace = lazy(() =>
  import("./features/pipelines/AgentManagementWorkspace").then((m) => ({ default: m.AgentManagementWorkspace })),
);
const AiGenerationWorkspace = lazy(() =>
  import("./features/ai-generation/AiGenerationWorkspace").then((m) => ({ default: m.AiGenerationWorkspace })),
);
const ImageLibraryWorkspace = lazy(() =>
  import("./features/image-library/ImageLibraryWorkspace").then((m) => ({ default: m.ImageLibraryWorkspace })),
);
const ContentWorkspace = lazy(() =>
  import("./features/content/ContentWorkspace").then((m) => ({ default: m.ContentWorkspace })),
);
const PromptsWorkspace = lazy(() =>
  import("./features/prompt-templates/PromptsWorkspace").then((m) => ({ default: m.PromptsWorkspace })),
);
const AccountsWorkspace = lazy(() =>
  import("./features/accounts/AccountsWorkspace").then((m) => ({ default: m.AccountsWorkspace })),
);
const TasksWorkspace = lazy(() =>
  import("./features/tasks/TasksWorkspace").then((m) => ({ default: m.TasksWorkspace })),
);
const SystemWorkspace = lazy(() =>
  import("./features/system/SystemWorkspace").then((m) => ({ default: m.SystemWorkspace })),
);
const HotListsWorkspace = lazy(() =>
  import("./features/hot-lists/HotListsWorkspace").then((m) => ({ default: m.HotListsWorkspace })),
);
const UsersWorkspace = lazy(() =>
  import("./features/auth/UsersWorkspace").then((m) => ({ default: m.UsersWorkspace })),
);
const AuditLogsWorkspace = lazy(() =>
  import("./features/system/AuditLogsWorkspace").then((m) => ({ default: m.AuditLogsWorkspace })),
);

function TabFallback() {
  return (
    <p className="emptyText" style={{ padding: 24 }}>
      页面加载中...
    </p>
  );
}

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
      <GlobalErrorListener />
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
                <ErrorBoundary title="智能体管理">
                  <Suspense fallback={<TabFallback />}>
                    <AgentManagementWorkspace />
                  </Suspense>
                </ErrorBoundary>
              </div>
            )}
            {visitedTabs.has("ai") && (
              <div style={{ display: activeNav === "ai" ? undefined : "none" }}>
                <ErrorBoundary title="AI 生文">
                  <Suspense fallback={<TabFallback />}>
                    <AiGenerationWorkspace onNavigateToContent={() => handleNavClick("content")} />
                  </Suspense>
                </ErrorBoundary>
              </div>
            )}
            <div style={{ display: activeNav === "content" ? undefined : "none" }}>
              <ErrorBoundary title="内容管理">
                <Suspense fallback={<TabFallback />}>
                  <ContentWorkspace dirtyCheckRef={contentDirtyRef} isActive={activeNav === "content"} />
                </Suspense>
              </ErrorBoundary>
            </div>
            {visitedTabs.has("prompts") && (
              <div style={{ display: activeNav === "prompts" ? undefined : "none" }}>
                <ErrorBoundary title="提示词管理">
                  <Suspense fallback={<TabFallback />}>
                    <PromptsWorkspace />
                  </Suspense>
                </ErrorBoundary>
              </div>
            )}
            {visitedTabs.has("image-library") && (
              <div style={{ display: activeNav === "image-library" ? undefined : "none" }}>
                <ErrorBoundary title="图片库">
                  <Suspense fallback={<TabFallback />}>
                    <ImageLibraryWorkspace />
                  </Suspense>
                </ErrorBoundary>
              </div>
            )}
            {visitedTabs.has("media") && (
              <div style={{ display: activeNav === "media" ? undefined : "none" }}>
                <ErrorBoundary title="媒体矩阵">
                  <Suspense fallback={<TabFallback />}>
                    <AccountsWorkspace isActive={activeNav === "media"} />
                  </Suspense>
                </ErrorBoundary>
              </div>
            )}
            {visitedTabs.has("tasks") && (
              <div style={{ display: activeNav === "tasks" ? undefined : "none" }}>
                <ErrorBoundary title="分发引擎">
                  <Suspense fallback={<TabFallback />}>
                    <TasksWorkspace isActive={activeNav === "tasks"} />
                  </Suspense>
                </ErrorBoundary>
              </div>
            )}
            {visitedTabs.has("system") && (
              <div style={{ display: activeNav === "system" ? undefined : "none" }}>
                <ErrorBoundary title="系统状态">
                  <Suspense fallback={<TabFallback />}>
                    <SystemWorkspace />
                  </Suspense>
                </ErrorBoundary>
              </div>
            )}
            {visitedTabs.has("hot-lists") && (
              <div style={{ display: activeNav === "hot-lists" ? undefined : "none" }}>
                <ErrorBoundary title="热榜">
                  <Suspense fallback={<TabFallback />}>
                    <HotListsWorkspace />
                  </Suspense>
                </ErrorBoundary>
              </div>
            )}
            {user.role === "admin" && visitedTabs.has("admin") && (
              <div style={{ display: activeNav === "admin" ? undefined : "none" }}>
                <ErrorBoundary title="用户管理">
                  <Suspense fallback={<TabFallback />}>
                    <UsersWorkspace />
                  </Suspense>
                </ErrorBoundary>
              </div>
            )}
            {user.role === "admin" && visitedTabs.has("audit-logs") && (
              <div style={{ display: activeNav === "audit-logs" ? undefined : "none" }}>
                <ErrorBoundary title="审计日志">
                  <Suspense fallback={<TabFallback />}>
                    <AuditLogsWorkspace />
                  </Suspense>
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
