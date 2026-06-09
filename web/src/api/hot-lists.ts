import { api } from "./core";

export type HotListSource = { name: string; path: string };

export type HotListItem = {
  id: number | string;
  title: string;
  cover?: string;
  author?: string;
  desc?: string;
  hot?: number;
  timestamp?: number;
  url: string;
  mobileUrl?: string;
};

export type HotListResponse = {
  code: number;
  name: string;
  title: string;
  type: string;
  link?: string;
  total: number;
  updateTime?: string | number;
  fromCache?: boolean;
  data: HotListItem[];
};

type AllResponse = { code: number; count: number; routes: HotListSource[] };

export async function listHotSources(): Promise<HotListSource[]> {
  const res = await api<AllResponse>("/api/hot-lists");
  return res.routes.filter((r) => Boolean(r.path));
}

export function getHotList(
  source: string,
  opts?: { limit?: number; noCache?: boolean },
): Promise<HotListResponse> {
  const params = new URLSearchParams();
  if (opts?.limit) params.set("limit", String(opts.limit));
  if (opts?.noCache) params.set("cache", "false");
  const query = params.toString();
  return api<HotListResponse>(`/api/hot-lists/${source}${query ? `?${query}` : ""}`);
}
