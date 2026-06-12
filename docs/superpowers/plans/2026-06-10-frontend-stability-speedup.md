# 前端稳定 + 提速小包 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 不引入新依赖，按 tab 懒加载分包提速、ErrorBoundary 原地重试 + 全局异步错误 toast 防白屏/静默失败，并新增统一数据加载 hook 示范改造 TasksWorkspace。

**Architecture:** 两个互不重叠的 PR。PR1（Task 1-4）只动 `App.tsx` / `ErrorBoundary.tsx` / 新增 `GlobalErrorListener.tsx`；PR2（Task 5-7）只动新增 `hooks/useApiData.ts` / `TasksWorkspace.tsx`。**两个 PR 文件集合不相交，可由两个 subagent 在各自 worktree 并行执行**，分支均从 `main` 切出：`fe/stability-pr1`、`fe/stability-pr2`。

**Tech Stack:** React 19 + Vite + TypeScript（strict）。前端无单测框架，门禁 = `pnpm --filter @geo/web typecheck` + `pnpm --filter @geo/web build`（在仓库根目录执行）。

**Spec:** `docs/superpowers/specs/2026-06-10-frontend-stability-speedup-design.md`

**重要背景（执行者必读）:**

- 本仓库前端**没有任何测试框架**（无 vitest/jest），所以本计划没有"写失败测试"步骤；每个 Task 的验证 = typecheck + build 通过。
- 所有 Workspace 组件都是**具名导出**（named export），`React.lazy` 需要 `.then((m) => ({ default: m.Xxx }))` 转换。
- `ErrorBoundary` 全仓库只有 `App.tsx` 一个使用方（已验证），改它的 props 不影响别处。
- spec 原文写 PR2 还要改 PipelineEditor 轮询，**已descope**（其实现已有防脏写守卫且是命令式触发，硬套声明式 hook 风险大于收益）——以本计划为准。

---

## PR1（分支 `fe/stability-pr1`）

### Task 1: ErrorBoundary 增加 title prop、移除 fallback prop

**Files:**
- Modify: `web/src/components/ErrorBoundary.tsx`

- [ ] **Step 1: 修改 Props 与兜底渲染**

将 `web/src/components/ErrorBoundary.tsx` 全文替换为：

```tsx
import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";

interface Props {
  children: ReactNode;
  /** 出错时显示的上下文名称，如「内容管理」→ 标题渲染为「内容管理出错」 */
  title?: string;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("ErrorBoundary caught:", error, info.componentStack);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="panel" style={{ borderColor: "var(--red-soft)", color: "var(--red)", padding: 24, margin: 24 }}>
          <h2>{this.props.title ? `${this.props.title}出错` : "出现错误"}</h2>
          <p>{this.state.error?.message || "未知错误"}</p>
          <button
            className="secondaryButton"
            type="button"
            onClick={() => this.setState({ hasError: false, error: null })}
            style={{ marginTop: 12 }}
          >
            重试
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
```

变化点：删除 `fallback?: ReactNode` prop（仅 App.tsx 在用，Task 3 会同步去掉传参）；新增可选 `title`。「重试」按钮通过重置 state 原地重挂载子树——对 `React.lazy` 的 chunk 加载失败（如部署新版本后旧页面拿不到旧 chunk）同样有效：重试会重新发起 import。

- [ ] **Step 2: 提交**

```bash
git add web/src/components/ErrorBoundary.tsx
git commit -m "feat(web): ErrorBoundary 支持 title 上下文，移除未用的 fallback prop"
```

注意：此时 App.tsx 还在传 `fallback`，typecheck 会报错——属预期中间态，Task 3 完成后整体验证。若执行者希望每步可验证，可将 Task 1 与 Task 3 合并提交。

### Task 2: 新增 GlobalErrorListener（全局异步错误 toast）

**Files:**
- Create: `web/src/components/GlobalErrorListener.tsx`

- [ ] **Step 1: 创建组件**

```tsx
import { useEffect } from "react";
import { useToast } from "./Toast";

/**
 * 捕获所有漏掉 catch 的 Promise 失败与未捕获异常，弹 error toast。
 * 已被局部 try/catch 处理的错误不会触发这两个事件。
 */
export function GlobalErrorListener() {
  const { toast } = useToast();

  useEffect(() => {
    function onRejection(event: PromiseRejectionEvent) {
      const reason: unknown = event.reason;
      const message = reason instanceof Error ? reason.message : String(reason);
      toast(`操作失败：${message}`, "error");
    }
    function onError(event: ErrorEvent) {
      if (!event.message) return;
      toast(`页面错误：${event.message}`, "error");
    }
    window.addEventListener("unhandledrejection", onRejection);
    window.addEventListener("error", onError);
    return () => {
      window.removeEventListener("unhandledrejection", onRejection);
      window.removeEventListener("error", onError);
    };
  }, [toast]);

  return null;
}
```

