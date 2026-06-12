import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { AlertCircle, Flame, RefreshCw, RotateCw, Search } from "lucide-react";
import {
  getHotList,
  listHotSources,
  type HotListResponse,
  type HotListSource,
} from "../../api/hot-lists";

// ── Platform metadata ───────────────────────────────────────────────────────
// Maps a DailyHotApi source key → brand color, logo initial, and category.
// Unknown sources fall back to a hashed color + the first char of their title.
type Category = "news" | "community" | "tech" | "video" | "finance" | "game";
type Meta = { c: string; i: string; cat?: Category };

const PLATFORM_META: Record<string, Meta> = {
  weibo: { c: "#E6162D", i: "微", cat: "community" },
  zhihu: { c: "#0066FF", i: "知", cat: "community" },
  "zhihu-daily": { c: "#0066FF", i: "知", cat: "news" },
  bilibili: { c: "#FB7299", i: "B", cat: "video" },
  douyin: { c: "#161823", i: "抖", cat: "video" },
  kuaishou: { c: "#FF4906", i: "快", cat: "video" },
  acfun: { c: "#FD4C5C", i: "A", cat: "video" },
  baidu: { c: "#2932E1", i: "百", cat: "news" },
  toutiao: { c: "#FF5000", i: "头", cat: "news" },
  "qq-news": { c: "#11A8FF", i: "腾", cat: "news" },
  "netease-news": { c: "#E60012", i: "网", cat: "news" },
  sina: { c: "#E6162D", i: "新", cat: "news" },
  "sina-news": { c: "#E6162D", i: "新", cat: "news" },
  thepaper: { c: "#D40000", i: "澎", cat: "news" },
  "36kr": { c: "#2B6CB0", i: "氪", cat: "tech" },
  ithome: { c: "#C50000", i: "I", cat: "tech" },
  "ithome-xijiayi": { c: "#C50000", i: "I", cat: "tech" },
  juejin: { c: "#1E80FF", i: "掘", cat: "tech" },
  csdn: { c: "#FC5531", i: "C", cat: "tech" },
  github: { c: "#24292E", i: "G", cat: "tech" },
  hackernews: { c: "#FF6600", i: "Y", cat: "tech" },
  hellogithub: { c: "#353535", i: "H", cat: "tech" },
  sspai: { c: "#D71920", i: "少", cat: "tech" },
  producthunt: { c: "#DA552F", i: "P", cat: "tech" },
  "51cto": { c: "#E60012", i: "51", cat: "tech" },
  guokr: { c: "#41A85F", i: "果", cat: "tech" },
  huxiu: { c: "#ED2226", i: "虎", cat: "tech" },
  ifanr: { c: "#00A5A8", i: "爱", cat: "tech" },
  geekpark: { c: "#1E1E1E", i: "极", cat: "tech" },
  dgtle: { c: "#28B4A0", i: "数", cat: "tech" },
  "52pojie": { c: "#2B2B2B", i: "吾", cat: "tech" },
  v2ex: { c: "#2B2B2B", i: "V", cat: "community" },
  nodeseek: { c: "#2B6CB0", i: "N", cat: "community" },
  linuxdo: { c: "#FFB003", i: "L", cat: "community" },
  hostloc: { c: "#5C6BC0", i: "H", cat: "community" },
  hupu: { c: "#D81E06", i: "虎", cat: "community" },
  tieba: { c: "#3D78D6", i: "贴", cat: "community" },
  coolapk: { c: "#11AA66", i: "酷", cat: "community" },
  jianshu: { c: "#EA6F5A", i: "简", cat: "community" },
  newsmth: { c: "#1E6FBF", i: "水", cat: "community" },
  "douban-group": { c: "#2D963D", i: "豆", cat: "community" },
  "douban-movie": { c: "#2D963D", i: "豆", cat: "video" },
  weread: { c: "#2DA641", i: "读", cat: "community" },
  smzdm: { c: "#E62828", i: "值", cat: "finance" },
  lol: { c: "#C28F2C", i: "L", cat: "game" },
  genshin: { c: "#4A90D9", i: "原", cat: "game" },
  honkai: { c: "#4A90D9", i: "崩", cat: "game" },
  starrail: { c: "#4A90D9", i: "星", cat: "game" },
  miyoushe: { c: "#4A90D9", i: "米", cat: "game" },
  ngabbs: { c: "#2B2B2B", i: "N", cat: "game" },
  gameres: { c: "#6A4FB3", i: "G", cat: "game" },
  yystv: { c: "#1A1A1A", i: "游", cat: "game" },
  history: { c: "#8A6D3B", i: "史", cat: "news" },
  earthquake: { c: "#C5482E", i: "震", cat: "news" },
  weatheralarm: { c: "#E08A1E", i: "警", cat: "news" },
};

