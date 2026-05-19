# 图片传输速度优化方案

## 现状传输链路

```
上传 → 存磁盘 (原图, full-res, 原始格式)
       → /api/assets/{id}
         → uvicorn FileResponse  (dev)
         → NGINX X-Accel-Redirect (prod, 已就绪)
       → 前端
         → 列表封面: 原图
         → 正文图片: 原图
         → 封面详情: 原图
```

瓶颈不在后端 IO，在 **图片文件本身太大，且没有按场景分尺寸传输**。

---

## P0 — 上传时生成 WebP（~25 行）

**位置**: `server/app/modules/articles/asset_Store.py`

在 `_create_asset` / `_create_asset_from_path` 写完原图后追加：

```python
from PIL import Image
img = Image.open(path)
webp_path = path.with_suffix(".webp")
img.save(webp_path, "WEBP", quality=80, optimize=True)
asset.webp_size = webp_path.stat().st_size
asset.webp_storage_key = storage_key.with_suffix(".webp").as_posix()
```

**Model 追加字段**: `Asset.webp_storage_key: str | None`, `Asset.webp_size: int | None`

**服务时协商**: `server/app/api/routes/assets.py#read_asset_file`

```python
accept = request.headers.get("accept", "")
if "image/webp" in accept and asset.webp_storage_key:
    path = resolve_webp_path(asset)
```

**收益**: PNG → WebP 缩小 **70-80%**，JPEG → WebP 缩小 **25-35%**。

---

## P1 — 上传时生成预览缩略图（~20 行）

**位置**: 同上，`_create_asset` / `_create_asset_from_path`

```python
thumb = img.copy()
thumb.thumbnail((400, 400))
thumb_path = path.with_stem(f"{path.stem}_thumb")
thumb.save(thumb_path, "WEBP" if asset.webp_storage_key else None, quality=75)
```

**新增路由**: `GET /api/assets/{id}/thumbnail` → 返回缩略图

**前端使用**:
- 文章列表封面 → `assetSrc(id) + "/thumbnail"`
- 编辑器内 inline 预览 → 缩略图（点击/选中时切原图）

**收益**: 列表页封面从 ~500KB 降到 ~15-30KB。

---

## P2 — 前端图片懒加载（~5 行）

**位置**: `web/src/features/content/ContentWorkspace.tsx`、`web/src/features/tasks/TasksWorkspace.tsx`

```tsx
<img loading="lazy" src={...} />
```

| 位置 | 标签 |
|------|------|
| 正文 body 中 driver/publish 渲染的图片 | `loading="lazy"` |
| 任务日志截图 | `loading="lazy"` |
| 列表封面 | `loading="lazy"` |

封面是首屏关键元素，**不加 lazy**，改用 P3 的 preload。

**收益**: 首屏不等待不可见图下载，LCP 降低 30-50%。

---

## P3 — 封面 `<link rel="preload">`（~3 行）

**位置**: 文章详情/编辑页 `<head>`

```tsx
<link rel="preload" href={assetSrc(article.cover_asset_id)} as="image" />
```

确保封面是浏览器最高优先级下载的资源。

---

## 不做的事项

| 事项 | 原因 |
|------|------|
| 拆 `modules/assets` | 纯架构洁癖，对传输速度零影响 |
| 分片上传流式 IO 改造 | 影响后端内存峰值，不影响用户端传输速度 |
| 启用分片上传 session 持久化 | 多 worker 场景才需要，当前单进程无影响 |
| NGINX 配置调整 | X-Accel-Redirect 已在生产就绪 |
| AVIF 格式 | WebP 浏览器覆盖率 97%+，AVIF 收益边际且编码慢 |
| 图片 CDN | 内部 CMS 无此必要 |

---

## 总改动量

| # | 文件 | 行数 |
|---|------|------|
| P0 | `asset_Store.py` + `assets.py` + `Asset` model | ~25 |
| P1 | `asset_Store.py` + `assets.py` + 前端列表组件 | ~20 |
| P2 | `ContentWorkspace.tsx` + `TasksWorkspace.tsx` + 列表组件 | ~5 |
| P3 | 文章编辑页 HTML head | ~3 |
| **合计** | | **~53** |
