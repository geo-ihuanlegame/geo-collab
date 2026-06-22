import { lazy, Suspense, useRef, useState } from "react";
import { navItems } from "./types";
import type { NavKey, PromptScope, ReviewStatus } from "./types";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { ToastProvider } from "./components/Toast";
import { GlobalErrorListener } from "./components/GlobalErrorListener";
import { AuthProvider, useAuth } from "./features/auth/AuthContext";
import { LoginPage } from "./features/auth/LoginPage";
import { ChangePasswordPage } from "./features/auth/ChangePasswordPage";
import { ChevronDown, ChevronLeft, Cpu, LogOut, ScrollText, Users } from "lucide-react";
import { MobileNav } from "./components/MobileNav";
import { MobileMorePage } from "./components/MobileMorePage";
import { ScrollPanel } from "./components/ScrollPanel";
import { useIsMobile } from "./hooks/useIsMobile";
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
const McpConnectWorkspace = lazy(() =>
  import("./features/mcp/McpConnectWorkspace").then((m) => ({ default: m.McpConnectWorkspace })),
);
const UsersWorkspace = lazy(() =>
  import("./features/auth/UsersWorkspace").then((m) => ({ default: m.UsersWorkspace })),
);
const AuditLogsWorkspace = lazy(() =>
  import("./features/system/AuditLogsWorkspace").then((m) => ({ default: m.AuditLogsWorkspace })),
);
const AiModelsWorkspace = lazy(() =>
  import("./features/system/AiModelsWorkspace").then((m) => ({ default: m.AiModelsWorkspace })),
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
  const isMobile = useIsMobile();
  const [moreOpen, setMoreOpen] = useState(false);
  const [activeNav, setActiveNav] = useState<NavKey>("agents");
  // 当前页是否属于「更多」分区（即不在底栏 4 个高频入口中）
  const onMoreSection = !(["agents", "ai", "content", "tasks"] as NavKey[]).includes(activeNav);
  const [visitedTabs, setVisitedTabs] = useState<Set<NavKey>>(new Set(["agents"]));
  const contentDirtyRef = useRef<() => boolean>(() => false);
  const [openGroup, setOpenGroup] = useState<NavKey | null>(null);
  const [contentReviewTab, setContentReviewTab] = useState<ReviewStatus>("pending");
  const [promptsScope, setPromptsScope] = useState<PromptScope>("generation");

  function handleNavClick(key: NavKey) {
    if (activeNav === "content" && key !== "content" && contentDirtyRef.current()) {
      if (!window.confirm("当前文章有未保存内容，确定要切换页面吗？未保存的修改将丢失。")) return;
    }
    setVisitedTabs((prev) => (prev.has(key) ? prev : new Set(prev).add(key)));
    setActiveNav(key);
    const hasChildren = navItems.some((i) => i.key === key && (i.children?.length ?? 0) > 0);
    if (!hasChildren) setOpenGroup(null);
  }

  function toggleGroup(key: NavKey) {
    setOpenGroup((prev) => (prev === key ? null : key));
  }
  function childValueFor(parentKey: NavKey): string {
    if (parentKey === "content") return contentReviewTab;
    if (parentKey === "prompts") return promptsScope;
    return "";
  }
  function selectChild(parentKey: NavKey, value: string) {
    if (parentKey === "content") setContentReviewTab(value as ReviewStatus);
    else if (parentKey === "prompts") setPromptsScope(value as PromptScope);
    setOpenGroup(parentKey);
    handleNavClick(parentKey);
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
      {/* 必须挂在 ToastProvider 内（useToast 的 context 默认值是 no-op）；登录页无 toast 面板，自带内联报错 */}
      <GlobalErrorListener />
      <main className={`shell${isMobile ? " shellMobile" : ""}`}>
        {isMobile && (
          <header className="mobileAppBar">
            {onMoreSection ? (
              <button type="button" className="mobileAppBack" onClick={() => setMoreOpen(true)}>
                <ChevronLeft size={22} />
                <span>更多</span>
              </button>
            ) : (
              <>
                <div className="brandMark">AI</div>
                <span className="mobileAppName">AI智能体平台</span>
              </>
            )}
          </header>
        )}
        <aside className="sidebar">
          <div className="brand">
            <div className="brandMark">AI</div>
            <span className="brandName">AI智能体平台</span>
          </div>
          <nav className="nav">
            {navItems.map((item) => {
              const Icon = item.icon;
              if (item.children && item.children.length > 0) {
                const isOpen = openGroup === item.key;
                return (
                  <div className="navGroup" key={item.key}>
                    <button
                      className={`navItem navParent ${activeNav === item.key ? "active" : ""}`}
                      type="button"
                      onClick={() => {
                        if (activeNav === item.key) {
                          toggleGroup(item.key);
                        } else {
                          handleNavClick(item.key);
                          setOpenGroup(item.key);
                        }
                      }}
                    >
                      <Icon size={17} />
                      <span>{item.label}</span>
                      <ChevronDown size={15} className={`navChevron${isOpen ? " open" : ""}`} />
                    </button>
                    <div className={`navSub ${isOpen ? "open" : ""}`}>
                      <div className="navChildren">
                        <div className="navChildrenInner">
                          {item.children.map((child) => {
                            const childActive =
                              activeNav === item.key && childValueFor(item.key) === child.value;
                            return (
                              <button
                                className={`navChild ${childActive ? "active" : ""}`}
                                key={child.key}
                                type="button"
                                onClick={() => selectChild(item.key, child.value)}
                              >
                                {child.label}
                              </button>
                            );
                          })}
                        </div>
                      </div>
                    </div>
                  </div>
                );
              }
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
            {user.role === "admin" && (
              <button
                className={`navItem ${activeNav === "ai-models" ? "active" : ""}`}
                type="button"
                onClick={() => handleNavClick("ai-models")}
              >
                <Cpu size={17} />
                <span>AI 模型</span>
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
              <ScrollPanel id="agents" active={activeNav === "agents"}>
                <ErrorBoundary title="智能体管理">
                  <Suspense fallback={<TabFallback />}>
                    <AgentManagementWorkspace />
                  </Suspense>
                </ErrorBoundary>
              </ScrollPanel>
            )}
            {visitedTabs.has("ai") && (
              <ScrollPanel id="ai" active={activeNav === "ai"}>
                <ErrorBoundary title="AI 生文">
                  <Suspense fallback={<TabFallback />}>
                    <AiGenerationWorkspace onNavigateToContent={() => handleNavClick("content")} />
                  </Suspense>
                </ErrorBoundary>
              </ScrollPanel>
            )}
            <ScrollPanel id="content" active={activeNav === "content"}>
              <ErrorBoundary title="内容管理">
                <Suspense fallback={<TabFallback />}>
                  <ContentWorkspace
                    dirtyCheckRef={contentDirtyRef}
                    isActive={activeNav === "content"}
                    reviewTab={contentReviewTab}
                    isMobile={isMobile}
                    onReviewTabChange={setContentReviewTab}
                  />
                </Suspense>
              </ErrorBoundary>
            </ScrollPanel>
            {visitedTabs.has("prompts") && (
              <ScrollPanel id="prompts" active={activeNav === "prompts"}>
                <ErrorBoundary title="提示词管理">
                  <Suspense fallback={<TabFallback />}>
                    <PromptsWorkspace
                    scope={promptsScope}
                    isMobile={isMobile}
                    onScopeChange={setPromptsScope}
                  />
                  </Suspense>
                </ErrorBoundary>
              </ScrollPanel>
            )}
            {visitedTabs.has("image-library") && (
              <ScrollPanel id="image-library" active={activeNav === "image-library"}>
                <ErrorBoundary title="图片库">
                  <Suspense fallback={<TabFallback />}>
                    <ImageLibraryWorkspace />
                  </Suspense>
                </ErrorBoundary>
              </ScrollPanel>
            )}
            {visitedTabs.has("media") && (
              <ScrollPanel id="media" active={activeNav === "media"}>
                <ErrorBoundary title="媒体矩阵">
                  <Suspense fallback={<TabFallback />}>
                    <AccountsWorkspace isActive={activeNav === "media"} />
                  </Suspense>
                </ErrorBoundary>
              </ScrollPanel>
            )}
            {visitedTabs.has("tasks") && (
              <ScrollPanel id="tasks" active={activeNav === "tasks"}>
                <ErrorBoundary title="分发引擎">
                  <Suspense fallback={<TabFallback />}>
                    <TasksWorkspace isActive={activeNav === "tasks"} />
                  </Suspense>
                </ErrorBoundary>
              </ScrollPanel>
            )}
            {visitedTabs.has("system") && (
              <ScrollPanel id="system" active={activeNav === "system"}>
                <ErrorBoundary title="系统状态">
                  <Suspense fallback={<TabFallback />}>
                    <SystemWorkspace />
                  </Suspense>
                </ErrorBoundary>
              </ScrollPanel>
            )}
            {visitedTabs.has("hot-lists") && (
              <ScrollPanel id="hot-lists" active={activeNav === "hot-lists"}>
                <ErrorBoundary title="热榜">
                  <Suspense fallback={<TabFallback />}>
                    <HotListsWorkspace />
                  </Suspense>
                </ErrorBoundary>
              </ScrollPanel>
            )}
            {visitedTabs.has("mcp") && (
              <ScrollPanel id="mcp" active={activeNav === "mcp"}>
                <ErrorBoundary title="MCP 接入">
                  <Suspense fallback={<TabFallback />}>
                    <McpConnectWorkspace />
                  </Suspense>
                </ErrorBoundary>
              </ScrollPanel>
            )}
            {user.role === "admin" && visitedTabs.has("admin") && (
              <ScrollPanel id="admin" active={activeNav === "admin"}>
                <ErrorBoundary title="用户管理">
                  <Suspense fallback={<TabFallback />}>
                    <UsersWorkspace />
                  </Suspense>
                </ErrorBoundary>
              </ScrollPanel>
            )}
            {user.role === "admin" && visitedTabs.has("audit-logs") && (
              <ScrollPanel id="audit-logs" active={activeNav === "audit-logs"}>
                <ErrorBoundary title="审计日志">
                  <Suspense fallback={<TabFallback />}>
                    <AuditLogsWorkspace />
                  </Suspense>
                </ErrorBoundary>
              </ScrollPanel>
            )}
            {user.role === "admin" && visitedTabs.has("ai-models") && (
              <ScrollPanel id="ai-models" active={activeNav === "ai-models"}>
                <ErrorBoundary title="AI 模型管理">
                  <Suspense fallback={<TabFallback />}>
                    <AiModelsWorkspace />
                  </Suspense>
                </ErrorBoundary>
              </ScrollPanel>
            )}
          </div>
        </section>
        {isMobile && moreOpen && (
          <MobileMorePage
            username={user.username}
            role={user.role}
            isAdmin={user.role === "admin"}
            onNavigate={(key) => { setMoreOpen(false); handleNavClick(key); }}
            onLogout={() => { if (window.confirm("确定退出登录？")) logout(); }}
          />
        )}
        {isMobile && (
          <MobileNav
            activeNav={activeNav}
            onNavigate={(key) => { setMoreOpen(false); handleNavClick(key); }}
            moreActive={moreOpen || onMoreSection}
            onMoreClick={() => setMoreOpen((v) => !v)}
          />
        )}
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
