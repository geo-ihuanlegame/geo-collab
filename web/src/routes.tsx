/* eslint-disable react-refresh/only-export-components -- 路由配置文件：导出 router 之外的组件定义不参与 Fast Refresh，符合预期 */
import { lazy } from "react";
import { createBrowserRouter, Navigate, useNavigate, useParams } from "react-router-dom";
import type { ReactElement } from "react";
import { RootLayout } from "./App";
import { useAuth } from "./features/auth/AuthContext";
import { useIsMobile } from "./hooks/useIsMobile";
import type { PromptScope, ReviewStatus } from "./types";

// 各工作区仍走代码分割懒加载（动态 import → 独立 chunk）；
// RootLayout 的 <Outlet/> 外层有统一 <Suspense> 兜住加载态。
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

// admin 专属页守卫：非 admin 直接重定向回默认页（RootLayout 已保证此处必有登录用户）。
function RequireAdmin({ children }: { children: ReactElement }) {
  const { user } = useAuth();
  if (user?.role !== "admin") return <Navigate to="/agents" replace />;
  return children;
}

// 「内容管理」子页（未审核 / 已审核）由 URL 段驱动：/content/:status。
function ContentRoute() {
  const { status } = useParams();
  const navigate = useNavigate();
  const isMobile = useIsMobile();
  const reviewTab: ReviewStatus = status === "approved" ? "approved" : "pending";
  return (
    <ContentWorkspace
      isActive
      reviewTab={reviewTab}
      isMobile={isMobile}
      onReviewTabChange={(t) => navigate(`/content/${t}`)}
    />
  );
}

const PROMPT_SCOPES: PromptScope[] = ["generation", "ai_format", "image_search", "image_companion"];

// 「提示词管理」子页（4 个 scope）由 URL 段驱动：/prompts/:scope。
function PromptsRoute() {
  const { scope } = useParams();
  const navigate = useNavigate();
  const isMobile = useIsMobile();
  const active: PromptScope = PROMPT_SCOPES.includes(scope as PromptScope)
    ? (scope as PromptScope)
    : "generation";
  return (
    <PromptsWorkspace
      scope={active}
      isMobile={isMobile}
      onScopeChange={(s) => navigate(`/prompts/${s}`)}
    />
  );
}

// AI 生文里「打开文章」跳转到内容管理。
function AiRoute() {
  const navigate = useNavigate();
  return <AiGenerationWorkspace onNavigateToContent={() => navigate("/content")} />;
}

export const router = createBrowserRouter([
  {
    path: "/",
    element: <RootLayout />,
    children: [
      { index: true, element: <Navigate to="/agents" replace /> },
      { path: "agents", element: <AgentManagementWorkspace /> },
      { path: "ai", element: <AiRoute /> },
      { path: "content", element: <ContentRoute /> },
      { path: "content/:status", element: <ContentRoute /> },
      { path: "prompts", element: <PromptsRoute /> },
      { path: "prompts/:scope", element: <PromptsRoute /> },
      { path: "image-library", element: <ImageLibraryWorkspace /> },
      { path: "media", element: <AccountsWorkspace isActive /> },
      { path: "tasks", element: <TasksWorkspace isActive /> },
      { path: "system", element: <SystemWorkspace /> },
      { path: "hot-lists", element: <HotListsWorkspace /> },
      { path: "mcp-connect", element: <McpConnectWorkspace /> },
      {
        path: "admin",
        element: (
          <RequireAdmin>
            <UsersWorkspace />
          </RequireAdmin>
        ),
      },
      {
        path: "audit-logs",
        element: (
          <RequireAdmin>
            <AuditLogsWorkspace />
          </RequireAdmin>
        ),
      },
      {
        path: "ai-models",
        element: (
          <RequireAdmin>
            <AiModelsWorkspace />
          </RequireAdmin>
        ),
      },
      { path: "*", element: <Navigate to="/agents" replace /> },
    ],
  },
]);
