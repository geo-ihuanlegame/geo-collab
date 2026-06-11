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
