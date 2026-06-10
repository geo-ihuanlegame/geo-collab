import { useRef, useState } from "react";
import { navItems } from "./types";
import type { NavKey, PromptScope, ReviewStatus } from "./types";
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
import { ChevronDown, ChevronLeft, LogOut, ScrollText, Users } from "lucide-react";
import { MobileNav } from "./components/MobileNav";
import { MobileMorePage } from "./components/MobileMorePage";
import { ScrollPanel } from "./components/ScrollPanel";
import { useIsMobile } from "./hooks/useIsMobile";
import "./styles.css";

function AppShell() {
  const { user, loading, logout } = useAuth();
  const isMobile = useIsMobile();
  const [moreOpen, setMoreOpen] = useState(false);
  const [activeNav, setActiveNav] = useState<NavKey>("agents");
  // 当前页是否属于「更多」分区（即不在底栏 4 个高频入口中）
  const onMoreSection = !(["agents", "ai", "content", "tasks"] as NavKey[]).includes(activeNav);
  const [visitedTabs, setVisitedTabs] = useState<Set<NavKey>>(new Set(["agents"]));
  const [openGroup, setOpenGroup] = useState<NavKey | null>(null);
  const [contentReviewTab, setContentReviewTab] = useState<ReviewStatus>("pending");
  const [promptsScope, setPromptsScope] = useState<PromptScope>("generation");
  const contentDirtyRef = useRef<() => boolean>(() => false);

  // 手风琴：同一时间只展开一个含子页的父菜单
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

  function handleNavClick(key: NavKey) {
    if (activeNav === "content" && key !== "content" && contentDirtyRef.current()) {
      if (!window.confirm("当前文章有未保存内容，确定要切换页面吗？未保存的修改将丢失。")) return;
    }
    setVisitedTabs((prev) => (prev.has(key) ? prev : new Set(prev).add(key)));
    setActiveNav(key);
    // 选中非父级菜单时，收起所有含子页的父菜单
    const hasChildren = navItems.some((i) => i.key === key && (i.children?.length ?? 0) > 0);
    if (!hasChildren) setOpenGroup(null);
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
                <ErrorBoundary fallback={<p role="alert">智能体管理出错，请刷新重试</p>}>
                  <AgentManagementWorkspace />
                </ErrorBoundary>
              </ScrollPanel>
            )}
            {visitedTabs.has("ai") && (
              <ScrollPanel id="ai" active={activeNav === "ai"}>
                <ErrorBoundary fallback={<p role="alert">AI 生文出错，请刷新重试</p>}>
                  <AiGenerationWorkspace onNavigateToContent={() => handleNavClick("content")} />
                </ErrorBoundary>
              </ScrollPanel>
            )}
            <ScrollPanel id="content" active={activeNav === "content"}>
              <ErrorBoundary fallback={<p role="alert">内容管理出错，请刷新重试</p>}>
                <ContentWorkspace
                  dirtyCheckRef={contentDirtyRef}
                  isActive={activeNav === "content"}
                  reviewTab={contentReviewTab}
                  isMobile={isMobile}
                  onReviewTabChange={setContentReviewTab}
                />
              </ErrorBoundary>
            </ScrollPanel>
            {visitedTabs.has("prompts") && (
              <ScrollPanel id="prompts" active={activeNav === "prompts"}>
                <ErrorBoundary fallback={<p role="alert">提示词管理出错，请刷新重试</p>}>
                  <PromptsWorkspace
                  scope={promptsScope}
                  isMobile={isMobile}
                  onScopeChange={setPromptsScope}
                />
                </ErrorBoundary>
              </ScrollPanel>
            )}
            {visitedTabs.has("image-library") && (
              <ScrollPanel id="image-library" active={activeNav === "image-library"}>
                <ErrorBoundary fallback={<p role="alert">图片库出错，请刷新重试</p>}>
                  <ImageLibraryWorkspace />
                </ErrorBoundary>
              </ScrollPanel>
            )}
            {visitedTabs.has("media") && (
              <ScrollPanel id="media" active={activeNav === "media"}>
                <ErrorBoundary fallback={<p role="alert">媒体矩阵出错，请刷新重试</p>}>
                  <AccountsWorkspace isActive={activeNav === "media"} />
                </ErrorBoundary>
              </ScrollPanel>
            )}
            {visitedTabs.has("tasks") && (
              <ScrollPanel id="tasks" active={activeNav === "tasks"}>
                <ErrorBoundary fallback={<p role="alert">分发引擎出错，请刷新重试</p>}>
                  <TasksWorkspace isActive={activeNav === "tasks"} />
                </ErrorBoundary>
              </ScrollPanel>
            )}
            {visitedTabs.has("system") && (
              <ScrollPanel id="system" active={activeNav === "system"}>
                <ErrorBoundary fallback={<p role="alert">系统状态出错，请刷新重试</p>}>
                  <SystemWorkspace />
                </ErrorBoundary>
              </ScrollPanel>
            )}
            {user.role === "admin" && visitedTabs.has("admin") && (
              <ScrollPanel id="admin" active={activeNav === "admin"}>
                <ErrorBoundary fallback={<p role="alert">用户管理出错，请刷新重试</p>}>
                  <UsersWorkspace />
                </ErrorBoundary>
              </ScrollPanel>
            )}
            {user.role === "admin" && visitedTabs.has("audit-logs") && (
              <ScrollPanel id="audit-logs" active={activeNav === "audit-logs"}>
                <ErrorBoundary fallback={<p role="alert">审计日志出错，请刷新重试</p>}>
                  <AuditLogsWorkspace />
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
