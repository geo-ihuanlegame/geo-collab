import { Bot, FileText, Flame, Images, MessagesSquare, MonitorCog, Plug, RadioTower, Send, Sparkles } from "lucide-react";
import type { ComponentType } from "react";

export type NavKey = "agents" | "ai" | "content" | "prompts" | "image-library" | "media" | "tasks" | "system" | "hot-lists" | "mcp" | "admin" | "audit-logs" | "ai-models";

export type PromptScope = "generation" | "ai_format" | "image_search" | "image_companion";

export type PromptTemplate = {
  id: number;
  name: string;
  content: string;
  scope: PromptScope;
  user_id: number | null;
  is_system: boolean;
  is_enabled: boolean;
  is_deleted: boolean;
  created_at: string;
  updated_at: string;
};

export type GenerationSession = {
  id: number;
  status: "pending" | "running" | "done" | "failed";
  article_ids: number[];
  error_message: string | null;
  created_at: string;
  completed_at: string | null;
};

export type QuestionPool = {
  id: number;
  name: string;
  feishu_app_token: string | null;
  feishu_table_id: string | null;
  last_synced_at: string | null;
  created_at: string;
  pending_count: number;
};

export type QuestionItem = {
  id: number;
  record_id: string;
  fields: Record<string, unknown>;
  question_text: string | null;
  category: string | null;
  status: string;
  article_id: number | null;
};

export type QuestionSyncResult = {
  total: number;
  added: number;
  updated: number;
  reactivated: number;
  deactivated: number;
};

// ── 方案池 / 方案运行（scheme flow）──────────────────────────────────────────

export type AiEngine = { label: string; model: string };

// ── AI 模型注册表（admin 管理；密钥只存 env，行只引用 api_key_env 变量名）────────
export type AiModelScope = "generation" | "ai_format";

export type AiModel = {
  id: number;
  label: string;
  model: string;
  scope: AiModelScope;
  base_url: string | null;
  api_key_env: string | null;
  is_enabled: boolean;
  is_default: boolean;
  sort_order: number;
  created_at: string;
  updated_at: string;
};

export type AiModelPayload = {
  label: string;
  model: string;
  scope: AiModelScope;
  base_url: string | null;
  api_key_env: string | null;
  is_enabled: boolean;
  is_default: boolean;
  sort_order: number;
};

export type QuestionBrief = {
  id: number;
  record_id: string;
  question_text: string | null;
};

export type QuestionType = {
  question_type: string | null;
  count: number;
  questions: QuestionBrief[];
};

export type SchemeLineQuestion = {
  question_item_id: number | null;
  record_id: string | null;
  question_text: string | null;
  question_type: string | null;
};

export type SchemeLine = {
  id: number;
  question_type: string | null;
  article_count: number;
  allowed_prompt_template_ids: number[];
  questions: SchemeLineQuestion[];
};

export type Scheme = {
  id: number;
  name: string;
  pool_id: number;
  is_enabled: boolean;
  ai_engine: string | null;
  created_at: string;
  updated_at: string;
  lines: SchemeLine[];
};

export type SchemeRunStatus = "pending" | "running" | "done" | "partial_failed" | "failed";
export type SchemeTaskStatus = "pending" | "running" | "done" | "failed";

export type SchemeRunTask = {
  id: number;
  scheme_line_id: number | null;
  question_type: string | null;
  question_text: string | null;
  question_item_ids: number[];
  allowed_prompt_template_ids: number[];
  actual_prompt_template_id: number | null;
  status: SchemeTaskStatus;
  article_id: number | null;
  error_message: string | null;
};

export type SchemeRun = {
  id: number;
  scheme_id: number;
  status: SchemeRunStatus;
  article_ids: number[];
  error_message: string | null;
  created_at: string;
  completed_at: string | null;
  tasks: SchemeRunTask[];
};

export type SchemeRunSummary = {
  id: number;
  status: SchemeRunStatus;
  article_count: number;
  task_count: number;
  created_at: string;
  completed_at: string | null;
};

