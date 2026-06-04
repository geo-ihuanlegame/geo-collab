import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  Check,
  ChevronDown,
  CircleAlert,
  ExternalLink,
  Hourglass,
  Loader,
  X,
} from "lucide-react";
import { getSchemeRun, listSchemeRuns } from "../../api/ai-generation";
import type { SchemeRun, SchemeRunStatus, SchemeRunSummary, SchemeRunTask } from "../../types";

const RUN_STATUS_META: Record<SchemeRunStatus, { label: string; bg: string; fg: string }> = {
  pending: { label: "等待中", bg: "var(--cream-2)", fg: "var(--fg-3)" },
  running: { label: "运行中", bg: "var(--accent-soft)", fg: "var(--accent-deep)" },
  done: { label: "已完成", bg: "var(--green-soft)", fg: "var(--green)" },
  partial_failed: { label: "部分失败", bg: "var(--yellow-soft)", fg: "var(--yellow)" },
  failed: { label: "全部失败", bg: "var(--red-soft)", fg: "var(--red)" },
};

const TERMINAL = new Set<SchemeRunStatus>(["done", "partial_failed", "failed"]);

function relTime(iso: string): string {
  const d = new Date(iso).getTime();
  const diff = Date.now() - d;
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  return `${Math.floor(diff / 86_400_000)} 天前`;
}

function TaskCard({
  task,
  onOpenArticle,
}: {
  task: SchemeRunTask;
  onOpenArticle: () => void;
}) {
  const typeLabel = task.question_type || "未分类";
  let icon = <Hourglass size={13} color="var(--fg-3)" />;
  let circleBg = "var(--cream-2)";
  if (task.status === "done") {
    icon = <Check size={13} color="var(--green)" />;
    circleBg = "var(--green-soft)";
  } else if (task.status === "running") {
    icon = <Loader size={13} color="var(--accent-deep)" />;
    circleBg = "var(--accent-soft)";
  } else if (task.status === "failed") {
    icon = <X size={13} color="var(--red)" />;
    circleBg = "var(--red-soft)";
  }
  return (
    <div className="runTask">
      <span className="runTaskIcon" style={{ background: circleBg }}>
        {icon}
      </span>
      <span className="runTaskInfo">
        <span className="runTaskLabel">{typeLabel}</span>
        <span className="runTaskTpl">
          {task.status === "failed"
            ? "—"
            : task.status === "pending"
              ? "待分配"
              : task.actual_prompt_template_id
                ? `模板 #${task.actual_prompt_template_id}`
                : "—"}
        </span>
      </span>
      {task.status === "done" && task.article_id ? (
        <button className="runTaskRes schemeLink" type="button" onClick={onOpenArticle}>
          <ArrowRight size={13} color="var(--fg-3)" />
          文章 #{task.article_id}
        </button>
      ) : task.status === "failed" ? (
        <span className="runTaskRes" style={{ color: "var(--red)" }}>
          <CircleAlert size={13} color="var(--red)" />
          {task.error_message ? task.error_message.slice(0, 28) : "失败"}
        </span>
      ) : task.status === "running" ? (
        <span className="runTaskRes" style={{ color: "var(--fg-3)" }}>
          生成中…
        </span>
      ) : (
        <span className="runTaskRes" style={{ color: "var(--fg-3)" }}>
          等待中
        </span>
      )}
    </div>
  );
}

