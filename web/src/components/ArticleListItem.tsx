import React from "react";
import type { ArticleSummary, ReviewStatus } from "../types";

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
        <strong>{article.title}</strong>
        <span>{article.author || "未填写作者"}</span>
        <small>
          {new Date(article.updated_at).toLocaleString()}
          {article.published_count > 0 ? <span style={{ color: "#16a34a", marginLeft: 6 }}>· 已发布 {article.published_count} 次</span> : null}
        </small>
      </button>
      <div className="articleItemBadge">
        <ReviewBadge status={article.review_status} />
      </div>
    </article>
  );
});