const CATEGORY_ORDER: Category[] = ["news", "community", "tech", "video", "finance", "game"];
const CATEGORY_LABELS: Record<"all" | Category, string> = {
  all: "全部",
  news: "资讯",
  community: "社区",
  tech: "科技",
  video: "影音",
  finance: "财经",
  game: "游戏",
};

const FALLBACK_COLORS = [
  "#C5482E",
  "#B88A3E",
  "#3F8F5C",
  "#5B5FE9",
  "#2B6CB0",
  "#D07B3C",
  "#7A5CC9",
  "#1E80FF",
];

function metaFor(name: string, title?: string): Meta {
  const known = PLATFORM_META[name];
  if (known) return known;
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return { c: FALLBACK_COLORS[h % FALLBACK_COLORS.length], i: (title || name).charAt(0).toUpperCase() };
}

// ── Formatting helpers ──────────────────────────────────────────────────────
function pad(n: number): string {
  return n < 10 ? `0${n}` : `${n}`;
}

function toDate(updateTime?: string | number, fetchedAt?: number): Date | null {
  if (updateTime != null) {
    const d = new Date(updateTime);
    if (!Number.isNaN(d.getTime())) return d;
  }
  if (fetchedAt) return new Date(fetchedAt);
  return null;
}

function formatTime(updateTime?: string | number, fetchedAt?: number): string {
  const d = toDate(updateTime, fetchedAt);
  return d ? `${pad(d.getHours())}:${pad(d.getMinutes())}` : "--:--";
}

function formatShort(updateTime?: string | number, fetchedAt?: number): string {
  const d = toDate(updateTime, fetchedAt);
  return d ? `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}` : "";
}

function formatHot(hot: number | string | null | undefined): string | null {
  if (hot == null) return null;
  if (typeof hot === "number") {
    if (hot >= 1e8) return `${(hot / 1e8).toFixed(1).replace(/\.0$/, "")}亿`;
    if (hot >= 1e4) return `${(hot / 1e4).toFixed(1).replace(/\.0$/, "")}万`;
    return hot.toLocaleString();
  }
  const s = String(hot).trim();
  return s || null;
}

// Run an async worker over items with bounded concurrency.
async function eachLimit<T>(items: T[], limit: number, fn: (item: T) => Promise<void>): Promise<void> {
  let cursor = 0;
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (cursor < items.length) {
      const item = items[cursor];
      cursor += 1;
      await fn(item);
    }
  });
  await Promise.all(workers);
}

type BoardState = {
  status: "loading" | "ok" | "error";
  data?: HotListResponse;
  error?: string;
  fetchedAt?: number;
};