export function RunDetailModal({
  schemeId,
  schemeName,
  initialRunId,
  onClose,
  onNavigateToContent,
}: {
  schemeId: number;
  schemeName: string;
  initialRunId: number;
  onClose: () => void;
  onNavigateToContent: () => void;
}) {
  const [runId, setRunId] = useState(initialRunId);
  const [run, setRun] = useState<SchemeRun | null>(null);
  const [runs, setRuns] = useState<SchemeRunSummary[]>([]);
  const [switcherOpen, setSwitcherOpen] = useState(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stop = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  useEffect(() => {
    listSchemeRuns(schemeId).then(setRuns).catch(() => {});
  }, [schemeId]);

  useEffect(() => {
    let alive = true;
    stop();
    setRun(null);
    const tick = async () => {
      try {
        const r = await getSchemeRun(runId);
        if (!alive) return;
        setRun(r);
        if (TERMINAL.has(r.status)) stop();
      } catch {
        /* 网络抖动，继续轮询 */
      }
    };
    tick();
    timerRef.current = setInterval(tick, 1500);
    return () => {
      alive = false;
      stop();
    };
  }, [runId, stop]);

  const tasks = run?.tasks ?? [];
  const total = tasks.length;
  const done = tasks.filter((t) => t.status === "done").length;
  const running = tasks.filter((t) => t.status === "running").length;
  const failed = tasks.filter((t) => t.status === "failed").length;
  const pending = tasks.filter((t) => t.status === "pending").length;
  const pct = total ? Math.round((done / total) * 100) : 0;
  const meta = run ? RUN_STATUS_META[run.status] : RUN_STATUS_META.pending;

  return (
    <div className="modalBackdrop" role="dialog" aria-modal="true" onClick={onClose}>
      <div
        className="schemePanel"
        style={{ width: "min(1000px, calc(100vw - 48px))" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="schemePanelHead" style={{ alignItems: "center" }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <h3>运行 #{runId}</h3>
            <p className="schemePanelHint">方案：{schemeName}</p>
          </div>

          <div className="runsSwitcher">
            <button
              className="runsSwitcherBtn"
              type="button"
              onClick={() => setSwitcherOpen((v) => !v)}
            >
              运行历史
              <ChevronDown size={15} color="var(--fg-3)" />
            </button>
            {switcherOpen && (
              <div className="runsPopover">
                {runs.length === 0 && (
                  <div style={{ padding: 10, fontSize: 12, color: "var(--fg-3)" }}>暂无运行记录</div>
                )}
                {runs.map((r) => {
                  const m = RUN_STATUS_META[r.status];
                  return (
                    <button
                      key={r.id}
                      type="button"
                      className={`runsPopItem${r.id === runId ? " active" : ""}`}
                      onClick={() => {
                        setRunId(r.id);
                        setSwitcherOpen(false);
                      }}
                    >
                      <span
                        style={{
                          width: 7,
                          height: 7,
                          borderRadius: "50%",
                          background: m.fg,
                          flexShrink: 0,
                        }}
                      />
                      <span style={{ flex: 1, display: "flex", flexDirection: "column", gap: 1 }}>
                        <span style={{ fontSize: 13, color: "var(--fg)" }}>运行 #{r.id}</span>
                        <span style={{ fontSize: 11, color: "var(--fg-3)" }}>
                          {m.label} · {relTime(r.created_at)}
                        </span>
                      </span>
                      {r.id === runId && <Check size={14} color="var(--accent-deep)" />}
                    </button>
                  );
                })}
              </div>
            )}
          </div>

          <div style={{ display: "flex", gap: 14, alignItems: "center" }}>
            <span
              className="schemeBadge"
              style={{ background: meta.bg, color: meta.fg, cursor: "default" }}
            >
              <span className="schemeBadgeDot" style={{ background: meta.fg }} />
              {meta.label}
            </span>
            <span style={{ fontSize: 13, color: "var(--fg-2)" }}>
              {done} / {total} 完成
            </span>
            <button className="iconButton" type="button" onClick={onClose}>
              ×
            </button>
          </div>
        </div>

        <div className="schemePanelBody">
          <div className="runSummary">
            <span style={{ fontSize: 13, color: "var(--fg)" }}>共 {total} 篇</span>
            <span className="runSummaryItem">
              <Check size={13} color="var(--green)" />
              {done} 完成
            </span>
            <span className="runSummaryItem">
              <Loader size={13} color="var(--accent-deep)" />
              {running} 进行中
            </span>
            <span className="runSummaryItem">
              <X size={13} color="var(--red)" />
              {failed} 失败
            </span>
            <span className="runSummaryItem">
              <Hourglass size={13} color="var(--fg-3)" />
              {pending} 等待
            </span>
          </div>

          <div className="runProgress">
            <div className="runProgressFill" style={{ width: `${pct}%` }} />
          </div>

          {total === 0 ? (
            <div className="schemeEmpty">正在准备任务…</div>
          ) : (
            <div className="runGrid">
              {tasks.map((t) => (
                <TaskCard key={t.id} task={t} onOpenArticle={onNavigateToContent} />
              ))}
            </div>
          )}
        </div>

        <div className="schemePanelFoot" style={{ justifyContent: "space-between" }}>
          <span style={{ fontSize: 12, color: "var(--fg-3)" }}>运行中可关闭，任务在后台继续</span>
          <div style={{ display: "flex", gap: 12 }}>
            <button className="secondaryButton" type="button" onClick={onClose}>
              关闭
            </button>
            <button
              className="primaryButton"
              type="button"
              onClick={onNavigateToContent}
              disabled={done === 0}
            >
              <ExternalLink size={14} />
              查看全部文章
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