export type SchemeLineInput = {
  question_type: string | null;
  question_item_ids: number[];
  article_count: number;
  allowed_prompt_template_ids: number[];
};

export type SchemeCreatePayload = {
  name: string;
  pool_id: number;
  is_enabled: boolean;
  ai_engine: string | null;
  lines: SchemeLineInput[];
};

export type SchemeUpdatePayload = {
  name: string;
  is_enabled: boolean;
  ai_engine: string | null;
  lines: SchemeLineInput[];
};

export type Asset = {
  id: string;
  filename: string;
  mime_type: string;
  size: number;
  width: number | null;
  height: number | null;
  url: string;
};

export type ArticleBodyAsset = {
  asset_id: string;
  position: number;
  editor_node_id: string | null;
};

export type ReviewStatus = "pending" | "approved";

export type ArticleSummary = {
  id: number;
  title: string;
  author: string | null;
  cover_asset_id: string | null;
  word_count: number;
  status: string;
  version: number;
  published_count: number;
  review_status: ReviewStatus;
  created_at: string;
  updated_at: string;
};

export type Article = ArticleSummary & {
  content_json: Record<string, unknown>;
  content_html: string;
  plain_text: string;
  body_assets: ArticleBodyAsset[];
  /** @deprecated 使用 stock_category_ids */
  stock_category_id: number | null;
  stock_category_ids: number[];
  ai_checking: boolean;
  ai_format_error: string | null;
};

export type ArticleReviewSummary = {
  total: number;
  approved: number;
};

export type ArticleGroup = {
  id: number;
  name: string;
  items: { article_id: number; sort_order: number }[];
  version: number;
  review_summary?: ArticleReviewSummary;
  created_at: string;
  updated_at: string;
};

export type Account = {
  id: number;
  platform_code: string;
  platform_name: string;
  display_name: string;
  platform_user_id: string | null;
  status: string;
  last_checked_at: string | null;
  last_login_at: string | null;
  state_path: string | null;
  note: string | null;
  contact: string | null;
  avatar_asset_id: string | null;
  distribution_enabled: boolean;
  app_id: string | null;
  app_secret_tail: string | null;
  created_at: string;
  updated_at: string;
  owner_name: string | null;
  member_count: number;
  can_manage: boolean;
  identity_known: boolean;
};

export type AccountMember = {
  user_id: number;
  username: string | null;
  display_name?: string | null;
  account_id?: number;
  is_owner: boolean;
  granted_via: string;
  created_at?: string;
};

export type BackfillIdentitySummary = {
  processed: number;
  backfilled: number;
  merged: number;
  conflicts: number;
  still_unknown: number;
  failed: number;
};

export type AccountBrowserSession = {
  account: Account;
  platform_code: string;
  account_key: string;
  session_id: string;
  novnc_url: string | null;
  status?: "pending" | "queued" | "starting" | "active" | "failed" | "cancelled";
  queue_reason?: string | null;
  error_message?: string | null;
};

export type AccountLoginSessionStatus =
  | "pending"
  | "queued"
  | "starting"
  | "active"
  | "failed"
  | "cancelled";

export type AccountLoginSessionStatusResponse = {
  status: AccountLoginSessionStatus;
  novnc_url: string | null;
  error_message: string | null;
  queue_reason?: string | null;
  browser_session_id: string | null;
};

export type AccountBrowserSessionFinish = {
  account: Account;
  logged_in: boolean;
  url: string;
  title: string;
};

export type Draft = {
  id: number | null;
  title: string;
  author: string;
  cover_asset_id: string | null;
  status: string;
  version: number | null;
  stock_category_ids: number[];
};

export type TaskAccountRead = {
  account_id: number;
  sort_order: number;
  display_name: string;
  status: string;
};

