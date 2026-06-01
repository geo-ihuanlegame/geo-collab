export function Pagination({
  page,
  totalPages,
  loading,
  onPrev,
  onNext,
}: {
  page: number;
  totalPages: number;
  loading: boolean;
  onPrev: () => void;
  onNext: () => void;
}) {
  return (
    <div className="pagerRow">
      <button type="button" disabled={page === 0 || loading} onClick={onPrev}>
        上一页
      </button>
      <span>第 {page + 1} / {totalPages} 页</span>
      <button
        type="button"
        disabled={page >= totalPages - 1 || loading}
        onClick={onNext}
      >
        下一页
      </button>
    </div>
  );
}
