import { api } from "./core";
import type { Asset } from "../types";

export function uploadAsset(file: Blob): Promise<Asset> {
  const form = new FormData();
  form.append("file", file);
  return api<Asset>("/api/assets", { method: "POST", body: form });
}