export type Task = {
  id: number;
  name: string;
  task_type: string;
  status: string;
  platform_id: number;
  platform_code: string;
  article_id: number | null;
  group_id: number | null;
  stop_before_publish: boolean;
  cancel_requested: boolean;
  accounts: TaskAccountRead[];
  record_count: number;
  worker_id: string | null;
  worker_heartbeat_at: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

export type PublishRecord = {
  id: number;
  task_id: number;
  article_id: number;
  platform_id: number;
  account_id: number;
  status: string;
  queue_reason: string | null;
  publish_url: string | null;
  error_message: string | null;
  retry_of_record_id: number | null;
  started_at: string | null;
  finished_at: string | null;
  remote_browser_session_id: string | null;
  novnc_url: string | null;
  failure_kind?: string | null;
};

export type TaskLog = {
  id: number;
  task_id: number;
  record_id: number | null;
  level: string;
  message: string;
  screenshot_asset_id: string | null;
  created_at: string;
};

export type AssignmentPreview = {
  task_type: string;
  platform_code: string;
  article_count: number;
  account_count: number;
  items: { position: number; article_id: number; account_id: number; account_sort_order: number }[];
};

export type SystemStatus = {
  service: string;
  directories_ready: boolean;
  article_count: number;
  account_count: number;
  task_count: number;
  browser_ready: boolean;
  pending_task_count: number;
  active_browser_sessions: number;
  worker_online: boolean;
  novnc_runtime_ready: boolean;
};

export type PlatformOption = {
  code: string;
  name: string;
  mode?: "api" | "browser"; // api=凭据直填(如微信公众号)，browser=浏览器扫码登录(如头条)
};

// API Request Bodies
export type ArticleCreatePayload = {
  title: string;
  author?: string | null;
  cover_asset_id?: string | null;
  content_json: Record<string, unknown>;
  content_html?: string;
  plain_text?: string;
  word_count?: number;
  status?: string;
  version?: number | null;
  client_request_id?: string;
};

export type ArticleUpdatePayload = {
  title?: string;
  author?: string | null;
  cover_asset_id?: string | null;
  content_json?: Record<string, unknown>;
  content_html?: string;
  plain_text?: string;
  word_count?: number;
  status?: string;
  version?: number | null;
  stock_category_ids?: number[];
  client_request_id?: string;
};

export type StockCategory = {
  id: number;
  name: string;
  bucket_name: string;
  kind: "main" | "companion";
  description: string | null;
  official_url: string | null;
  created_at: string;
  latest_image_at: string | null;
};

export type ImageSearchResult = {
  id: number;
  filename: string;
  url: string;
  category_id: number;
  category_name: string;
  kind: "main" | "companion";
};

export type StockImage = {
  id: number;
  category_id: number;
  minio_key: string;
  filename: string;
  description: string | null;
  tags: string[];
  width: number | null;
  height: number | null;
  url: string;
  created_at: string;
};

export type TaskCreatePayload = {
  name: string;
  client_request_id: string;
  task_type: "single" | "group_round_robin";
  article_id?: number | null;
  group_id?: number | null;
  accounts: { account_id: number; sort_order: number }[];
  stop_before_publish?: boolean;
  platform_code?: string;
};

export type AutoDistributePayload = {
  article_id?: number;
  group_id?: number;
  account_ids: number[];
  name?: string;
};

export type ApiCredentialsIn = {
  app_id: string;
  app_secret: string;
};

export type ApiAccountCreatePayload = {
  platform_code: string;
  display_name: string;
  api_credentials: ApiCredentialsIn;
  contact?: string | null;
  note?: string | null;
  avatar_asset_id?: string | null;
  distribution_enabled?: boolean;
};

export type AccountUpdatePayload = {
  display_name?: string;
  contact?: string | null;
  note?: string | null;
  avatar_asset_id?: string | null;
  distribution_enabled?: boolean;
  api_credentials?: ApiCredentialsIn;
};

export type PlatformLoginPayload = {
  display_name: string;
  account_key: string;
  use_browser?: boolean;
  note?: string | null;
  contact?: string | null;
  avatar_asset_id?: string | null;
  distribution_enabled?: boolean;
};

export type ArticleGroupUpdateItemsPayload = {
  items: { article_id: number; sort_order: number }[];
};

export type ManualConfirmPayload = {
  outcome: "succeeded" | "failed";
  publish_url?: string | null;
  error_message?: string | null;
};

export function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    pending: "待执行",
    running: "执行中",
    succeeded: "已发布",
    partial_failed: "部分失败",
    failed: "失败",
    cancelled: "已取消",
    waiting_manual_publish: "等待确认",
    waiting_user_input: "需要处理",
  };
  return labels[status] ?? status;
}

