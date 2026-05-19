# 分块上传实现指南

## 概述

实现了**3MB分块 + 4并发**的大文件上传方案，预期将10MB文件上传时间从20秒降至2-3秒。

## 核心改动

### 后端

1. **新增模块** `server/app/modules/articles/chunked_upload.py`
   - `ChunkedUploadManager` — 管理上传会话的生命周期
   - 临时文件存储在 `{GEO_DATA_DIR}/.uploads/{upload_id}/`
   - 支持分块保存、状态查询、合并验证

2. **新增API路由** `server/app/api/routes/chunked_assets.py`
   ```
   POST /api/chunked-assets/upload-start
   POST /api/chunked-assets/upload-chunk/{upload_id}
   POST /api/chunked-assets/upload-status/{upload_id}
   POST /api/chunked-assets/upload-complete/{upload_id}
   ```

3. **改动** `server/app/main.py`
   - 注册 `chunked_assets_router`

4. **优化** `server/app/modules/articles/asset_Store.py`
   - chunk size 从 256KB 改为 8MB（用于传统单文件上传）

### 前端

1. **新增模块** `web/src/api/chunked-upload.ts`
   - `uploadLargeFile()` — 分块上传函数
   - `computeFileHash()` — 前端计算SHA256哈希
   - 自动并发控制（最多4个并发）

2. **改动** `web/src/api/assets.ts`
   - `uploadAsset()` 自动根据文件大小选择上传方式
   - 小文件（<3MB）：传统API
   - 大文件（≥3MB）：分块上传

## 工作流程

### 后端流程

```
1. /upload-start
   └─ 初始化会话，计算分块数，返回 upload_id

2. /upload-chunk/{upload_id} (并发)
   └─ 保存分块到临时目录
   └─ 验证分块大小
   └─ 返回 ok

3. /upload-complete/{upload_id}
   └─ 验证所有分块已上传
   └─ 合并分块到单个文件
   └─ 验证 SHA256 哈希
   └─ 创建 Asset 记录
   └─ 清理临时文件
   └─ 返回 Asset 信息
```

### 前端流程

```javascript
// 自动使用分块上传（如果文件>3MB）
const asset = await uploadAsset(file, (percent) => {
  console.log(`进度: ${percent}%`)
})
```

## 性能预期

| 文件大小 | 传统方案 | 分块方案 | 改进 |
|---------|---------|---------|------|
| 10MB | 20s | 2-3s | 7-10x |
| 20MB | 40s | 4-6s | 7-10x |

**假设条件**：200M带宽，4并发

实际速度依赖于：
- 网络稳定性
- 磁盘写入速度
- 服务器负载

## 测试

```bash
# 测试脚本（需要后端运行）
python test_chunked_upload.py

# 输出示例：
# [SUCCESS] Upload complete!
#   Time: 2.45s
#   Speed: 4.08 MB/s
```

## 注意事项

1. **临时文件清理** — 即使上传失败，临时文件会在调用 `/upload-complete` 时被清理
2. **哈希验证** — 前端计算SHA256，后端验证完整性，防止传输损坏
3. **并发限制** — 硬编码4并发，避免过多TCP连接占用
4. **超时配置** — nginx `client_body_timeout 300s` 足够大（5分钟）

## 可选增强

### 1. 断点续传
在 `/upload-status/{upload_id}` 基础上，前端记录已上传分块，网络中断后继续：
```javascript
const status = await fetch(`/api/chunked-assets/upload-status/${uploadId}`)
const { uploaded_chunks } = await status.json()
// 仅重新上传未成功的分块
```

### 2. 并发数动态调整
根据网络速度自动调整并发数（需前端埋点）

### 3. 后端并发限制
如果担心多用户同时上传导致服务器压力，可在 `ChunkedUploadManager` 中限制全局并发会话数

## 文件位置

```
server/app/modules/articles/
├── chunked_upload.py          [新建]
└── asset_Store.py             [改: chunk_size 8MB]

server/app/api/routes/
├── chunked_assets.py          [新建]
└── assets.ts                  [改: 自动选择上传方式]

web/src/api/
├── chunked-upload.ts          [新建]
└── assets.ts                  [改: 调用分块API]

server/app/
└── main.py                    [改: 注册路由]
```