- [ ] **Step 2: 提交**

```bash
git add web/src/components/GlobalErrorListener.tsx
git commit -m "feat(web): 全局 unhandledrejection/error 监听弹 toast，根治静默失败"
```

### Task 3: App.tsx 按 tab 懒加载 + 挂载 GlobalErrorListener

**Files:**
- Modify: `web/src/App.tsx`

- [ ] **Step 1: 全文替换 App.tsx**

要点：① 11 个 Workspace 改 `lazy()`；② 每个 tab 的 ErrorBoundary 内套 `<Suspense>`；③ 自定义 `fallback` 改为 `title`；④ `ToastProvider` 内挂 `GlobalErrorListener`；⑤ 登录/改密页保持静态 import（体积小且是首屏）。

```tsx
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
```

- [ ] **Step 2: 提交**

```bash
git add web/src/App.tsx
git commit -m "feat(web): 11 个 tab 按需懒加载分包；ErrorBoundary 带上下文重试；挂载全局错误监听"
```

### Task 4: PR1 验证与建 PR

- [ ] **Step 1: typecheck + build**

在仓库根目录运行：

```bash
pnpm --filter @geo/web typecheck
pnpm --filter @geo/web build
```

Expected: 两者 exit 0。build 输出应出现**多个** `dist/assets/*.js` chunk（每个 Workspace 一个），而不是改造前的单一大 chunk。把 build 输出的 chunk 列表（文件名+体积）记录下来写进 PR 描述。

- [ ] **Step 2: 用 gh 建 PR**

```bash
git push -u origin fe/stability-pr1
gh pr create --title "feat(web): 按 tab 懒加载分包 + ErrorBoundary 原地重试 + 全局异步错误 toast" --body "<对照 spec 写：动机、三项改动、build 前后 chunk 对比、手动冒烟项>"
```

---

## PR2（分支 `fe/stability-pr2`）

### Task 5: 新增 useApiData / usePolling hooks

**Files:**
- Create: `web/src/hooks/useApiData.ts`（`web/src/hooks/` 目录尚不存在，一并创建）

- [ ] **Step 1: 创建 hooks 文件**

```ts
import { useCallback, useEffect, useRef, useState } from "react";

/**
 * 统一管理「加载一份远端数据」的 loading / error / data 三件套。
 * - 挂载时自动加载一次；refresh() 手动重载。
 * - 用自增 generation 丢弃过期响应（连续 refresh 的竞态安全）。
 * - 错误存入 state（不向上抛），不会触发全局 unhandledrejection toast。
 */
export function useApiData<T>(fetcher: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const generationRef = useRef(0);

  const refresh = useCallback(async () => {
    const generation = ++generationRef.current;
    setLoading(true);
    try {
      const result = await fetcherRef.current();
      if (generationRef.current !== generation) return;
      setData(result);
      setError(null);
    } catch (err) {
      if (generationRef.current !== generation) return;
      console.warn("useApiData fetch failed", err);
      setError(err instanceof Error ? err : new Error(String(err)));
    } finally {
      if (generationRef.current === generation) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { data, loading, error, refresh };
}

/**
 * 统一轮询：enabled 为 true 时每 intervalMs 调一次 fn，false 时自动清理。
 * fn 的异常在内部吞掉并 console.warn（轮询失败不应弹全局 toast 刷屏）。
 * fn 经 ref 透传，闭包里读到的 state 永远是最新值，无需加入依赖。
 */
export function usePolling(
  fn: () => void | Promise<void>,
  intervalMs: number,
  enabled: boolean,
  options?: { immediate?: boolean },
) {
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const immediate = options?.immediate ?? false;

  useEffect(() => {
    if (!enabled) return;
    const tick = () => {
      void Promise.resolve(fnRef.current()).catch((err) => {
        console.warn("usePolling tick failed", err);
      });
    };
    if (immediate) tick();
    const timer = window.setInterval(tick, intervalMs);
    return () => window.clearInterval(timer);
  }, [enabled, intervalMs, immediate]);
}
```

- [ ] **Step 2: 提交**

```bash
git add web/src/hooks/useApiData.ts
git commit -m "feat(web): 新增 useApiData/usePolling 统一数据加载与轮询"
```

### Task 6: TasksWorkspace 改用新 hooks

**Files:**
- Modify: `web/src/features/tasks/TasksWorkspace.tsx`

