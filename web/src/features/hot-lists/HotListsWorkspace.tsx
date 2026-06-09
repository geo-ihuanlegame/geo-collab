import { useEffect, useState } from "react";
import {
  listHotSources,
  getHotList,
  type HotListSource,
  type HotListResponse,
} from "../../api/hot-lists";

export function HotListsWorkspace() {
  const [sources, setSources] = useState<HotListSource[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [data, setData] = useState<HotListResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listHotSources()
      .then((list) => {
        setSources(list);
        if (list.length > 0) setCurrent(list[0].name);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "加载榜单列表失败"));
  }, []);

  function load(source: string, noCache = false) {
    setLoading(true);
    setError(null);
    getHotList(source, { noCache })
      .then((res) => setData(res))
      .catch((e) => {
        setData(null);
        setError(e instanceof Error ? e.message : "加载热榜失败");
      })
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    if (current) load(current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current]);

  return (
    <div style={{ display: "flex", gap: 16, height: "100%", padding: 16 }}>
      <aside style={{ width: 180, overflowY: "auto", borderRight: "1px solid #e5e7eb" }}>
        {sources.map((s) => (
          <button
            key={s.name}
            type="button"
            onClick={() => setCurrent(s.name)}
            style={{
              display: "block",
              width: "100%",
              textAlign: "left",
              padding: "6px 10px",
              border: "none",
              background: current === s.name ? "#eef2ff" : "transparent",
              cursor: "pointer",
              fontWeight: current === s.name ? 600 : 400,
            }}
          >
            {s.name}
          </button>
        ))}
      </aside>
      <section style={{ flex: 1, overflowY: "auto" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>
            {data ? `${data.title} · ${data.type}` : current ?? "热榜"}
          </h2>
          {data?.updateTime && (
            <span style={{ color: "#6b7280", fontSize: 12 }}>更新于 {String(data.updateTime)}</span>
          )}
          {current && (
            <button type="button" onClick={() => load(current, true)} disabled={loading}>
              {loading ? "刷新中…" : "刷新"}
            </button>
          )}
        </div>
        {error && (
          <p role="alert" style={{ color: "#dc2626" }}>
            {error}
          </p>
        )}
        {loading && !data && <p>加载中…</p>}
        {data && data.data.length === 0 && !loading && <p>暂无数据</p>}
        <ol style={{ paddingLeft: 0, listStyle: "none", margin: 0 }}>
          {data?.data.map((item, idx) => (
            <li
              key={String(item.id)}
              style={{ padding: "8px 0", borderBottom: "1px solid #f3f4f6" }}
            >
              <a
                href={item.url}
                target="_blank"
                rel="noopener noreferrer"
                style={{
                  display: "flex",
                  gap: 10,
                  alignItems: "baseline",
                  textDecoration: "none",
                  color: "inherit",
                }}
              >
                <span style={{ color: "#9ca3af", width: 24, textAlign: "right" }}>{idx + 1}</span>
                <span style={{ flex: 1 }}>{item.title}</span>
                {typeof item.hot === "number" && (
                  <span style={{ color: "#f97316", fontSize: 12 }}>{item.hot.toLocaleString()}</span>
                )}
              </a>
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}
