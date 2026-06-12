# 编辑器图片保存：文件浏览式弹框 + 正文选图高亮 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让运营在内容管理 tab 的编辑器里更顺手地把正文图片存进图片库——正文选中图片有明显高亮、工具栏用磁盘图标、保存弹框换成"文件浏览式"（两层下钻 + 新建/删除文件夹）。

**Architecture:** 复用现有 image_library API/模型；后端只加两处小改（删分类端点 + bucket 自动派名 + MinIO 删空桶）；前端重写 `ImageSaveDialog` 为自带状态的浏览式弹框，工具栏与高亮是独立小改。整页图片库不动。

**Tech Stack:** 后端 FastAPI + SQLAlchemy + MinIO（pytest/MySQL，MinIO 全程 monkeypatch）；前端 React 19 + TypeScript + Tiptap + Lucide（无单测，门禁 = `typecheck` + `build`）。

**Spec:** `docs/superpowers/specs/2026-06-11-image-save-folder-browser-design.md`

---

## 文件结构

| 文件 | 职责 | 改动 |
|------|------|------|
| `server/app/modules/image_library/store.py` | MinIO 封装 | 加 `remove_bucket` |
| `server/app/modules/image_library/router.py` | 图片库路由 | 加 `DELETE /categories/{id}`；`CategoryCreate.bucket_name` 可选 + 自动派名 |
| `server/tests/test_image_library_folder_ops.py` | 新测试 | 删分类 + 自动派名用例 |
| `web/src/api/image-library.ts` | 前端 API | `createCategory` bucket 可选 + 新增 `deleteCategory` |
| `web/src/components/editor/EditorToolbar.tsx` | 编辑器工具栏 | 文字按钮 → `Save` 图标按钮 |
| `web/src/styles.css` | 全局样式 | 选中图片高亮 + 新弹框样式 |
| `web/src/components/editor/ImageSaveDialog.tsx` | 保存弹框 | 整体重写为浏览式 |

> 运行后端测试需 `GEO_TEST_DATABASE_URL`（库名含 `test`），且 conda 环境 `geo_xzpt`。本仓库工具 shell 里 `conda activate` 不生效，pytest 用环境内 python 全路径调用（见各步命令）。

---

## Task 1: 后端 — MinIO 删空桶 + 删分类端点

**Files:**
- Modify: `server/app/modules/image_library/store.py`
- Modify: `server/app/modules/image_library/router.py`
- Create: `server/tests/test_image_library_folder_ops.py`

- [ ] **Step 1: 写失败测试（删分类：成功 / 非空 409 / 不存在 404）**

Create `server/tests/test_image_library_folder_ops.py`:

```python
import pytest

from server.tests.utils import build_test_app


def _patch_minio(monkeypatch):
    """测试环境无 MinIO：建桶/删桶/上传全部打成 no-op。"""
    monkeypatch.setattr(
        "server.app.modules.image_library.router.minio_store.ensure_bucket",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "server.app.modules.image_library.router.minio_store.remove_bucket",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "server.app.modules.image_library.router.minio_store.upload_image",
        lambda *a, **k: None,
    )


def _make_cat(client, name="文件夹A", bucket="folder-a", kind="main"):
    r = client.post(
        "/api/image-library/categories",
        json={"name": name, "bucket_name": bucket, "kind": kind},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _upload(client, category_id):
    r = client.post(
        f"/api/image-library/images?category_id={category_id}",
        files={"file": ("a.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32, "image/png")},
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.mysql
def test_delete_empty_folder_succeeds(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        client = app.client
        cat = _make_cat(client)
        r = client.delete(f"/api/image-library/categories/{cat['id']}")
        assert r.status_code == 204, r.text
        # 已不在列表里
        ids = {x["id"] for x in client.get("/api/image-library/categories").json()}
        assert cat["id"] not in ids
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_delete_nonempty_folder_409(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        client = app.client
        cat = _make_cat(client)
        _upload(client, cat["id"])
        r = client.delete(f"/api/image-library/categories/{cat['id']}")
        assert r.status_code == 409, r.text
        # 仍然存在
        ids = {x["id"] for x in client.get("/api/image-library/categories").json()}
        assert cat["id"] in ids
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_delete_missing_folder_404(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        client = app.client
        r = client.delete("/api/image-library/categories/999999")
        assert r.status_code == 404, r.text
    finally:
        app.cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_image_library_folder_ops.py -q`