// ── Workspace ───────────────────────────────────────────────────────────────
export function HotListsWorkspace() {
  const [sources, setSources] = useState<HotListSource[]>([]);
  const [boards, setBoards] = useState<Record<string, BoardState>>({});
  const [sourcesError, setSourcesError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState<Record<string, boolean>>({});
  const [refreshingAll, setRefreshingAll] = useState(false);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<"all" | Category>("all");

  const loadBoard = useCallback(async (name: string, noCache = false) => {
    setBoards((prev) => ({
      ...prev,
      [name]: prev[name]?.data ? prev[name] : { status: "loading" },
    }));
    try {
      const data = await getHotList(name, { limit: 30, noCache });
      setBoards((prev) => ({ ...prev, [name]: { status: "ok", data, fetchedAt: Date.now() } }));
    } catch (e) {
      setBoards((prev) => ({
        ...prev,
        [name]: {
          status: "error",
          error: e instanceof Error ? e.message : "加载失败",
          data: prev[name]?.data,
          fetchedAt: prev[name]?.fetchedAt,
        },
      }));
    }
  }, []);

  const loadSources = useCallback(() => {
    setSourcesError(null);
    listHotSources()
      .then((list) => {
        setSources(list);
        setBoards(Object.fromEntries(list.map((s) => [s.name, { status: "loading" as const }])));
        void eachLimit(list, 6, (s) => loadBoard(s.name));
      })
      .catch((e) => setSourcesError(e instanceof Error ? e.message : "加载榜单列表失败"));
  }, [loadBoard]);

  useEffect(() => {
    loadSources();
  }, [loadSources]);

  const refreshOne = useCallback(
    async (name: string) => {
      setRefreshing((p) => ({ ...p, [name]: true }));
      await loadBoard(name, true);
      setRefreshing((p) => ({ ...p, [name]: false }));
    },
    [loadBoard],
  );

  const refreshAll = useCallback(async () => {
    if (sources.length === 0) return;
    setRefreshingAll(true);
    await eachLimit(sources, 6, (s) => loadBoard(s.name, true));
    setRefreshingAll(false);
  }, [sources, loadBoard]);

  const chips = useMemo<("all" | Category)[]>(() => {
    const present = new Set<Category>();
    for (const s of sources) {
      const cat = metaFor(s.name).cat;
      if (cat) present.add(cat);
    }
    return ["all", ...CATEGORY_ORDER.filter((c) => present.has(c))];
  }, [sources]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return sources.filter((s) => {
      if (category !== "all" && metaFor(s.name).cat !== category) return false;
      if (!q) return true;
      const board = boards[s.name];
      const title = (board?.data?.title || s.name).toLowerCase();
      if (title.includes(q)) return true;
      return (board?.data?.data || []).some((it) => (it.title || "").toLowerCase().includes(q));
    });
  }, [sources, boards, query, category]);

  return (
    <div className="hotDash">
      <header className="hotHeader">
        <div className="hotHeaderTop">
          <div>
            <div className="hotEyebrow">实时热点</div>
            <div className="hotTitle">热榜聚合</div>
            <p className="hotSub">实时聚合 {sources.length || "多"} 个平台热门榜单 · 点击条目跳转原文</p>
          </div>
          <div className="hotControls">
            <label className="hotSearch">
              <Search size={15} aria-hidden />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="搜索榜单或关键词"
                aria-label="搜索榜单或关键词"
              />
            </label>
            <button
              type="button"
              className="secondaryButton"
              onClick={refreshAll}
              disabled={refreshingAll || sources.length === 0}
            >
              <RefreshCw size={15} className={refreshingAll ? "hotSpin" : ""} aria-hidden />
              全部刷新
            </button>
          </div>
        </div>
        {chips.length > 1 && (
          <div className="hotChips">
            {chips.map((c) => (
              <button
                key={c}
                type="button"
                className={`hotChip${category === c ? " active" : ""}`}
                onClick={() => setCategory(c)}
              >
                {CATEGORY_LABELS[c]}
              </button>
            ))}
          </div>
        )}
      </header>

      <div className="hotScroll">
        {sourcesError ? (
          <div className="hotGlobalState">
            <AlertCircle size={22} aria-hidden />
            <span>{sourcesError}</span>
            <button type="button" className="hotRetry" onClick={loadSources}>
              重试
            </button>
          </div>
        ) : sources.length === 0 ? (
          <div className="hotGlobalState">加载榜单中…</div>
        ) : visible.length === 0 ? (
          <div className="hotGlobalState">没有匹配的榜单</div>
        ) : (
          <div className="hotGrid">
            {visible.map((s) => (
              <HotCard
                key={s.name}
                source={s}
                board={boards[s.name]}
                refreshing={!!refreshing[s.name]}
                query={query}
                onRefresh={() => refreshOne(s.name)}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Card ────────────────────────────────────────────────────────────────────
function HotCard({
  source,
  board,
  refreshing,
  query,
  onRefresh,
}: {
  source: HotListSource;
  board?: BoardState;
  refreshing: boolean;
  query: string;
  onRefresh: () => void;
}) {
  const title = board?.data?.title || source.name;
  const type = board?.data?.type;
  const meta = metaFor(source.name, board?.data?.title);
  const q = query.trim().toLowerCase();

  const ranked = (board?.data?.data || []).map((it, i) => ({ it, rank: i + 1 }));
  const items = q ? ranked.filter((r) => (r.it.title || "").toLowerCase().includes(q)) : ranked;

  let body: ReactNode;
  if (board?.status === "error" && !board.data) {
    body = (
      <div className="hotState">
        <AlertCircle size={20} aria-hidden />
        <span>{board.error || "加载失败"}</span>
        <button type="button" className="hotRetry" onClick={onRefresh}>
          重试
        </button>
      </div>
    );
  } else if (!board || (board.status === "loading" && !board.data)) {
    body = (
      <div className="hotList">
        {Array.from({ length: 8 }).map((_, i) => (
          <div className="hotSkelRow" key={i} />
        ))}
      </div>
    );
  } else if (items.length === 0) {
    body = <div className="hotState">{q ? "无匹配条目" : "暂无数据"}</div>;
  } else {
    body = (
      <div className="hotList">
        {items.map(({ it, rank }) => {
          const hot = formatHot(it.hot);
          return (
            <a
              key={`${it.id ?? rank}-${rank}`}
              className={`hotItem${rank === 1 ? " top" : ""}`}
              href={it.url}
              target="_blank"
              rel="noopener noreferrer"
              title={it.title}
            >
              <span className={`hotRank${rank <= 3 ? ` r${rank}` : ""}`}>{rank}</span>
              <span className="hotItemTitle">{it.title}</span>
              {hot != null && (
                <span className="hotItemHot">
                  <Flame size={11} aria-hidden />
                  {hot}
                </span>
              )}
            </a>
          );
        })}
      </div>
    );
  }

  const footText =
    board?.status === "ok"
      ? `更新于 ${formatTime(board.data?.updateTime, board.fetchedAt)}`
      : board?.status === "error"
        ? "加载失败"
        : "加载中…";

  return (
    <section className="hotCard">
      <div className="hotCardHead">
        <div className="hotLogo" style={{ background: meta.c }}>
          {meta.i}
        </div>
        <div className="hotCardTitle">
          <div className="hotName" title={title}>
            {title}
          </div>
          {type && <div className="hotType">{type}</div>}
        </div>
        <div className="hotCardMeta">
          {board?.status === "ok" && (
            <span className="hotTime">{formatShort(board.data?.updateTime, board.fetchedAt)}</span>
          )}
          <button
            type="button"
            className={`hotRefresh${refreshing ? " spinning" : ""}`}
            onClick={onRefresh}
            title="刷新"
            aria-label={`刷新 ${title}`}
          >
            <RotateCw size={14} aria-hidden />
          </button>
        </div>
      </div>
      {body}
      <div className="hotFoot">
        <span className="hotFootTime">{footText}</span>
      </div>
    </section>
  );
}
