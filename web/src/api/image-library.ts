import { api } from "./core";
import type { StockCategory, StockImage } from "../types";

export function listCategories(): Promise<StockCategory[]> {
  return api<StockCategory[]>("/api/image-library/categories");
}

export function createCategory(payload: {
  name: string;
  bucket_name: string;
  description?: string | null;
}): Promise<StockCategory> {
  return api<StockCategory>("/api/image-library/categories", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listImages(params?: { category_id?: number; tag?: string }): Promise<StockImage[]> {
  const q = new URLSearchParams();
  if (params?.category_id != null) q.set("category_id", String(params.category_id));
  if (params?.tag) q.set("tag", params.tag);
  const qs = q.toString();
  return api<StockImage[]>(qs ? `/api/image-library/images?${qs}` : "/api/image-library/images");
}

export async function uploadImage(payload: {
  category_id: number;
  tags?: string;
  description?: string;
  file: File;
}): Promise<StockImage> {
  const form = new FormData();
  form.append("file", payload.file);
  const q = new URLSearchParams();
  q.set("category_id", String(payload.category_id));
  if (payload.tags) q.set("tags", payload.tags);
  if (payload.description) q.set("description", payload.description);
  return api<StockImage>(`/api/image-library/images?${q.toString()}`, {
    method: "POST",
    body: form,
  });
}

export function deleteImage(imageId: number): Promise<void> {
  return api<void>(`/api/image-library/images/${imageId}`, { method: "DELETE" });
}