> 严格等价替换，不改任何业务行为。tasks / records / logs 状态因被 SSE、轮询、表单多处写入，**保持手动管理不动**；只迁移「只读列表」（accounts/articles/groups）和两个 setInterval。

- [ ] **Step 1: 改 import**

文件头部新增：

```ts
import { useApiData, usePolling } from "../../hooks/useApiData";
```

- [ ] **Step 2: 只读列表改 useApiData**

删除这三行 state（原 59-61 行）：

```ts
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [articles, setArticles] = useState<ArticleSummary[]>([]);
  const [groups, setGroups] = useState<ArticleGroup[]>([]);
```

在同一位置改为（注意必须放在后面所有用到 `accounts`/`articles`/`groups` 的 useMemo 之前）：

```ts
  const { data: accountsData, refresh: refreshAccounts } = useApiData(listAccounts);
  const { data: articlesData, refresh: refreshArticles } = useApiData(listArticles);
  const { data: groupsData, refresh: refreshGroups } = useApiData(listArticleGroups);
  const accounts = accountsData ?? [];
  const articles = articlesData ?? [];
  const groups = groupsData ?? [];
```

类型导入 `Account` / `ArticleSummary` / `ArticleGroup` 若因此不再被引用，从 import 中删掉（typecheck 会指出）。

- [ ] **Step 3: loadInitial 瘦身为 loadTasks**

删除整个 `loadInitial` 函数（原 201-220 行，Promise.allSettled 四连），替换为：

```ts
  async function loadTasks() {
    try {
      const nextTasks = await listTasks();
      setTasks(nextTasks);
    } catch (error) {
      console.warn("Failed to load tasks", error);
    }
  }
```

两处调用同步改名（原 101-103 行与 105-112 行的 effect）：

```ts
  useEffect(() => {
    void loadTasks();
  }, []);

  useEffect(() => {
    if (isInitialMountRef.current) {
      isInitialMountRef.current = false;
      return;
    }
    if (!isActive) return;
    void loadTasks();
    void refreshAccounts();
    void refreshArticles();
    void refreshGroups();
  }, [isActive, refreshAccounts, refreshArticles, refreshGroups]);
```

（refresh 回调是 `useCallback([])` 的稳定引用，加进依赖数组不会引发重跑。）

- [ ] **Step 4: 两个 setInterval 改 usePolling**

删除「任务列表自动刷新」effect（原 114-126 行），替换为：

```ts
  usePolling(
    async () => {
      const nextTasks = await listTasks();
      setTasks(nextTasks);
      setAutoRefreshTaskIds((prev) => pruneFinishedAutoRefreshIds(prev, nextTasks));
    },
    TASK_LIST_REFRESH_MS,
    hasActiveTasks,
  );
```

删除「SSE 降级轮询」effect（原 185-193 行），替换为：

```ts
  usePolling(
    () => {
      if (selectedTaskId) void refreshDetail(selectedTaskId).catch(() => {});
    },
    TASK_LIST_REFRESH_MS,
    shouldFallbackPollSelectedTask,
    { immediate: true },
  );
```

已知微小行为差异（可接受）：旧实现切换 selectedTaskId 会重启 interval 并立即拉一次；新实现仅在 `shouldFallbackPollSelectedTask` 翻转时重启——而 `selectTask()` 本身会清掉新任务的 fallback 标记并主动 `refreshDetail`，所以用户感知一致。

- [ ] **Step 5: 提交**

```bash
git add web/src/features/tasks/TasksWorkspace.tsx
git commit -m "refactor(web): TasksWorkspace 改用 useApiData/usePolling，去掉手写定时器与三件套"
```

### Task 7: PR2 验证与建 PR

- [ ] **Step 1: typecheck + build**

```bash
pnpm --filter @geo/web typecheck
pnpm --filter @geo/web build
```

Expected: 两者 exit 0。

- [ ] **Step 2: 用 gh 建 PR**

```bash
git push -u origin fe/stability-pr2
gh pr create --title "refactor(web): 统一数据加载 hook（useApiData/usePolling）+ TasksWorkspace 示范改造" --body "<对照 spec 写：动机、hook 设计、TasksWorkspace 等价替换说明、已知微小行为差异>"
```

---

## 合并后人工冒烟清单（由用户/主会话执行，不属于 subagent 任务）

1. 逐 tab 打开，确认懒加载占位一闪而过、页面正常；
2. DevTools Network 断网后点任意刷新按钮 → 应弹红色 toast 而非无反应；
3. 任意 tab 渲染抛错（可临时在组件里 throw）→ 显示「xx出错 + 重试」，点重试原地恢复，其它 tab 状态不丢；
4. 分发引擎：创建任务 → 执行 → 观察列表 4s 自动刷新、SSE 日志滚动正常。
