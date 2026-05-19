/**
 * 分块上传支持 — 大文件快速上传
 * 支持 3MB 分块，4 并发
 */

const CHUNK_SIZE = 3 * 1024 * 1024; // 3MB
const MAX_CONCURRENT = 4;

export interface UploadProgress {
  uploadedChunks: number[];
  totalChunks: number;
  percent: number;
}

export interface ChunkedUploadResult {
  id: string;
  filename: string;
  size: number;
  width?: number;
  height?: number;
  url: string;
}

/**
 * 计算文件的 SHA256 哈希
 */
export async function computeFileHash(file: File): Promise<string> {
  const buffer = await file.arrayBuffer();
  const hashBuffer = await crypto.subtle.digest("SHA-256", buffer);
  const hashArray = Array.from(new Uint8Array(hashBuffer));
  return hashArray.map((b) => b.toString(16).padStart(2, "0")).join("");
}

/**
 * TCP 预热 — 在上传前建立连接，触发 TCP 慢启动
 * 通过发送一个空的初始化请求来"热身"连接
 */
async function warmupConnection(): Promise<void> {
  try {
    // 发送一个小的 HEAD 请求来建立 TCP 连接和完成 TLS 握手
    await fetch("/api/chunked-assets/upload-start", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        total_size: 0,
        file_hash: "",
      }),
    }).catch(() => {
      // 忽略错误，这只是为了建立连接
    });
  } catch {
    // 预热失败不影响实际上传
  }
}

/**
 * 上传大文件（自动分块）
 */
export async function uploadLargeFile(
  file: File,
  onProgress?: (progress: UploadProgress) => void
): Promise<ChunkedUploadResult> {
  const totalSize = file.size;

  // 对于小文件，直接上传（兼容旧 API）
  if (totalSize < CHUNK_SIZE) {
    return uploadSmallFile(file);
  }

  // TCP 预热：建立连接，触发慢启动阶段
  // 这样真正的数据传输时，拥塞窗口已经打开，初速更快
  await warmupConnection();

  // 计算文件哈希
  const fileHash = await computeFileHash(file);
  const chunkCount = Math.ceil(totalSize / CHUNK_SIZE);

  // 初始化上传
  const initResponse = await fetch("/api/chunked-assets/upload-start", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      total_size: totalSize,
      file_hash: fileHash,
    }),
  });

  if (!initResponse.ok) {
    throw new Error(`Upload initialization failed: ${initResponse.statusText}`);
  }

  const { upload_id } = (await initResponse.json()) as { upload_id: string };

  // 准备分块
  const chunks: Blob[] = [];
  for (let i = 0; i < chunkCount; i++) {
    const start = i * CHUNK_SIZE;
    const end = Math.min(start + CHUNK_SIZE, totalSize);
    chunks.push(file.slice(start, end));
  }

  // 并发上传（最多 MAX_CONCURRENT 个）
  const uploadedChunks = new Set<number>();
  let activeUploads = 0;
  let uploadError: Error | null = null;

  const uploadChunk = async (index: number) => {
    if (uploadError) return;

    try {
      const formData = new FormData();
      formData.append("file", chunks[index]);

      const response = await fetch(
        `/api/chunked-assets/upload-chunk/${upload_id}?chunk_index=${index}`,
        {
          method: "POST",
          body: formData,
        }
      );

      if (!response.ok) {
        throw new Error(`Chunk ${index} upload failed: ${response.statusText}`);
      }

      uploadedChunks.add(index);

      // 触发进度回调
      if (onProgress) {
        onProgress({
          uploadedChunks: Array.from(uploadedChunks),
          totalChunks: chunkCount,
          percent: Math.round((uploadedChunks.size / chunkCount) * 100),
        });
      }
    } catch (err) {
      uploadError = err instanceof Error ? err : new Error(String(err));
      throw uploadError;
    }
  };

  // 并发控制
  const queue: Promise<void>[] = [];
  for (let i = 0; i < chunkCount; i++) {
    const promise = (async () => {
      while (activeUploads >= MAX_CONCURRENT) {
        await new Promise((resolve) => setTimeout(resolve, 100));
      }
      activeUploads++;
      try {
        await uploadChunk(i);
      } finally {
        activeUploads--;
      }
    })();

    queue.push(promise);
  }

  // 等待所有分块上传完成
  await Promise.all(queue);

  if (uploadError) {
    throw uploadError;
  }

  // 完成上传，合并分块
  const completeResponse = await fetch(
    `/api/chunked-assets/upload-complete/${upload_id}`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        filename: file.name,
        content_type: file.type || "application/octet-stream",
      }),
    }
  );

  if (!completeResponse.ok) {
    throw new Error(`Upload completion failed: ${completeResponse.statusText}`);
  }

  const result = (await completeResponse.json()) as ChunkedUploadResult;
  return result;
}

/**
 * 上传小文件（直接使用传统 API）
 */
async function uploadSmallFile(file: File): Promise<ChunkedUploadResult> {
  const form = new FormData();
  form.append("file", file);

  const response = await fetch("/api/assets", {
    method: "POST",
    body: form,
  });

  if (!response.ok) {
    throw new Error(`Upload failed: ${response.statusText}`);
  }

  const result = (await response.json()) as ChunkedUploadResult;
  return result;
}