export type NavChild = { key: string; label: string; value: string };

export const navItems: {
  key: NavKey;
  label: string;
  icon: ComponentType<{ size?: number }>;
  children?: NavChild[];
}[] = [
  { key: "agents", label: "智能体管理", icon: Bot },
  { key: "ai", label: "AI 生文", icon: Sparkles },
  {
    key: "content",
    label: "内容管理",
    icon: FileText,
    children: [
      { key: "content:pending", label: "未审核库", value: "pending" },
      { key: "content:approved", label: "已审核库", value: "approved" },
    ],
  },
  {
    key: "prompts",
    label: "提示词管理",
    icon: MessagesSquare,
    children: [
      { key: "prompts:generation", label: "AI生文提示词", value: "generation" },
      { key: "prompts:ai_format", label: "AI格式提示词", value: "ai_format" },
      { key: "prompts:image_search", label: "搜图关键词", value: "image_search" },
      { key: "prompts:image_companion", label: "陪衬配图提示词", value: "image_companion" },
    ],
  },
  { key: "image-library", label: "图片库", icon: Images },
  { key: "media", label: "媒体矩阵", icon: RadioTower },
  { key: "tasks", label: "分发引擎", icon: Send },
  { key: "system", label: "系统状态", icon: MonitorCog },
  { key: "hot-lists", label: "热榜", icon: Flame },
  { key: "mcp", label: "MCP 接入", icon: Plug },
];

export const TERMINAL_STATUSES = new Set(["succeeded", "partial_failed", "failed", "cancelled"]);

export type UserInfo = {
  id: number;
  username: string;
  role: "admin" | "operator";
  must_change_password: boolean;
  ai_format_preset_id: number | null;
};

export type UserRecord = {
  id: number;
  username: string;
  role: "admin" | "operator";
  is_active: boolean;
  must_change_password: boolean;
  created_at: string;
  last_login_at: string | null;
};

export const ITEM_HEIGHT = 82;

// ── 流程编排（pipelines）─────────────────────────────────────────────────────

export interface PipelineNodeDef {
  node_type: string;
  name: string;
  node_index: number;
  config: Record<string, unknown>;
  flow_meta: PipelineFlowMeta | null;
}
export interface PipelineFlowMeta {
  schemaVersion?: number;
  dependsOnIndex?: number | null;
  inputMapping?: { from: string; to: string }[];
  condition?: { field: string; op: "eq" | "neq" | "contains"; value: string } | null;
}
export interface Pipeline {
  id: number;
  name: string;
  description: string | null;
  has_draft: boolean;
  is_running: boolean;
  created_at: string;
  updated_at: string;
  type: string;
  tags: string[];
  ignore_exception: boolean;
  is_enabled: boolean;
  schedule_kind: string;
  schedule_minute: number | null;
  schedule_hour: number | null;
  schedule_weekday: number | null;
  window_start: string | null;
  window_end: string | null;
  last_scheduled_run_at: string | null;
  nodes: PipelineNodeDef[];
}
export interface PipelineVersionSummary {
  id: number; pipeline_id: number; version_no: number;
  remark: string | null; created_by: number; created_at: string;
}
export interface PipelineRun {
  id: number; pipeline_id: number; status: string;
  article_ids: number[]; node_results: Record<string, unknown>;
  error_message: string | null; created_at: string; completed_at: string | null;
}
export interface NodeTypeDef {
  type: string; label: string;
  config_schema: { key: string; type: string; label: string; default?: boolean | string | number; hint?: string; note?: string }[];
}

export type RunLogRow = {
  batch: number;
  run_status: string;
  step: number;
  task_name: string;
  level: "INFO" | "ERROR";
  message: string;
  time: string | null;
};

export type RunLogPage = {
  items: RunLogRow[];
  total: number;
  page: number;
  page_size: number;
};
