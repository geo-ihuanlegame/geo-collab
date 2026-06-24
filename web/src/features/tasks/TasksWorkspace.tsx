import { useEffect, useMemo, useRef, useState } from "react";
import { listAccounts, listPlatforms } from "../../api/accounts";
import { listArticleGroups, listArticles } from "../../api/articles";
import { newClientRequestId, singleFlight } from "../../api/core";
import {
  cancelTask as cancelTaskRequest,
  createTask as createTaskRequest,
  executeTask as executeTaskRequest,
  listTaskLogs,
  listTaskRecords,
  listTasks,
  manualConfirmRecord,
  previewTaskAssignment,
  resolveRecordUserInput,
  retryRecord as retryRecordRequest,
} from "../../api/tasks";
import type { Task, TaskCreatePayload, PublishRecord, TaskLog, AssignmentPreview } from "../../types";
import { TERMINAL_STATUSES, statusLabel } from "../../types";
import { Plus, RefreshCw, Send } from "lucide-react";
import { useToast } from "../../components/Toast";
import { useApiData, usePolling } from "../../hooks/useApiData";
import { formatDate, formatDateTime, formatTime } from "../../utils/dateFormat";
import { openRemoteBrowser } from "../../utils/remoteBrowser";
import { Pagination } from "../../components/Pagination";

const TASK_PAGE_SIZE = 10;
const TASK_LIST_REFRESH_MS = 4_000;

function isTaskActive(task: Task | null | undefined): task is Task {
  return Boolean(task && !TERMINAL_STATUSES.has(task.status));
}

function upsertTask(tasks: Task[], task: Task): Task[] {
  return tasks.some((item) => item.id === task.id)
    ? tasks.map((item) => (item.id === task.id ? task : item))
    : [task, ...tasks];
}

function pruneFinishedAutoRefreshIds(ids: Set<number>, tasks: Task[]): Set<number> {
  if (ids.size === 0) return ids;
  const taskById = new Map(tasks.map((task) => [task.id, task]));
  let changed = false;
  const next = new Set(ids);
  ids.forEach((taskId) => {
    const task = taskById.get(taskId);
    if (!task || TERMINAL_STATUSES.has(task.status)) {
      next.delete(taskId);
      changed = true;
    }
  });
  return changed ? next : ids;
}

