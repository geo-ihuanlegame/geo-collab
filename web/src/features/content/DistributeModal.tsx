import { useMemo, useState } from "react";
import { ArrowRight, Folder, Send } from "lucide-react";
import { Modal } from "../../components/Modal";
import { useToast } from "../../components/Toast";
import { autoDistribute } from "../../api/tasks";
import type { Account, ArticleSummary } from "../../types";

export type DistributeTarget =
  | { kind: "article"; article: ArticleSummary }
  | { kind: "group"; groupId: number; name: string; articles: ArticleSummary[] }
  | { kind: "selection"; articles: ArticleSummary[] };

interface Props {
  target: DistributeTarget;
  accounts: Account[];
  onClose: () => void;
  onDistributed?: () => void;
}

function accountIsValid(account: Account): boolean {
  return account.status === "valid";
}

function targetArticles(target: DistributeTarget): ArticleSummary[] {
  if (target.kind === "article") return [target.article];
  return target.articles;
}

function targetTitle(target: DistributeTarget): string {
  if (target.kind === "article") return target.article.title;
  if (target.kind === "group") return `方案组 · ${target.name}`;
  return `已选 ${target.articles.length} 篇文章`;
}

const PREVIEW_LIMIT = 4;

export function DistributeModal({ target, accounts, onClose, onDistributed }: Props) {
  const { toast } = useToast();
  const [submitting, setSubmitting] = useState(false);

  const articles = useMemo(() => targetArticles(target), [target]);

  // Group accounts by platform, valid first. Default-select all valid accounts.
  const platforms = useMemo(() => {
    const byPlatform = new Map<string, { code: string; name: string; accounts: Account[] }>();
    for (const account of accounts) {
      const entry = byPlatform.get(account.platform_code) ?? {
        code: account.platform_code,
        name: account.platform_name,
        accounts: [],
      };
      entry.accounts.push(account);
      byPlatform.set(account.platform_code, entry);
    }
    return [...byPlatform.values()].map((p) => ({
      ...p,
      accounts: p.accounts.slice().sort((a, b) => Number(accountIsValid(b)) - Number(accountIsValid(a))),
    }));
  }, [accounts]);

  const validIds = useMemo(() => accounts.filter(accountIsValid).map((a) => a.id), [accounts]);

  const [selectedIds, setSelectedIds] = useState<Set<number>>(() => new Set(validIds));

  const accountById = useMemo(() => new Map(accounts.map((a) => [a.id, a])), [accounts]);

  const orderedSelected = useMemo(
    // Preserve the account list order for a stable round-robin preview.
    () => accounts.filter((a) => accountIsValid(a) && selectedIds.has(a.id)).map((a) => a.id),
    [accounts, selectedIds],
  );

  function toggleAccount(account: Account) {
    if (!accountIsValid(account)) return;
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(account.id)) next.delete(account.id);
      else next.add(account.id);
      return next;
    });
  }

  function togglePlatform(platformAccounts: Account[]) {
    const valid = platformAccounts.filter(accountIsValid);
    const allSelected = valid.length > 0 && valid.every((a) => selectedIds.has(a.id));
    setSelectedIds((prev) => {
      const next = new Set(prev);
      for (const account of valid) {
        if (allSelected) next.delete(account.id);
        else next.add(account.id);
      }
      return next;
    });
  }

  const selectedCount = orderedSelected.length;

  const preview = useMemo(() => {
    if (orderedSelected.length === 0) return [];
    return articles.map((article, index) => ({
      article,
      accountId: orderedSelected[index % orderedSelected.length],
    }));
  }, [articles, orderedSelected]);

  async function handleConfirm() {
    if (selectedCount === 0) {
      toast("请至少选择一个有效账号", "error");
      return;
    }
    setSubmitting(true);
    try {
      const accountIds = orderedSelected;
      if (target.kind === "group") {
        await autoDistribute({ group_id: target.groupId, account_ids: accountIds, name: target.name });
      } else if (target.kind === "article") {
        await autoDistribute({ article_id: target.article.id, account_ids: accountIds });
      } else {
        // Multi-article selection: one task per article, round-robined across the
        // selected accounts so the result matches the preview (article i → account i%M).
        // (A "single" task round-robins to its first account, so pass one account each.)
        for (let index = 0; index < target.articles.length; index++) {
          const article = target.articles[index];
          const accountId = accountIds[index % accountIds.length];
          await autoDistribute({ article_id: article.id, account_ids: [accountId] });
        }
      }
      toast("已创建自动分发任务", "success");
      onDistributed?.();
      onClose();
    } catch (error) {
      toast(error instanceof Error ? error.message : "自动分发失败", "error");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Modal
      title="自动分发"
      width={520}
      maxHeight={760}
      onClose={onClose}
      footer={
        <>
          <button type="button" onClick={onClose} disabled={submitting}>
            取消
          </button>
          <button type="button" onClick={() => void handleConfirm()} disabled={submitting || selectedCount === 0}>
            <Send size={14} style={{ marginRight: 6, verticalAlign: "-2px" }} />
            确认分发
          </button>
        </>
      }
    >
      <div className="distributeBody">
        <div className="distributeTarget">
          <div className="distributeTargetIcon">
            <Folder size={18} />
          </div>
          <div className="distributeTargetText">
            <strong>{targetTitle(target)}</strong>
            <span>{articles.length} 篇文章 · 已全部过审</span>
          </div>
        </div>

        <div className="distributeSection">
          <div className="distributeSectionHead">
            <span className="distributeSectionLabel">选择账号</span>
            <span className="distributeSectionCount">已选 {selectedCount} / {validIds.length}</span>
          </div>

          {platforms.length === 0 ? (
            <p className="emptyText">暂无可用账号</p>
          ) : (
            platforms.map((platform) => {
              const valid = platform.accounts.filter(accountIsValid);
              const allSelected = valid.length > 0 && valid.every((a) => selectedIds.has(a.id));
              return (
                <div className="distributePlatform" key={platform.code}>
                  <div className="distributePlatformHead">
                    <span className="distributePlatformName">{platform.name}</span>
                    <button
                      type="button"
                      className="distributeAllBtn"
                      disabled={valid.length === 0}
                      onClick={() => togglePlatform(platform.accounts)}
                    >
                      {allSelected ? "取消全选" : "全选"}
                    </button>
                  </div>
                  {platform.accounts.map((account) => {
                    const isValid = accountIsValid(account);
                    const checked = isValid && selectedIds.has(account.id);
                    return (
                      <label
                        key={account.id}
                        className={`distributeAcct ${checked ? "selected" : ""} ${isValid ? "" : "disabled"}`}
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          disabled={!isValid}
                          onChange={() => toggleAccount(account)}
                        />
                        <span className="distributeAvatar">{account.display_name.slice(0, 1) || "?"}</span>
                        <div className="distributeAcctText">
                          <strong>{account.display_name}</strong>
                          <span>{account.platform_name}</span>
                        </div>
                        <span className={`badge ${isValid ? "valid" : "expired"}`}>
                          {isValid ? "已登录" : "已失效"}
                        </span>
                      </label>
                    );
                  })}
                </div>
              );
            })
          )}
        </div>

        <div className="distributePreview">
          <div className="distributePreviewHead">
            <span className="distributePreviewLabel">轮询投放预览</span>
            <span className="distributePreviewSummary">
              {articles.length} 篇 → {selectedCount} 个账号
            </span>
          </div>
          {selectedCount === 0 ? (
            <p className="emptyText" style={{ margin: 0 }}>请选择账号后预览轮询投放</p>
          ) : (
            <>
              {preview.slice(0, PREVIEW_LIMIT).map((row, index) => {
                const account = accountById.get(row.accountId);
                return (
                  <div className="distributeMapRow" key={row.article.id}>
                    <span className="distributeMapIndex">{index + 1}</span>
                    <span className="distributeMapArticle">{row.article.title}</span>
                    <ArrowRight size={13} className="distributeMapArrow" />
                    <span className="distributeMapAccount">{account?.display_name ?? `账号 ${row.accountId}`}</span>
                  </div>
                );
              })}
              {preview.length > PREVIEW_LIMIT ? (
                <p className="distributeMapMore">…余 {preview.length - PREVIEW_LIMIT} 篇按相同规则轮流</p>
              ) : null}
            </>
          )}
        </div>
      </div>
    </Modal>
  );
}
