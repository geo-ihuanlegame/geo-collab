# 图片传输速度优化方案

## 目标

提升图片传输速度，核心不是只改上传接口，而是减少图片在站内预览、编辑器、列表页、发布前处理中的传输字节和重复处理。

## 核心方案

1. 保留原图作为 master，不直接替换原文件。
2. 上传成功后生成派生图：
   - preview：约 480px WebP，用于封面预览、列表缩略图、编辑器轻量预览。
   - medium：约 1600px WebP，用于正文编辑器和普通站内查看。
   - original：原图，仅用于下载、发布兜底、需要原始质量的场景。
3. 服务接口支持 variant：
   - `/api/assets/{id}?variant=preview`
   - `/api/assets/{id}?variant=medium`
   - `/api/assets/{id}?variant=original`
   - 旧 `/api/assets/{id}` 保持返回原图，保证兼容。
4. 前端默认不再直接请求原图：
   - 文章列表、封面预览用 preview。
   - 编辑器正文图片用 medium 或 preview。
   - 多图、日志截图加 `loading="lazy"`。
5. 分片上传完成阶段优化：
   - 合并分片时流式写入并计算 SHA256。
   - 完成后只读文件头做 magic/尺寸判断，不再 `read_bytes()` 整文件。
6. 发布侧后续优化：
   - Toutiao 发布压缩图做可复用缓存，避免重复发布时重复转码。
   - 发布仍可按平台要求使用 original 或 publish derivative。

## 优先级

P0：
- 去掉 `complete_chunked_upload()` 的整文件读取。
- 新增 preview WebP 派生图。
- 前端封面预览、列表页改用 preview。

P1：
- 新增 medium WebP 派生图。
- 编辑器正文图片改用 medium。
- 图片标签补 `loading="lazy"`。

P2：
- 独立 `modules/assets`。
- 增加 publish derivative 缓存。
- 增加清理派生文件的 orphan cleanup。

## 验收标准

- 5MB 原图在列表/编辑器中实际下载降到约 100KB-500KB。
- 分片上传完成接口不再出现整文件内存峰值。
- 原图 URL 兼容旧逻辑。
- 派生图生成失败时前端可回退原图。
- 重复发布同一图片不重复生成发布压缩图。