Expected: FAIL（`DELETE` 端点不存在 → 405/404 不匹配；或 `remove_bucket` 属性不存在）。

> 命令里用环境内 python 全路径，例如 `D:\miniconda3\envs\geo_xzpt\python.exe -m pytest ...`，并先设好 `GEO_TEST_DATABASE_URL`。下同。

- [ ] **Step 3: 加 `remove_bucket` 到 store.py**

在 `server/app/modules/image_library/store.py` 末尾追加：

```python
def remove_bucket(bucket_name: str) -> None:
    """删除空分桶。MinIO 仅允许删空桶，非空时 client 抛错——与"非空禁止删"语义天然一致。"""
    client = _client()
    client.remove_bucket(bucket_name)
```

- [ ] **Step 4: 加 `DELETE /categories/{id}` 端点**

在 `server/app/modules/image_library/router.py` 的 `update_category` 函数之后（约 L260，「图片路由」分隔注释之前）插入：

```python
@router.delete("/categories/{category_id}", status_code=204)
def delete_category(
    category_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    cat = db.get(StockCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="栏目不存在")

    image_count = (
        db.query(StockImage).filter(StockImage.category_id == category_id).count()
    )
    if image_count > 0:
        raise HTTPException(status_code=409, detail="该文件夹内还有图片，请先清空")

    cat_name = cat.name
    bucket_name = cat.bucket_name
    try:
        minio_store.remove_bucket(bucket_name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"MinIO bucket 删除失败: {exc}") from exc

    db.delete(cat)
    db.commit()
    add_audit_entry(
        db,
        user=current_user,
        action="stock_category.delete",
        target_type="stock_category",
        target_id=category_id,
        payload={"name": cat_name},
        request=request,
    )
```