export function TasksWorkspace({ isActive }: { isActive?: boolean } = {}) {
  const { toast } = useToast();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [taskPage, setTaskPage] = useState(0);
  const [selectedTaskId, setSelectedTaskId] = useState<number | null>(null);
  const [records, setRecords] = useState<PublishRecord[]>([]);
  const [logs, setLogs] = useState<TaskLog[]>([]);
  const { data: accountsData, refresh: refreshAccounts } = useApiData(listAccounts);
  const { data: articlesData, refresh: refreshArticles } = useApiData(listArticles);
  const { data: groupsData, refresh: refreshGroups } = useApiData(listArticleGroups);
  const { data: platformsData } = useApiData(listPlatforms);
  const accounts = useMemo(() => accountsData ?? [], [accountsData]);
  const articles = useMemo(() => articlesData ?? [], [articlesData]);
  const groups = useMemo(() => groupsData ?? [], [groupsData]);
  const platforms = useMemo(() => platformsData ?? [], [platformsData]);
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [loading, setLoading] = useState(false);
  const [autoRefreshTaskIds, setAutoRefreshTaskIds] = useState<Set<number>>(new Set());
  const [sseFallbackTaskIds, setSseFallbackTaskIds] = useState<Set<number>>(new Set());

  const [formName, setFormName] = useState("");
  const [formType, setFormType] = useState<"single" | "group_round_robin">("single");
  const [formArticleId, setFormArticleId] = useState<number | null>(null);
  const [formGroupId, setFormGroupId] = useState<number | null>(null);
  const [formAccountIds, setFormAccountIds] = useState<number[]>([]);
  const [formPlatformFilter, setFormPlatformFilter] = useState<string>("");
  const [preview, setPreview] = useState<AssignmentPreview | null>(null);
  const [formError, setFormError] = useState("");

  const lastLogIdRef = useRef(0);
  const isInitialMountRef = useRef(true);
  const prevRecordsJsonRef = useRef<string>("");
  const prevTasksJsonRef = useRef<string>("");

  const selectedTask = tasks.find((t) => t.id === selectedTaskId) ?? null;
  const hasActiveRecords = records.some(r =>
    r.status === "running" || r.status === "waiting_user_input" || r.status === "waiting_manual_publish"
  );
  const shouldPollSelectedTask =
    selectedTaskId !== null &&
    (isTaskActive(selectedTask) || hasActiveRecords || autoRefreshTaskIds.has(selectedTaskId));
  const shouldStreamSelectedTask =
    shouldPollSelectedTask && selectedTaskId !== null && !sseFallbackTaskIds.has(selectedTaskId);
  const shouldFallbackPollSelectedTask =
    shouldPollSelectedTask && selectedTaskId !== null && sseFallbackTaskIds.has(selectedTaskId);
  const hasActiveTasks = tasks.some(isTaskActive);
  const articleMap = useMemo(() => Object.fromEntries(articles.map((a) => [a.id, a])), [articles]);
  const accountMap = useMemo(() => Object.fromEntries(accounts.map((a) => [a.id, a])), [accounts]);
  const platformMap = useMemo(() => Object.fromEntries(platforms.map((p) => [p.code, p])), [platforms]);
  const sortedTasks = useMemo(
    () => tasks.slice().sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()),
    [tasks],
  );
  const totalTaskPages = Math.max(1, Math.ceil(sortedTasks.length / TASK_PAGE_SIZE));
  const pagedTasks = sortedTasks.slice(taskPage * TASK_PAGE_SIZE, (taskPage + 1) * TASK_PAGE_SIZE);

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

  usePolling(
    async () => {
      const nextTasks = await listTasks();
      setTasks(nextTasks);
      setAutoRefreshTaskIds((prev) => pruneFinishedAutoRefreshIds(prev, nextTasks));
    },
    TASK_LIST_REFRESH_MS,
    hasActiveTasks,
  );

  useEffect(() => {
    if (!selectedTaskId || !shouldStreamSelectedTask) return;
    const taskId = selectedTaskId;
    const es = new EventSource(`/api/tasks/${taskId}/stream?after_log_id=${lastLogIdRef.current}`);
    let errorCount = 0;

    es.addEventListener("task", (e) => {
      try {
        const updated = JSON.parse(e.data) as Task;
        setTasks((prev) => prev.map((t) => (t.id === updated.id ? updated : t)));
        errorCount = 0;
      } catch { errorCount = 0; }
    });

    es.addEventListener("log", (e) => {
      try {
        const log = JSON.parse(e.data) as TaskLog;
        setLogs((prev) => {
          if (prev.some((l) => l.id === log.id)) return prev;
          return [...prev, log];
        });
        lastLogIdRef.current = Math.max(lastLogIdRef.current, log.id);
        errorCount = 0;
      } catch { errorCount = 0; }
    });

    es.addEventListener("records", (e) => {
      try {
        const rs = JSON.parse(e.data) as PublishRecord[];
        setRecords(rs);
        errorCount = 0;
      } catch { errorCount = 0; }
    });

    es.addEventListener("done", () => {
      es.close();
      setAutoRefreshTaskIds((prev) => {
        const next = new Set(prev);
        next.delete(taskId);
        return next;
      });
      // 500ms 后兜底拉取最终状态
      setTimeout(() => void refreshDetail(taskId).catch(() => {}), 500);
    });

    es.onerror = () => {
      errorCount++;
      if (errorCount > 5) {
        es.close();
        setSseFallbackTaskIds((prev) => new Set(prev).add(taskId));
        void refreshDetail(taskId).catch(() => {});
      }
    };

    return () => { es.close(); };
  }, [selectedTaskId, shouldStreamSelectedTask]);

  usePolling(
    () => {
      if (selectedTaskId) void refreshDetail(selectedTaskId).catch(() => {});
    },
    TASK_LIST_REFRESH_MS,
    shouldFallbackPollSelectedTask,
    { immediate: true },
  );

  useEffect(() => {
    if (taskPage >= totalTaskPages) {
      setTaskPage(totalTaskPages - 1);
    }
  }, [taskPage, totalTaskPages]);

  async function loadTasks() {
    try {
      const nextTasks = await listTasks();
      setTasks(nextTasks);
    } catch (error) {
      console.warn("Failed to load tasks", error);
    }
  }

  async function refreshDetail(taskId: number) {
    const [rs, ls, ts] = await Promise.all([
      listTaskRecords(taskId),
      listTaskLogs(taskId, lastLogIdRef.current),
      listTasks(),
    ]);
    const rsJson = JSON.stringify(rs);
    if (rsJson !== prevRecordsJsonRef.current) {
      setRecords(rs);
      prevRecordsJsonRef.current = rsJson;
    }
    if (ls.length > 0) {
      setLogs((prev) => [...prev, ...ls]);
      lastLogIdRef.current = Math.max(...ls.map((l) => l.id));
    }
    const tsJson = JSON.stringify(ts);
    if (tsJson !== prevTasksJsonRef.current) {
      setTasks(ts);
      prevTasksJsonRef.current = tsJson;
    }
    const currentTask = ts.find((task) => task.id === taskId);
    if (!currentTask || TERMINAL_STATUSES.has(currentTask.status)) {
      setAutoRefreshTaskIds((prev) => {
        if (!prev.has(taskId)) return prev;
        const next = new Set(prev);
        next.delete(taskId);
        return next;
      });
      setSseFallbackTaskIds((prev) => {
        if (!prev.has(taskId)) return prev;
        const next = new Set(prev);
        next.delete(taskId);
        return next;
      });
    }
  }

  async function selectTask(taskId: number) {
    setSelectedTaskId(taskId);
    setSseFallbackTaskIds((prev) => {
      if (!prev.has(taskId)) return prev;
      const next = new Set(prev);
      next.delete(taskId);
      return next;
    });
    lastLogIdRef.current = 0;
    setLogs([]);
    await refreshDetail(taskId);
  }

  async function createTask() {
    setFormError("");
    if (!formName.trim() || formAccountIds.length === 0) {
      setFormError("请填写任务名称并选择账号");
      return;
    }
    if (formType === "single" && !formArticleId) {
      setFormError("请选择文章");
      return;
    }
    if (formType === "group_round_robin" && !formGroupId) {
      setFormError("请选择分组");
      return;
    }
    const taskPlatforms = new Set(formAccountIds.map((id) => accountMap[id]?.platform_code));
    if (taskPlatforms.size > 1) {
      setFormError("所选账号跨平台，一个任务只能发同一个平台");
      return;
    }
    const platformCode = accountMap[formAccountIds[0]]?.platform_code;
    setLoading(true);
    try {
      const payload: TaskCreatePayload = {
        name: formName.trim(),
        client_request_id: newClientRequestId("task"),
        task_type: formType,
        article_id: formType === "single" ? formArticleId : null,
        group_id: formType === "group_round_robin" ? formGroupId : null,
        accounts: formAccountIds.map((id, index) => ({ account_id: id, sort_order: index })),
        stop_before_publish: false,
        platform_code: platformCode,
      };
      const task = await singleFlight("task-create", () =>
        createTaskRequest(payload),
      );
      if (!task) return;
      setTasks((prev) => upsertTask(prev, task));
      setAutoRefreshTaskIds((prev) => new Set(prev).add(task.id));
      setShowCreateForm(false);
      setFormName("");
      setFormArticleId(null);
      setFormGroupId(null);
      setFormAccountIds([]);
      setFormError("");
      setPreview(null);
      setTaskPage(0);
      toast("任务已创建，正在等待执行", "success");
      await selectTask(task.id);
    } catch (error) {
      const msg = error instanceof Error ? error.message : "创建失败";
      toast(msg, "error");
      setFormError(msg);
    } finally {
      setLoading(false);
    }
  }

  async function loadPreview() {
    if (!formGroupId || formAccountIds.length === 0) return;
    setLoading(true);
    try {
      const result = await previewTaskAssignment({
          name: formName || "预览",
        task_type: formType,
        group_id: formGroupId,
        accounts: formAccountIds.map((id, index) => ({ account_id: id, sort_order: index })),
        stop_before_publish: false,
      });
      setPreview(result);
    } catch (error) {
      toast(error instanceof Error ? error.message : "预览失败", "error");
    } finally {
      setLoading(false);
    }
  }

  async function executeTask() {
    if (!selectedTaskId) return;
    setLoading(true);
    try {
      setAutoRefreshTaskIds((prev) => new Set(prev).add(selectedTaskId));
      await singleFlight(`task-execute-${selectedTaskId}`, () =>
        executeTaskRequest(selectedTaskId),
      );
      await refreshDetail(selectedTaskId);
      toast("已启动", "success");
    } catch (error) {
      toast(error instanceof Error ? error.message : "启动失败", "error");
    } finally {
      setLoading(false);
    }
  }

  async function cancelTask() {
    if (!selectedTaskId) return;
    setLoading(true);
    try {
      await singleFlight(`task-cancel-${selectedTaskId}`, () =>
        cancelTaskRequest(selectedTaskId),
      );
      setAutoRefreshTaskIds((prev) => {
        if (!prev.has(selectedTaskId)) return prev;
        const next = new Set(prev);
        next.delete(selectedTaskId);
        return next;
      });
      await refreshDetail(selectedTaskId);
      toast("已请求取消，将停止后续未开始的发布", "success");
    } catch (error) {
      toast(error instanceof Error ? error.message : "取消失败", "error");
    } finally {
      setLoading(false);
    }
  }

  async function resolveUserInput(recordId: number) {
    setLoading(true);
    try {
      await singleFlight(`record-resolve-${recordId}`, () =>
        resolveRecordUserInput(recordId),
      );
      if (selectedTaskId) {
        setAutoRefreshTaskIds((prev) => new Set(prev).add(selectedTaskId));
        await refreshDetail(selectedTaskId);
      }
      toast("已完成，继续执行", "success");
    } catch (error) {
      toast(error instanceof Error ? error.message : "操作失败", "error");
    } finally {
      setLoading(false);
    }
  }

  async function manualConfirm(recordId: number, outcome: "succeeded" | "failed") {
    setLoading(true);
    try {
      await singleFlight(`record-confirm-${recordId}-${outcome}`, () =>
        manualConfirmRecord(recordId, { outcome }),
      );
      if (selectedTaskId) {
        setAutoRefreshTaskIds((prev) => new Set(prev).add(selectedTaskId));
        await refreshDetail(selectedTaskId);
      }
      toast(outcome === "succeeded" ? "已确认发布" : "已标记失败", "success");
    } catch (error) {
      toast(error instanceof Error ? error.message : "操作失败", "error");
    } finally {
      setLoading(false);
    }
  }

  async function retryRecord(recordId: number, opts?: { force?: boolean }) {
    setLoading(true);
    try {
      await singleFlight(`record-retry-${recordId}`, () =>
        retryRecordRequest(recordId, opts),
      );
      if (selectedTaskId) {
        setAutoRefreshTaskIds((prev) => new Set(prev).add(selectedTaskId));
        await singleFlight(`task-execute-${selectedTaskId}`, () =>
          executeTaskRequest(selectedTaskId),
        );
        await refreshDetail(selectedTaskId);
      }
      toast(opts?.force ? "已强制重发" : "重试已启动", "success");
    } catch (error) {
      toast(error instanceof Error ? error.message : "重试失败", "error");
    } finally {
      setLoading(false);
    }
  }

  function toggleAccount(accountId: number) {
    if (formType === "single") {
      setFormAccountIds([accountId]);
    } else {
      setFormAccountIds((prev) =>
        prev.includes(accountId) ? prev.filter((id) => id !== accountId) : [...prev, accountId],
      );
    }
    setPreview(null);
  }

  const validAccounts = accounts.filter((a) => a.status !== "deleted" && (!formPlatformFilter || a.platform_code === formPlatformFilter));
  const canExecute = selectedTask && selectedTask.status === "pending";
  const canCancel = selectedTask && !selectedTask.cancel_requested && (selectedTask.status === "running" || selectedTask.status === "pending");

  return (
    <>
      <header className="topbar">
        <div>
          <p className="eyebrow">分发引擎</p>
          <h1>任务管理</h1>
        </div>
        <div className="topActions">
        </div>
      </header>

      <section className="taskGrid">
        <div className="listPane">
          <button
            className="primaryButton"
            style={{ width: "100%", marginBottom: 12 }}
            type="button"
            onClick={() => { setShowCreateForm((v) => !v); setPreview(null); }}
          >
            <Plus size={16} />
            {showCreateForm ? "收起" : "创建任务"}
          </button>

          {showCreateForm ? (
            <div className="createForm">
              <label>
                任务名称
                <input value={formName} placeholder="例如：头条号5月第一批" onChange={(e) => setFormName(e.target.value)} />
              </label>
              <label>
                任务类型
                <select
                  value={formType}
                  onChange={(e) => { setFormType(e.target.value as "single" | "group_round_robin"); setPreview(null); }}
                >
                  <option value="single">单篇发布</option>
                  <option value="group_round_robin">分组轮询</option>
                </select>
              </label>
              {formType === "single" ? (
                <label>
                  文章
                  <select
                    value={formArticleId ?? ""}
                    onChange={(e) => setFormArticleId(Number(e.target.value) || null)}
                  >
                    <option value="">请选择文章</option>
                    {articles.map((a) => (
                      <option key={a.id} value={a.id}>{a.title}</option>
                    ))}
                  </select>
                </label>
              ) : (
                <label>
                  文章分组
                  <select
                    value={formGroupId ?? ""}
                    onChange={(e) => { setFormGroupId(Number(e.target.value) || null); setPreview(null); }}
                  >
                    <option value="">请选择分组</option>
                    {groups.map((g) => (
                      <option key={g.id} value={g.id}>{g.name}（{g.items.length} 篇）</option>
                    ))}
                  </select>
                </label>
              )}
              <div>
                <p style={{ margin: "0 0 6px", fontSize: 13, color: "#475569" }}>发布账号</p>
                <div style={{ display: "flex", gap: 8, marginBottom: 8, alignItems: "center" }}>
                  <select
                    style={{ flex: 1, height: 32, border: "1px solid var(--hair)", borderRadius: "var(--r-sm)", padding: "0 8px", fontSize: 12 }}
                    value={formPlatformFilter}
                    onChange={(e) => { setFormPlatformFilter(e.target.value); setFormAccountIds([]); }}
                  >
                    <option value="">全部平台</option>
                    {platforms.map((p) => (
                      <option key={p.code} value={p.code}>{p.name}</option>
                    ))}
                  </select>
                  {formType === "single" && <span style={{ fontSize: 12, color: "#e67e22" }}>单篇只能选一个账号</span>}
                </div>
                <div style={{ maxHeight: "200px", overflowY: "auto", border: "1px solid #e2e8f0", borderRadius: "4px", padding: "8px" }}>
                  {validAccounts.map((a) => (
                    <label key={a.id} className="checkLine">
                      <input type={formType === "single" ? "radio" : "checkbox"} name="formAccount" checked={formAccountIds.includes(a.id)} onChange={() => toggleAccount(a.id)} />
                      <span>{a.display_name}</span>
                      <span style={{ fontSize: 11, color: "#94a3b8", marginLeft: "4px" }}>({a.platform_name})</span>
                      {a.status !== "valid" && <span style={{ fontSize: 11, color: "#94a3b8", marginLeft: "4px" }}>[{a.status}]</span>}
                    </label>
                  ))}
                </div>
                {validAccounts.length === 0 ? <p className="emptyText">暂无账号</p> : null}
              </div>
              {formType === "group_round_robin" && formGroupId && formAccountIds.length > 0 ? (
                <button className="secondaryButton" style={{ width: "100%" }} type="button" disabled={loading} onClick={() => void loadPreview()}>
                  预览分配
                </button>
              ) : null}
              {preview ? (
                <div className="previewBox">
                  <p style={{ margin: "0 0 6px", fontSize: 13, color: "#475569" }}>
                    {preview.article_count} 篇 · {preview.account_count} 个账号
                  </p>
                  {preview.items.map((item) => (
                    <div key={item.position} className="previewRow">
                      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {articleMap[item.article_id]?.title ?? `文章 ${item.article_id}`}
                      </span>
                      <span style={{ flexShrink: 0, color: "#64748b" }}>
                        {accountMap[item.account_id]?.display_name ?? `账号 ${item.account_id}`}
                      </span>
                    </div>
                  ))}
                </div>
              ) : null}
              {formError ? (
                <div style={{ padding: "8px 12px", background: "var(--red-soft)", color: "var(--red)", borderRadius: "var(--r)", fontSize: 13 }}>
                  {formError}
                </div>
              ) : null}
              <button className="primaryButton" style={{ width: "100%" }} type="button" disabled={loading} onClick={() => void createTask()}>
                创建并入队
              </button>
            </div>
          ) : null}

          <div className="articleList taskList">
            {pagedTasks.map((task) => (
              <button
                key={task.id}
                className={`taskItem ${task.id === selectedTaskId ? "selected" : ""}`}
                type="button"
                onClick={() => void selectTask(task.id)}
              >
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, marginBottom: 4 }}>
                  <strong style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}>
                    {task.name}
                  </strong>
                  <span className={`badge ${task.status}`}>{statusLabel(task.status)}</span>
                </div>
                <small style={{ color: "#64748b", fontSize: 12 }}>
                  {task.task_type === "single" ? "单篇" : "分组轮询"} · {task.record_count} 条 · {formatDate(task.created_at)}
                </small>
              </button>
            ))}
            {tasks.length === 0 ? <p className="emptyText">暂无任务</p> : null}
          </div>
          <Pagination
            page={taskPage}
            totalPages={totalTaskPages}
            loading={loading}
            onPrev={() => setTaskPage((page) => Math.max(0, page - 1))}
            onNext={() => setTaskPage((page) => Math.min(totalTaskPages - 1, page + 1))}
          />
        </div>

        {selectedTask ? (
          <div className="taskDetail">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12, marginBottom: 14 }}>
              <div>
                <h2 style={{ margin: "0 0 4px" }}>{selectedTask.name}</h2>
                <small style={{ color: "#64748b", fontSize: 13 }}>
                  {selectedTask.task_type === "single" ? "单篇发布" : "分组轮询"} · {platformMap[selectedTask.platform_code]?.name ?? selectedTask.platform_code}
                  {selectedTask.started_at ? ` · 开始于 ${formatDateTime(selectedTask.started_at)}` : ""}
                  {selectedTask.cancel_requested ? " · 已请求取消" : ""}
                </small>
              </div>
              <span className={`badge ${selectedTask.status}`}>{statusLabel(selectedTask.status)}</span>
            </div>

            <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
              {canExecute ? (
                <button className="primaryButton" type="button" disabled={loading} onClick={() => void executeTask()}>
                  <Send size={15} />
                  唤醒执行
                </button>
              ) : null}
              {canCancel ? (
                <button className="dangerButton" type="button" disabled={loading} onClick={() => void cancelTask()}>
                  停止后续发布
                </button>
              ) : null}
            </div>

            <hr className="sectionDivider" />
            <h2 style={{ marginBottom: 12 }}>发布记录</h2>
            <div style={{ display: "grid", gap: 10, marginBottom: 20 }}>
              {records.map((record) => {
                const article = articleMap[record.article_id];
                const account = accountMap[record.account_id];
                return (
                  <div key={record.id} className="recordItem">
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
                      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}>
                        {article?.title ?? `文章 ${record.article_id}`}
                      </span>
                      <span className={`badge ${record.status}`}>{statusLabel(record.status)}</span>
                    </div>
                    <small style={{ color: "#64748b", fontSize: 13 }}>
                      {account?.display_name ?? `账号 ${record.account_id}`}
                      {record.retry_of_record_id ? ` · 重试自 #${record.retry_of_record_id}` : ""}
                    </small>
                    {record.publish_url ? (
                      <small>
                        <a href={record.publish_url} target="_blank" rel="noreferrer" style={{ color: "#214f7a" }}>
                          查看已发布链接
                        </a>
                      </small>
                    ) : null}
                    {record.error_message ? (
                      <small style={{ color: "#dc2626" }}>{record.error_message}</small>
                    ) : null}
                    {record.queue_reason ? (
                      <small style={{ color: "#a16207" }}>{record.queue_reason}</small>
                    ) : null}

                    {record.status === "failed" && !records.some((r) => r.retry_of_record_id === record.id) ? (
                      record.failure_kind === "commit_uncertain" ? (
                        <div style={{ marginTop: 4 }}>
                          <small style={{ color: "#a16207", display: "block", marginBottom: 6 }}>
                            已提交但结果未知，请先到平台核对是否已发布。
                          </small>
                          <button
                            className="secondaryButton"
                            type="button"
                            disabled={loading}
                            style={{ justifySelf: "start" }}
                            onClick={() => void retryRecord(record.id, { force: true })}
                          >
                            <RefreshCw size={13} />
                            核对后强制重发
                          </button>
                        </div>
                      ) : (
                        <button
                          className="secondaryButton"
                          type="button"
                          disabled={loading}
                          style={{ justifySelf: "start" }}
                          onClick={() => void retryRecord(record.id)}
                        >
                          <RefreshCw size={13} />
                          重试
                        </button>
                      )
                    ) : null}
                    {record.status === "waiting_user_input" ? (
                      <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8, flexWrap: "wrap" }}>
                        {record.novnc_url ? (
                          <button
                            className="primaryButton"
                            type="button"
                            onClick={() => openRemoteBrowser(record.novnc_url!)}
                          >
                            打开远程浏览器
                          </button>
                        ) : null}
                        <button
                          className="secondaryButton"
                          type="button"
                          disabled={loading}
                          onClick={() => void resolveUserInput(record.id)}
                        >
                          操作完成
                        </button>
                        <small style={{ color: "#64748b" }}>
                          在远程浏览器完成操作后点击「操作完成」继续发布
                        </small>
                      </div>
                    ) : null}
                    {record.status === "waiting_manual_publish" ? (
                      <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8, flexWrap: "wrap" }}>
                        {record.novnc_url ? (
                          <button
                            className="secondaryButton"
                            type="button"
                            onClick={() => openRemoteBrowser(record.novnc_url!)}
                          >
                            打开远程浏览器
                          </button>
                        ) : null}
                        <button
                          className="primaryButton"
                          type="button"
                          disabled={loading}
                          onClick={() => void manualConfirm(record.id, "succeeded")}
                        >
                          确认发布
                        </button>
                        <button
                          className="dangerButton"
                          type="button"
                          disabled={loading}
                          onClick={() => void manualConfirm(record.id, "failed")}
                        >
                          标记失败
                        </button>
                        <small style={{ color: "#64748b" }}>
                          已在浏览器中完成发布后点击「确认发布」
                        </small>
                      </div>
                    ) : null}
                  </div>
                );
              })}
              {records.length === 0 ? <p className="emptyText">暂无发布记录</p> : null}
            </div>

            <hr className="sectionDivider" />
            <h2 style={{ marginBottom: 12 }}>执行日志</h2>
            <div className="logList">
              {logs.map((log) => (
                <div key={log.id}>
                  <div className="logItem">
                    <span className={`logLevel ${log.level}`}>{log.level.toUpperCase()}</span>
                    <span style={{ flex: 1 }}>{log.message}</span>
                    <small style={{ color: "#94a3b8", flexShrink: 0 }}>
                      {formatTime(log.created_at)}
                    </small>
                  </div>
                  {log.screenshot_asset_id ? (
                    <img
                      src={`/api/assets/${log.screenshot_asset_id}`}
                      alt="失败截图"
                      loading="lazy"
                      style={{ maxWidth: "100%", marginTop: 4, marginBottom: 4, borderRadius: 4, border: "1px solid #e2e8f0", display: "block" }}
                    />
                  ) : null}
                </div>
              ))}
              {logs.length === 0 ? <p className="emptyText" style={{ margin: "6px 0" }}>暂无日志</p> : null}
            </div>
          </div>
        ) : (
          <div className="taskDetail" style={{ display: "grid", placeItems: "center", minHeight: 260, color: "#94a3b8" }}>
            <p style={{ margin: 0 }}>选择左侧任务查看详情</p>
          </div>
        )}
      </section>
    </>
  );
}
