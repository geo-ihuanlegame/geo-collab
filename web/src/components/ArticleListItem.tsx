import React from "react";
import type { ArticleSummary, ReviewStatus } from "../types";

export function formatArticleTemplateSource(article: Pick<ArticleSummary, "source_template_id" | "source_template_name">): string {
  const name = article.source_template_name;
  if (article.source_template_id != null) {
    return `ID：${article.source_template_id}  模板：${name || "—"}`;
  }
  return `模板：${name || "—"}`;
}

export function ReviewBadge({ status }: { status: ReviewStatus }) {
  const approved = status === "approved";
  return (
    <span className={`badge ${approved ? "succeeded" : "waiting_manual_publish"}`}>
      {approved ? "已过审" : "待审核"}
    </span>
  );
}

export const ArticleListItem = React.memo(function ArticleListItem({
  article,
  draftId,
  selectedIds,
  onToggle,
  onSelect,
}: {
  article: ArticleSummary;
  draftId: number | null;
  selectedIds: number[];
  onToggle: (id: number) => void;
  onSelect: (article: ArticleSummary) => void;
}) {
  return (
    <article className={`articleItem ${article.id === draftId ? "selected" : ""}`}>
      <label className="checkLine">
        <input checked={selectedIds.includes(article.id)} type="checkbox" onChange={() => onToggle(article.id)} />
      </label>
      <button type="button" onClick={() => onSelect(article)}>
        <span style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
          <strong>{article.title}</strong>
          <span
            className="badge"
            style={{ flexShrink: 0, fontFamily: "var(--mono, monospace)", color: "var(--text-muted, #888)" }}
            title="数据库 ID"
          >
            ID {article.id}
          </span>
        </span>
        {article.auto_review_score != null && article.auto_review_score >= 0 ? (
          <span
            className="articleSourceLine"
            style={{ display: "flex", alignItems: "center", gap: 4 }}
            title="MCP 生文自评分（0-100，取 auto_review_decisions 最新一条）"
          >
            评分：
            <span
              className="badge"
              style={{
                color:
                  article.auto_review_score >= 70
                    ? "var(--green, #3fb950)"
                    : article.auto_review_score >= 40
                      ? "var(--amber, #d29922)"
                      : "var(--red, #f85149)",
              }}
            >
              {article.auto_review_score}
            </span>
          </span>
        ) : null}
        <span className="articleSourceLine">智能体：{article.source_agent_name || "—"}</span>
        <span className="articleSourceRow">
          <span className="articleSourceLine">{formatArticleTemplateSource(article)}</span>
          <small>
            {new Date(article.updated_at).toLocaleString()}
            {article.published_count > 0 ? <span style={{ color: "var(--green)", marginLeft: 6 }}>· 已发布 {article.published_count} 次</span> : null}
          </small>
        </span>
      </button>
      <div className="articleItemBadge">
        <ReviewBadge status={article.review_status} />
      </div>
    </article>
  );
});
