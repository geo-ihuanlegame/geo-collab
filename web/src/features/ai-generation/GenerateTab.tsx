import { useCallback, useEffect, useMemo, useState } from "react";
import { ChevronRight, Database, Layers, Pencil, Play, Plus, Trash2 } from "lucide-react";
import {
  deleteScheme,
  listAiEngines,
  listQuestionPools,
  listSchemeRuns,
  listSchemes,
  patchScheme,
  startSchemeRun,
} from "../../api/ai-generation";
import { useToast } from "../../components/Toast";
import type { AiEngine, QuestionPool, Scheme, SchemeRunStatus, SchemeRunSummary } from "../../types";
import { PoolManagerModal } from "./PoolManagerModal";
import { RunDetailModal } from "./RunDetailModal";
import { SchemeEditorModal } from "./SchemeEditorModal";

const RUN_STATUS_META: Record<SchemeRunStatus, { label: string; bg: string; fg: string }> = {
  pending: { label: "排队中", bg: "var(--cream-2)", fg: "var(--fg-3)" },
  running: { label: "运行中", bg: "var(--accent-soft)", fg: "var(--accent-deep)" },
  done: { label: "已完成", bg: "var(--green-soft)", fg: "var(--green)" },
  partial_failed: { label: "部分失败", bg: "var(--yellow-soft)", fg: "var(--yellow)" },
  failed: { label: "全部失败", bg: "var(--red-soft)", fg: "var(--red)" },
};