> `StockImage`、`HTTPException`、`add_audit_entry`、`minio_store` 在该文件顶部均已 import，无需新增。

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest server/tests/test_image_library_folder_ops.py -q`
Expected: 3 passed（`test_delete_empty_folder_succeeds` / `_409` / `_404`）。

- [ ] **Step 6: ruff 自检**

Run: `ruff check server/app/modules/image_library/ server/tests/test_image_library_folder_ops.py`
Expected: 无 error（CI 硬门禁）。

- [ ] **Step 7: 提交**

```bash
git add server/app/modules/image_library/store.py server/app/modules/image_library/router.py server/tests/test_image_library_folder_ops.py
git commit -m "feat(image-library): 删分类端点(非空禁止删)+MinIO 删空桶"
```

---

## Task 2: 后端 — 创建分类 bucket 名可选 + 自动派名

**Files:**
- Modify: `server/app/modules/image_library/router.py`
- Modify: `server/tests/test_image_library_folder_ops.py`

- [ ] **Step 1: 加失败测试（不传 bucket_name 时自动派名）**

在 `server/tests/test_image_library_folder_ops.py` 末尾追加：

```python
@pytest.mark.mysql
def test_create_folder_without_bucket_auto_slugs(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        client = app.client
        # 只给名字，不给 bucket_name
        r = client.post(
            "/api/image-library/categories",
            json={"name": "餐厅养成记", "kind": "main"},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        # 后端自动派生了非空 bucket（拼音 slug），且符合 S3 命名（小写字母数字，3~63）
        assert body["bucket_name"]
        assert body["bucket_name"].isalnum() and body["bucket_name"].islower()
        assert 3 <= len(body["bucket_name"]) <= 63
        assert body["kind"] == "main"
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_create_folder_with_explicit_bucket_still_works(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        client = app.client
        cat = _make_cat(client, name="显式桶", bucket="explicit-bucket", kind="companion")
        assert cat["bucket_name"] == "explicit-bucket"
    finally:
        app.cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_image_library_folder_ops.py::test_create_folder_without_bucket_auto_slugs -q`
Expected: FAIL（当前 `bucket_name` 必填 → 422）。

- [ ] **Step 3: `CategoryCreate.bucket_name` 改可选**

在 `server/app/modules/image_library/router.py` 的 `CategoryCreate` 里，把：

```python
    bucket_name: str = Field(min_length=1, max_length=63)
```

改为：

```python
    bucket_name: str | None = Field(default=None, max_length=63)
```

- [ ] **Step 4: `create_category` 加自动派名分支**

在 `router.py` 顶部 import 区（`from server.app.modules.image_library import store as minio_store` 那行下面）加：

```python
from server.app.modules.image_library import service as image_service
```

把 `create_category` 函数体开头改为（替换原先直接读 `payload.bucket_name` 的 409 校验段）：

```python
def create_category(
    payload: CategoryCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    if payload.bucket_name:
        bucket_name = payload.bucket_name
        existing = (
            db.query(StockCategory).filter(StockCategory.bucket_name == bucket_name).first()
        )
        if existing:
            raise HTTPException(status_code=409, detail="bucket_name 已存在")
    else:
        # 不暴露 bucket：按文件夹名拼音自动派一个唯一桶名
        bucket_name = image_service._unique_bucket_name(
            db, image_service.slugify_bucket(payload.name)
        )
    try:
        minio_store.ensure_bucket(bucket_name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"MinIO bucket 创建失败: {exc}") from exc
    cat = StockCategory(
        name=payload.name,
        bucket_name=bucket_name,
        kind=payload.kind,
        description=payload.description,
        official_url=payload.official_url,
    )
    db.add(cat)
    db.commit()
    db.refresh(cat)
    add_audit_entry(
        db,
        user=current_user,
        action="stock_category.create",
        target_type="stock_category",
        target_id=cat.id,
        payload={"name": cat.name},
        request=request,
    )
    return _to_category_read(cat)
```

> `slugify_bucket` 内部懒导入 `pypinyin`（生产联网兜底链路已在用）。`_unique_bucket_name` 是同包私有 helper，撞名加数字后缀并保证 ≤63，已天然规避 409。

- [ ] **Step 5: 跑测试确认通过（含 Task 1 用例不回归）**

Run: `python -m pytest server/tests/test_image_library_folder_ops.py -q`
Expected: 5 passed。

- [ ] **Step 6: 跑既有 kind 测试确认不回归**

Run: `python -m pytest server/tests/test_image_library_kind.py -q`
Expected: 2 passed（`test_category_kind_create_default_and_filter` 里 `_make_cat` 仍传 bucket，走显式分支不变）。

- [ ] **Step 7: ruff + 提交**

```bash
ruff check server/app/modules/image_library/router.py
git add server/app/modules/image_library/router.py server/tests/test_image_library_folder_ops.py
git commit -m "feat(image-library): 创建分类 bucket 可选,缺省按拼音自动派名"
```

---

## Task 3: 前端 API — deleteCategory + createCategory bucket 可选

**Files:**
- Modify: `web/src/api/image-library.ts`

- [ ] **Step 1: 改 `createCategory` 让 bucket_name 可选**

在 `web/src/api/image-library.ts` 把 `createCategory` 的入参类型：

```ts
export function createCategory(payload: {
  name: string;
  bucket_name: string;
  kind?: "main" | "companion";
  description?: string | null;
  official_url?: string | null;
}): Promise<StockCategory> {
```

改为（`bucket_name` 加 `?`）：

```ts
export function createCategory(payload: {
  name: string;
  bucket_name?: string;
  kind?: "main" | "companion";
  description?: string | null;
  official_url?: string | null;
}): Promise<StockCategory> {
```

函数体不变（`JSON.stringify(payload)` 省略 `bucket_name` 时后端自动派名）。

- [ ] **Step 2: 新增 `deleteCategory`**

在 `updateCategory` 之后插入：

```ts
export function deleteCategory(categoryId: number): Promise<void> {
  return api<void>(`/api/image-library/categories/${categoryId}`, { method: "DELETE" });
}
```

- [ ] **Step 3: typecheck**

Run: `pnpm --filter @geo/web typecheck`
Expected: 通过（`ImageLibraryWorkspace` 仍传 `bucket_name`，可选化不影响它）。

- [ ] **Step 4: 提交**

```bash
git add web/src/api/image-library.ts
git commit -m "feat(web): image-library api 加 deleteCategory + createCategory bucket 可选"
```

---

## Task 4: 工具栏 — 文字按钮换磁盘图标

**Files:**
- Modify: `web/src/components/editor/EditorToolbar.tsx`

- [ ] **Step 1: import 加 `Save`**

在 `web/src/components/editor/EditorToolbar.tsx` 顶部 lucide-react import 列表里加 `Save`（按字母位置插入即可），例如把：

```tsx
  List, ListOrdered, Quote, Redo2, Strikethrough,
```

改为：

```tsx
  List, ListOrdered, Quote, Redo2, Save, Strikethrough,
```

- [ ] **Step 2: 末尾按钮文字 → 图标**

把文件末尾这段（约 L83–90）：

```tsx
      <button
        onClick={onSaveImage}
        disabled={!imageSelected}
        title={imageSelected ? "把选中图片存进图库" : "先选中正文中的图片"}
        type="button"
      >
        图片保存
      </button>
```

替换为：

```tsx
      <button
        onClick={onSaveImage}
        disabled={!imageSelected}
        title={imageSelected ? "图片保存到图库" : "先选中正文中的图片"}
        type="button"
      >
        <Save size={16} />
      </button>
```

- [ ] **Step 3: typecheck**

Run: `pnpm --filter @geo/web typecheck`
Expected: 通过。

- [ ] **Step 4: 提交**

```bash
git add web/src/components/editor/EditorToolbar.tsx
git commit -m "feat(web): 编辑器工具栏图片保存改用磁盘图标"
```

---

## Task 5: 正文选中图片高亮（CSS）

**Files:**
- Modify: `web/src/styles.css`

- [ ] **Step 1: 加选中态高亮规则**

在 `web/src/styles.css` 的 `.editorSurface img { max-width: 100%; border-radius: var(--r); }`（约 L887）这一行**之后**新增：

```css
/* 正文里被选中的图片：明显蓝色描边 + 外发光（Tiptap 自动加 .ProseMirror-selectednode） */
.editorSurface img.ProseMirror-selectednode {
  outline: 3px solid var(--accent);
  outline-offset: 2px;
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent) 25%, transparent);
}
```

- [ ] **Step 2: build 确认 CSS 无语法错误**

Run: `pnpm --filter @geo/web build`
Expected: 构建成功。

- [ ] **Step 3: 提交**

```bash
git add web/src/styles.css
git commit -m "feat(web): 正文选中图片加显眼蓝框高亮"
```

> 手动验收（实现末尾统一做）：编辑器里点一张正文图片，图片四周出现明显蓝框。

---

## Task 6: 重写 ImageSaveDialog 为文件浏览式弹框

**Files:**
- Modify: `web/src/components/editor/ImageSaveDialog.tsx`（整体替换）
- Modify: `web/src/styles.css`（追加弹框样式）

- [ ] **Step 1: 整体替换 ImageSaveDialog.tsx**

把 `web/src/components/editor/ImageSaveDialog.tsx` 全文替换为：

```tsx
import { useEffect, useState } from "react";
import { ChevronLeft, Folder, FolderPlus, Trash2 } from "lucide-react";
import {
  createCategory,
  deleteCategory,
  listCategories,
  listImages,
  uploadImage,
} from "../../api/image-library";
import type { StockCategory, StockImage } from "../../types";
import { Modal } from "../Modal";

const KIND_LABEL: Record<"main" | "companion", string> = {
  main: "主推游戏",
  companion: "陪衬游戏",
};

/**
 * 文件浏览式「保存至图片库」弹框：把编辑器里选中的图片存进图片库。
 * 左栏切主推/陪衬 → 网格第一层是文件夹(=bucket)，点进去看已有图片 → 底部命名 + 保存。
 * 支持新建文件夹（只填名字，bucket 后端自动派名）/ 删除空文件夹。
 * 取图走 fetch(imageSrc)→Blob→File，再调 uploadImage 落 MinIO。
 */
export function ImageSaveDialog({
  imageSrc,
  onClose,
  onSaved,
  onError,
}: {
  imageSrc: string; // editor.getAttributes("image").src
  onClose: () => void;
  onSaved: (msg: string) => void;
  onError?: (msg: string) => void;
}) {
  const [kind, setKind] = useState<"main" | "companion">("main");
  const [folders, setFolders] = useState<StockCategory[]>([]);
  const [currentFolder, setCurrentFolder] = useState<StockCategory | null>(null);
  const [images, setImages] = useState<StockImage[]>([]);
  const [filename, setFilename] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [creating, setCreating] = useState(false);
  const [newFolderName, setNewFolderName] = useState("");
  const [folderBusy, setFolderBusy] = useState(false);

  // 切 kind：拉该类文件夹，复位下钻态
  useEffect(() => {
    let cancelled = false;
    setCurrentFolder(null);
    setImages([]);
    setCreating(false);
    setNewFolderName("");
    listCategories(kind)
      .then((data) => {
        if (!cancelled) setFolders(data);
      })
      .catch(() => {
        if (!cancelled) setFolders([]);
      });
    return () => {
      cancelled = true;
    };
  }, [kind]);

  // 进入某文件夹：拉它的图片
  useEffect(() => {
    if (currentFolder == null) {
      setImages([]);
      return;
    }
    let cancelled = false;
    listImages({ category_id: currentFolder.id })
      .then((data) => {
        if (!cancelled) setImages(data);
      })
      .catch(() => {
        if (!cancelled) setImages([]);
      });
    return () => {
      cancelled = true;
    };
  }, [currentFolder]);

  async function refreshFolders(selectId?: number) {
    const data = await listCategories(kind);
    setFolders(data);
    if (selectId != null) {
      setCurrentFolder(data.find((f) => f.id === selectId) ?? null);
    }
  }

  async function handleCreateFolder() {
    const name = newFolderName.trim();
    if (!name) return;
    setFolderBusy(true);
    setError(null);
    try {
      const cat = await createCategory({ name, kind });
      setCreating(false);
      setNewFolderName("");
      await refreshFolders(cat.id); // 自动下钻进新文件夹
    } catch (e) {
      const msg = e instanceof Error ? e.message : "新建文件夹失败";
      setError(msg);
      onError?.(msg);
    } finally {
      setFolderBusy(false);
    }
  }

  async function handleDeleteFolder() {
    if (currentFolder == null) return;
    if (!window.confirm(`确定删除文件夹「${currentFolder.name}」？`)) return;
    setFolderBusy(true);
    setError(null);
    try {
      await deleteCategory(currentFolder.id);
      setCurrentFolder(null);
      await refreshFolders();
    } catch (e) {
      const msg = e instanceof Error ? e.message : "该文件夹内还有图片，请先清空后再删除";
      setError(msg);
      onError?.(msg);
    } finally {
      setFolderBusy(false);
    }
  }

  async function handleSave() {
    if (currentFolder == null) {
      setError("请先进入一个文件夹");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const resp = await fetch(imageSrc);
      if (!resp.ok) throw new Error("读取图片失败");
      const blob = await resp.blob();
      const type = blob.type || "image/png";
      const ext = type.split("/")[1] || "png";
      const trimmed = filename.trim();
      const base = trimmed || `image-${Date.now()}`;
      const name = base.includes(".") ? base : `${base}.${ext}`;
      const file = new File([blob], name, { type });
      await uploadImage({ category_id: currentFolder.id, file });
      onSaved(`已保存到图库：${name}`);
      onClose();
    } catch (e) {
      // 跨源图片 fetch 可能被 CORS 挡 → 提示后由用户改用本地上传
      const msg = e instanceof Error ? e.message : "保存失败（可能是跨源图片）";
      setError(msg);
      onError?.(msg);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      title="保存至图片库"
      onClose={onClose}
      width={860}
      maxHeight={640}
      footer={
        <div className="imgSaveFooter">
          <label className="imgSaveNameField">
            图片名称
            <input
              value={filename}
              placeholder="留空则自动命名，如：餐厅养成记 · 封面"
              onChange={(e) => setFilename(e.target.value)}
            />
          </label>
          <div className="imgSaveFooterBtns">
            <button type="button" onClick={onClose} disabled={saving}>
              取消
            </button>
            <button
              type="button"
              className="primaryButton"
              onClick={() => void handleSave()}
              disabled={saving || currentFolder == null}
            >
              {saving ? "保存中…" : "保存"}
            </button>
          </div>
        </div>
      }
    >
      <div className="imgSaveBrowser">
        <div className="imgSaveTopbar">
          <button
            type="button"
            className="imgSaveNavBtn"
            disabled={currentFolder == null}
            onClick={() => setCurrentFolder(null)}
            title="返回上一层"
          >
            <ChevronLeft size={16} />
          </button>
          <div className="imgSaveCrumb">
            <button type="button" className="imgSaveCrumbLink" onClick={() => setCurrentFolder(null)}>
              图片库
            </button>
            <span className="imgSaveCrumbSep">›</span>
            <button type="button" className="imgSaveCrumbLink" onClick={() => setCurrentFolder(null)}>
              {KIND_LABEL[kind]}
            </button>
            {currentFolder && (
              <>
                <span className="imgSaveCrumbSep">›</span>
                <span className="imgSaveCrumbCurrent">{currentFolder.name}</span>
              </>
            )}
          </div>
          <div className="imgSaveTopActions">
            <button
              type="button"
              onClick={() => {
                setCreating(true);
                setNewFolderName("");
              }}
            >
              <FolderPlus size={14} /> 新建文件夹
            </button>
            <button
              type="button"
              disabled={currentFolder == null || folderBusy}
              onClick={() => void handleDeleteFolder()}
            >
              <Trash2 size={14} /> 删除文件夹
            </button>
          </div>
        </div>

        {creating && (
          <div className="imgSaveCreateRow">
            <input
              autoFocus
              value={newFolderName}
              placeholder="文件夹名称（如：餐厅养成记）"
              onChange={(e) => setNewFolderName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void handleCreateFolder();
                if (e.key === "Escape") setCreating(false);
              }}
            />
            <button
              type="button"
              className="primaryButton"
              disabled={folderBusy || !newFolderName.trim()}
              onClick={() => void handleCreateFolder()}
            >
              确认
            </button>
            <button type="button" onClick={() => setCreating(false)} disabled={folderBusy}>
              取消
            </button>
          </div>
        )}

        <div className="imgSaveBody">
          <aside className="imgSaveSidebar">
            {(["main", "companion"] as const).map((k) => (
              <button
                key={k}
                type="button"
                className={`imgSaveSideBtn${kind === k ? " active" : ""}`}
                onClick={() => setKind(k)}
              >
                {KIND_LABEL[k]}
              </button>
            ))}
          </aside>

          <div className="imgSaveGrid">
            {currentFolder == null ? (
              folders.length === 0 ? (
                <p className="emptyText">该类别下暂无文件夹，点「新建文件夹」开始</p>
              ) : (
                folders.map((f) => (
                  <button
                    key={f.id}
                    type="button"
                    className="imgSaveFolderCard"
                    onClick={() => setCurrentFolder(f)}
                  >
                    <Folder size={40} strokeWidth={1.3} />
                    <span className="imgSaveFolderName">{f.name}</span>
                  </button>
                ))
              )
            ) : images.length === 0 ? (
              <p className="emptyText">这个文件夹还没有图片</p>
            ) : (
              images.map((img) => (
                <div key={img.id} className="imgSaveImgCard">
                  <img src={img.url} alt={img.filename} loading="lazy" />
                  <span className="imgSaveImgName" title={img.filename}>
                    {img.filename}
                  </span>
                </div>
              ))
            )}
          </div>
        </div>

        {error ? <p className="imageSaveError">{error}</p> : null}
      </div>
    </Modal>
  );
}
```

> 复用现有 class：`primaryButton`、`emptyText`、`imageSaveError`。`Date.now()` 在浏览器代码里可用（Workflow 脚本才禁用）。

- [ ] **Step 2: 追加弹框样式**

在 `web/src/styles.css` 的 `/* Image save dialog (editor → image library) */`（约 L658）区块**之后**追加（旧的 `.imageSavePreview/.imageSaveTabs/.imageSaveCatList/.imageSaveNameRow` 规则现已无组件引用，留着无害，可后续清理）：

```css
/* Image save dialog v2: 文件浏览式 */
.imgSaveBrowser { display: flex; flex-direction: column; gap: 10px; min-height: 420px; }
.imgSaveTopbar { display: flex; align-items: center; gap: 10px; }
.imgSaveNavBtn {
  display: inline-flex; align-items: center; justify-content: center;
  width: 30px; height: 30px; padding: 0;
  border: 1px solid var(--hair); border-radius: var(--r); background: var(--paper);
}
.imgSaveNavBtn:disabled { opacity: .4; cursor: not-allowed; }
.imgSaveCrumb { display: flex; align-items: center; gap: 6px; flex: 1; min-width: 0; font-size: 13px; }
.imgSaveCrumbLink {
  border: none; background: none; padding: 0; cursor: pointer;
  color: var(--fg-2); font-size: 13px;
}
.imgSaveCrumbLink:hover { color: var(--accent); }
.imgSaveCrumbSep { color: var(--fg-3); }
.imgSaveCrumbCurrent { color: var(--fg); font-weight: 600; }
.imgSaveTopActions { display: flex; gap: 8px; }
.imgSaveTopActions button {
  display: inline-flex; align-items: center; gap: 4px;
  height: 30px; padding: 0 10px; font-size: 13px;
  border: 1px solid var(--hair); border-radius: var(--r); background: var(--paper); cursor: pointer;
}
.imgSaveTopActions button:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.imgSaveTopActions button:disabled { opacity: .4; cursor: not-allowed; }

.imgSaveCreateRow { display: flex; gap: 8px; }
.imgSaveCreateRow input {
  flex: 1; height: 32px; padding: 0 10px;
  border: 1px solid var(--hair); border-radius: var(--r);
}

.imgSaveBody { display: flex; gap: 12px; flex: 1; min-height: 0; }
.imgSaveSidebar { display: flex; flex-direction: column; gap: 4px; width: 132px; flex-shrink: 0; }
.imgSaveSideBtn {
  text-align: left; padding: 9px 12px; font-size: 14px;
  border: 1px solid transparent; border-radius: var(--r); background: none; cursor: pointer; color: var(--fg-2);
}
.imgSaveSideBtn:hover { background: var(--cream); }
.imgSaveSideBtn.active { background: var(--accent-soft); color: var(--accent-deep); font-weight: 600; }

.imgSaveGrid {
  flex: 1; min-width: 0;
  display: grid; grid-template-columns: repeat(auto-fill, minmax(132px, 1fr));
  gap: 12px; align-content: start;
  max-height: 380px; overflow-y: auto;
  border: 1px solid var(--hair); border-radius: var(--r); padding: 12px;
}
.imgSaveFolderCard {
  display: flex; flex-direction: column; align-items: center; gap: 8px;
  padding: 16px 8px; cursor: pointer;
  border: 1px solid var(--hair); border-radius: var(--r); background: var(--paper); color: var(--fg-2);
}
.imgSaveFolderCard:hover { border-color: var(--accent); color: var(--accent); }
.imgSaveFolderName {
  font-size: 13px; max-width: 100%;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.imgSaveImgCard { display: flex; flex-direction: column; gap: 4px; }
.imgSaveImgCard img {
  width: 100%; aspect-ratio: 1; object-fit: cover;
  border-radius: var(--r); border: 1px solid var(--hair); background: var(--cream);
}
.imgSaveImgName {
  font-size: 12px; color: var(--fg-3);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

.imgSaveFooter { display: flex; align-items: center; gap: 12px; width: 100%; }
.imgSaveNameField {
  display: flex; align-items: center; gap: 8px; flex: 1;
  font-size: 13px; color: var(--fg-2);
}
.imgSaveNameField input {
  flex: 1; height: 34px; padding: 0 10px;
  border: 1px solid var(--hair); border-radius: var(--r);
}
.imgSaveFooterBtns { display: flex; gap: 8px; flex-shrink: 0; }
```

- [ ] **Step 3: typecheck**

Run: `pnpm --filter @geo/web typecheck`
Expected: 通过。

- [ ] **Step 4: build**

Run: `pnpm --filter @geo/web build`
Expected: 构建成功。

- [ ] **Step 5: 提交**

```bash
git add web/src/components/editor/ImageSaveDialog.tsx web/src/styles.css
git commit -m "feat(web): 图片保存改为文件浏览式弹框(两层下钻+新建/删除文件夹)"
```

---

## Task 7: 全量收口（前后端门禁 + 手动验收）

**Files:** 无（仅验证）

- [ ] **Step 1: 后端 image_library 相关测试全绿**

Run: `python -m pytest server/tests/test_image_library_folder_ops.py server/tests/test_image_library_kind.py -q`
Expected: 7 passed。

- [ ] **Step 2: 前端门禁**

Run: `pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build`
Expected: 两步均成功。

- [ ] **Step 3: 手动验收清单（本地起 Vite 5173 + 后端 8000 + MinIO）**

- [ ] 编辑器里点选一张正文图片 → 图片四周出现明显蓝框（Task 5）。
- [ ] 工具栏末尾是磁盘图标，选中图片时可点、未选时灰（Task 4）。
- [ ] 点图标弹出「保存至图片库」：左栏切主推/陪衬，网格先显示文件夹卡 → 点进去显示该文件夹已有图片，面包屑/返回键能回上一层（Task 6）。
- [ ] 「新建文件夹」只填名字即可创建并自动进入；「删除文件夹」删空文件夹成功、删非空文件夹弹错误提示（Task 1/2/6）。
- [ ] 进入某文件夹 → 填图片名称 → 保存 → toast 成功，图片出现在该文件夹/整页图片库里。

> 注：本地无 MinIO 时无法走通保存/建桶链路；MinIO 不可用属环境问题，非本次改动缺陷。

---

## 自检记录（写计划时已核对）

- **Spec 覆盖**：选中高亮(Task5)、图标(Task4)、删分类+非空禁止(Task1)、bucket 自动派名(Task2)、前端 API(Task3)、浏览式弹框(Task6)、整页图片库不动(无任务=有意不改) —— 全覆盖。
- **类型一致**：`createCategory({name, kind})`（bucket 省略）与 Task2/3 的可选化一致；`deleteCategory(id)` 在 Task3 定义、Task6 使用；`StockCategory.kind` 用现有联合类型；`primaryButton/emptyText/imageSaveError` 为现有 class。
- **无占位符**：每个代码步骤均给出完整可粘贴代码与确切命令/预期。
```
