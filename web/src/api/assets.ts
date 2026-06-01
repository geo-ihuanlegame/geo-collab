import type { Asset } from "../types";
import { uploadLargeFile } from "./chunked-upload";

const CHUNKED_UPLOAD_THRESHOLD = 3 * 1024 * 1024; // 3MB

export async function uploadAsset(file: Blob, onProgress?: (percent: number) => void): Promise<Asset> {
  // 大文件（>3MB）使用分块上传
  if (file.size > CHUNKED_UPLOAD_THRESHOLD && file instanceof File) {
    try {
      const result = await uploadLargeFile(file, (progress) => {
        if (onProgress) {
          onProgress(progress.percent);
        }
      });
      return result as Asset;
    } catch (err) {
      if (err instanceof Error && err.message.includes("401")) {
        window.dispatchEvent(new CustomEvent("auth:unauthorized"));
      }
      throw err;
    }
  }

  // 小文件使用传统 API
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", file);
    const xhr = new XMLHttpRequest();

    if (onProgress) {
      xhr.upload.addEventListener("progress", (e) => {
        if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
      });
    }

    xhr.addEventListener("load", () => {
      if (xhr.status === 401) {
        window.dispatchEvent(new CustomEvent("auth:unauthorized"));
        reject(new Error("登录已过期，请重新登录"));
        return;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText) as Asset);
        } catch {
          reject(new Error("解析响应失败"));
        }
        return;
      }
      try {
        const payload = JSON.parse(xhr.responseText) as { detail?: string };
        if (xhr.status === 403 && payload.detail === "Password change required") {
          window.dispatchEvent(new CustomEvent("auth:password-change-required"));
        }
        reject(new Error(payload.detail || `${xhr.status} ${xhr.statusText}`));
      } catch {
        reject(new Error(`${xhr.status} ${xhr.statusText}`));
      }
    });

    xhr.addEventListener("error", () => reject(new Error("网络错误")));
    xhr.addEventListener("abort", () => reject(new Error("上传已取消")));
    xhr.open("POST", "/api/assets");
    xhr.send(form);
  });
}
