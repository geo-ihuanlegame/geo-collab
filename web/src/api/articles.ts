import { api } from "./core";
import type {
  Article,
  ArticleCreatePayload,
  ArticleGroup,
  ArticleGroupUpdateItemsPayload,
  ArticleSummary,
  ArticleUpdatePayload,
} from "../types";

export function listArticles(params?: URLSearchParams): Promise<ArticleSummary[]> {
  const query = params?.toString();
  return api<ArticleSummary[]>(query ? `/api/articles?${query}` : "/api/articles");
}

export function getArticle(articleId: number): Promise<Article> {
  return api<Article>(`/api/articles/${articleId}`);
}

export function createArticle(payload: ArticleCreatePayload): Promise<Article> {
  return api<Article>("/api/articles", { method: "POST", body: JSON.stringify(payload) });
}

export function updateArticle(articleId: number, payload: ArticleUpdatePayload): Promise<Article> {
  return api<Article>(`/api/articles/${articleId}`, { method: "PUT", body: JSON.stringify(payload) });
}

export function updateArticleCover(
  articleId: number,
  payload: { cover_asset_id: string; version: number | null },
): Promise<Article> {
  return api<Article>(`/api/articles/${articleId}/cover`, { method: "POST", body: JSON.stringify(payload) });
}

export function deleteArticle(articleId: number): Promise<void> {
  return api<void>(`/api/articles/${articleId}`, { method: "DELETE" });
}

export function listArticleGroups(): Promise<ArticleGroup[]> {
  return api<ArticleGroup[]>("/api/article-groups");
}

export function createArticleGroup(payload: { name: string }): Promise<ArticleGroup> {
  return api<ArticleGroup>("/api/article-groups", { method: "POST", body: JSON.stringify(payload) });
}

export function updateArticleGroup(
  groupId: number,
  payload: { name: string; version?: number },
): Promise<ArticleGroup> {
  return api<ArticleGroup>(`/api/article-groups/${groupId}`, { method: "PUT", body: JSON.stringify(payload) });
}

export function updateArticleGroupItems(
  groupId: number,
  payload: ArticleGroupUpdateItemsPayload & { version?: number },
): Promise<ArticleGroup> {
  return api<ArticleGroup>(`/api/article-groups/${groupId}/items`, { method: "PUT", body: JSON.stringify(payload) });
}

export function deleteArticleGroup(groupId: number): Promise<void> {
  return api<void>(`/api/article-groups/${groupId}`, { method: "DELETE" });
}