export function GenerateTab({ onNavigateToContent }: { onNavigateToContent: () => void }) {
  const { toast } = useToast();
  const [schemes, setSchemes] = useState<Scheme[]>([]);
  const [pools, setPools] = useState<QuestionPool[]>([]);
  const [engines, setEngines] = useState<AiEngine[]>([]);
  const [lastRuns, setLastRuns] = useState<Record<number, SchemeRunSummary | null>>({});
  const [loading, setLoading] = useState(true);

  const [editorOpen, setEditorOpen] = useState(false);
  const [editingScheme, setEditingScheme] = useState<Scheme | null>(null);
  const [poolMgrOpen, setPoolMgrOpen] = useState(false);
  const [runModal, setRunModal] = useState<{
    schemeId: number;
    schemeName: string;
    runId: number;
  } | null>(null);
  const [runningId, setRunningId] = useState<number | null>(null);

  const fetchLastRuns = useCallback(async (list: Scheme[]) => {
    const entries = await Promise.all(
      list.map(async (s) => {
        try {
          const rs = await listSchemeRuns(s.id);
          return [s.id, rs[0] ?? null] as const;
        } catch {
          return [s.id, null] as const;
        }
      }),
    );
    setLastRuns(Object.fromEntries(entries));
  }, []);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const list = await listSchemes();
      setSchemes(list);
      fetchLastRuns(list);
    } catch (e) {
      toast(e instanceof Error ? e.message : "加载方案失败", "error");
    } finally {
      setLoading(false);
    }
  }, [fetchLastRuns, toast]);

  const reloadPools = useCallback(() => {
    listQuestionPools().then(setPools).catch(() => {});
  }, []);

  useEffect(() => {
    reload();
    reloadPools();
    listAiEngines().then(setEngines).catch(() => {});
  }, [reload, reloadPools]);

  const poolName = (id: number) => pools.find((p) => p.id === id)?.name ?? `#${id}`;
  const engineLabel = (model: string) =>
    engines.find((e) => e.model === model)?.label ?? model;
  const totalArticles = (s: Scheme) => s.lines.reduce((sum, l) => sum + l.article_count, 0);

  function onNewScheme() {
    if (pools.length === 0) {
      toast("请先在「问题池」里创建并同步一个问题池", "error");
      setPoolMgrOpen(true);
      return;
    }
    setEditingScheme(null);
    setEditorOpen(true);
  }

  async function onToggle(s: Scheme) {
    try {
      const updated = await patchScheme(s.id, { is_enabled: !s.is_enabled });
      setSchemes((ls) => ls.map((x) => (x.id === s.id ? updated : x)));
    } catch (e) {
      toast(e instanceof Error ? e.message : "操作失败", "error");
    }
  }

  async function onDelete(s: Scheme) {
    if (!window.confirm(`确定删除方案「${s.name}」？历史运行记录会保留。`)) return;
    try {
      await deleteScheme(s.id);
      setSchemes((ls) => ls.filter((x) => x.id !== s.id));
      toast("已删除方案", "success");
    } catch (e) {
      toast(e instanceof Error ? e.message : "删除失败", "error");
    }
  }

  async function onRun(s: Scheme) {
    setRunningId(s.id);
    try {
      const { run_id } = await startSchemeRun(s.id);
      setRunModal({ schemeId: s.id, schemeName: s.name, runId: run_id });
      // 刷新该方案的最近运行
      listSchemeRuns(s.id)
        .then((rs) => setLastRuns((m) => ({ ...m, [s.id]: rs[0] ?? null })))
        .catch(() => {});
    } catch (e) {
      toast(e instanceof Error ? e.message : "启动运行失败", "error");
    } finally {
      setRunningId(null);
    }
  }

  function onOpenLastRun(s: Scheme) {
    const lr = lastRuns[s.id];
    if (lr) setRunModal({ schemeId: s.id, schemeName: s.name, runId: lr.id });
  }

  const hasSchemes = schemes.length > 0;
  const headerActions = useMemo(
    () => (
      <div style={{ display: "flex", gap: 10 }}>
        <button className="secondaryButton" type="button" onClick={() => setPoolMgrOpen(true)}>
          <Database size={14} />
          问题池
        </button>
        <button className="primaryButton" type="button" onClick={onNewScheme}>
          <Plus size={14} />
          新建方案
        </button>
      </div>
    ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [pools.length],
  );

  return (
    <div className="schemeLib">
      <div className="schemeLibHead">
        <div>
          <h2 className="schemeLibTitle">方案</h2>
          <p className="schemeLibSub">把问题类型组合成可复用的生成方案，一键批量出稿</p>
        </div>
        {headerActions}
      </div>

      {loading ? (
        <div className="schemeEmpty">加载中…</div>
      ) : !hasSchemes ? (
        <div className="schemeEmpty">
          <Layers size={40} style={{ opacity: 0.2 }} />
          <p style={{ marginTop: 12 }}>还没有方案。点右上「新建方案」，选问题池、勾问题、设文章数即可。</p>
        </div>
      ) : (
        <div className="schemeList">
          {schemes.map((s) => {
            const lr = lastRuns[s.id];
            const meta = lr ? RUN_STATUS_META[lr.status] : null;
            return (
              <div className="schemeCard" key={s.id}>
                <div className="schemeCardInfo">
                  <span className="schemeCardName">{s.name}</span>
                  <div className="schemeCardMeta">
                    <span className="schemeCardPool">
                      <Database size={13} />
                      {poolName(s.pool_id)}
                    </span>
                    <span className="schemeDot" />
                    <span>
                      {s.lines.length} 个问题类型 · 共 {totalArticles(s)} 篇
                    </span>
                    {s.ai_engine && (
                      <>
                        <span className="schemeDot" />
                        <span>引擎 {engineLabel(s.ai_engine)}</span>
                      </>
                    )}
                  </div>
                </div>

                <div className="schemeCardCtrls">
                  {meta ? (
                    <button
                      className="schemeBadge"
                      type="button"
                      style={{ background: meta.bg, color: meta.fg }}
                      title="查看运行明细"
                      onClick={() => onOpenLastRun(s)}
                    >
                      <span className="schemeBadgeDot" style={{ background: meta.fg }} />
                      {meta.label}
                      <ChevronRight size={13} />
                    </button>
                  ) : (
                    <span
                      className="schemeBadge"
                      style={{ background: "var(--cream-2)", color: "var(--fg-3)", cursor: "default" }}
                    >
                      <span className="schemeBadgeDot" style={{ background: "var(--hair-2)" }} />
                      未运行
                    </span>
                  )}

                  <button
                    type="button"
                    className={`schemeToggle ${s.is_enabled ? "on" : "off"}`}
                    title={s.is_enabled ? "已启用，点击停用" : "已停用，点击启用"}
                    onClick={() => onToggle(s)}
                  >
                    <span className="knob" />
                  </button>

                  <button
                    type="button"
                    className="schemeIconBtn"
                    title={s.is_enabled ? "运行方案" : "方案已停用，无法运行"}
                    disabled={!s.is_enabled || runningId === s.id}
                    onClick={() => onRun(s)}
                  >
                    <Play size={16} />
                  </button>
                  <button
                    type="button"
                    className="schemeIconBtn"
                    title="编辑方案"
                    onClick={() => {
                      setEditingScheme(s);
                      setEditorOpen(true);
                    }}
                  >
                    <Pencil size={16} />
                  </button>
                  <button
                    type="button"
                    className="schemeIconBtn danger"
                    title="删除方案"
                    onClick={() => onDelete(s)}
                  >
                    <Trash2 size={16} />
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {editorOpen && (
        <SchemeEditorModal
          scheme={editingScheme}
          pools={pools}
          onClose={() => setEditorOpen(false)}
          onSaved={() => {
            setEditorOpen(false);
            reload();
          }}
        />
      )}
      {poolMgrOpen && (
        <PoolManagerModal
          pools={pools}
          onClose={() => setPoolMgrOpen(false)}
          onChanged={reloadPools}
        />
      )}
      {runModal && (
        <RunDetailModal
          schemeId={runModal.schemeId}
          schemeName={runModal.schemeName}
          initialRunId={runModal.runId}
          onClose={() => {
            setRunModal(null);
            // 关闭后刷新最近运行状态
            const sid = runModal.schemeId;
            listSchemeRuns(sid)
              .then((rs) => setLastRuns((m) => ({ ...m, [sid]: rs[0] ?? null })))
              .catch(() => {});
          }}
          onNavigateToContent={onNavigateToContent}
        />
      )}
    </div>
  );
}
