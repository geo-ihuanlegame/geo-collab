import { useEffect, useRef, useState } from "react";
import { MoreHorizontal, Plus, Trash2, Upload, Pencil, ChevronLeft, ChevronRight, X } from "lucide-react";
import { createCategory, deleteImage, listCategories, listImages, updateImage, uploadImage } from "../../api/image-library";
import type { StockCategory, StockImage } from "../../types";
import { useToast } from "../../components/Toast";

export function ImageLibraryWorkspace() {
  const { toast: showToast } = useToast();
  const [categories, setCategories] = useState<StockCategory[]>([]);
  const [selectedCategoryId, setSelectedCategoryId] = useState<number | null>(null);
  const [images, setImages] = useState<StockImage[]>([]);
  const [loading, setLoading] = useState(false);

  const [showNewCat, setShowNewCat] = useState(false);
  const [catName, setCatName] = useState("");
  const [catBucket, setCatBucket] = useState("");
  const [catDesc, setCatDesc] = useState("");
  const [catSaving, setCatSaving] = useState(false);

  const [showUpload, setShowUpload] = useState(false);
  const [uploadCategoryId, setUploadCategoryId] = useState<number | null>(null);
  const [uploadFiles, setUploadFiles] = useState<File[]>([]);
  const [batchTags, setBatchTags] = useState("");
  const [batchDesc, setBatchDesc] = useState("");
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [menuOpenId, setMenuOpenId] = useState<number | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const [editingImage, setEditingImage] = useState<StockImage | null>(null);
  const [editTags, setEditTags] = useState("");
  const [editDesc, setEditDesc] = useState("");
  const [editSaving, setEditSaving] = useState(false);

  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpenId(null);
      }
    }
    document.addEventListener("click", handleClickOutside);
    return () => document.removeEventListener("click", handleClickOutside);
  }, []);

  useEffect(() => {
    if (lightboxIndex === null) return;
    function handleKey(e: KeyboardEvent) {
      if (e.key === "Escape") { setLightboxIndex(null); return; }
      if (e.key === "ArrowLeft") setLightboxIndex((i) => i === null ? null : (i - 1 + images.length) % images.length);
      if (e.key === "ArrowRight") setLightboxIndex((i) => i === null ? null : (i + 1) % images.length);
    }
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [lightboxIndex, images]);

  useEffect(() => {
    listCategories()
      .then((cats) => {
        setCategories(cats);
        if (cats.length > 0 && selectedCategoryId === null) {
          setSelectedCategoryId(cats[0].id);
        }
      })
      .catch(() => showToast("加载栏目失败", "error"));
  }, []);

  useEffect(() => {
    setLightboxIndex(null);
    if (selectedCategoryId === null) {
      setImages([]);
      return;
    }
    setLoading(true);
    listImages({ category_id: selectedCategoryId })
      .then(setImages)
      .catch(() => showToast("加载图片失败", "error"))
      .finally(() => setLoading(false));
  }, [selectedCategoryId]);

  async function handleCreateCategory() {
    if (!catName.trim() || !catBucket.trim()) return;
    setCatSaving(true);
    try {
      const cat = await createCategory({ name: catName.trim(), bucket_name: catBucket.trim(), description: catDesc.trim() || null });
      setCategories((prev) => [cat, ...prev]);
      setSelectedCategoryId(cat.id);
      setShowNewCat(false);
      setCatName(""); setCatBucket(""); setCatDesc("");
      showToast("栏目创建成功", "success");
    } catch (e: unknown) {
      showToast((e as Error).message, "error");
    } finally {
      setCatSaving(false);
    }
  }

  async function handleUpload() {
    if (uploadFiles.length === 0 || uploadCategoryId === null) return;
    setUploading(true);
    setUploadProgress(0);
    let successCount = 0;
    for (let i = 0; i < uploadFiles.length; i++) {
      try {
        const img = await uploadImage({
          category_id: uploadCategoryId,
          tags: batchTags.trim() || undefined,
          description: batchDesc.trim() || undefined,
          file: uploadFiles[i],
        });
        if (img.category_id === selectedCategoryId) {
          setImages((prev) => [img, ...prev]);
        }
        successCount++;
      } catch {
        showToast(`第 ${i + 1} 张上传失败`, "error");
      }
      setUploadProgress(i + 1);
    }
    setUploading(false);
    setShowUpload(false);
    setUploadFiles([]); setBatchTags(""); setBatchDesc("");
    showToast(`上传完成：${successCount}/${uploadFiles.length} 张`, successCount === uploadFiles.length ? "success" : "error");
  }

  async function handleDelete(img: StockImage) {
    setMenuOpenId(null);
    if (!window.confirm(`确定删除图片「${img.filename}」？`)) return;
    try {
      await deleteImage(img.id);
      setImages((prev) => prev.filter((i) => i.id !== img.id));
      showToast("已删除", "success");
    } catch (e: unknown) {
      showToast((e as Error).message, "error");
    }
  }

  function openEdit(img: StockImage) {
    setMenuOpenId(null);
    setEditingImage(img);
    setEditTags((img.tags ?? []).join(", "));
    setEditDesc(img.description ?? "");
  }

  async function handleSaveEdit() {
    if (!editingImage) return;
    setEditSaving(true);
    try {
      const updated = await updateImage(editingImage.id, {
        tags: editTags.trim() || null,
        description: editDesc.trim() || null,
      });
      setImages((prev) => prev.map((i) => i.id === updated.id ? updated : i));
      setEditingImage(null);
      showToast("已更新", "success");
    } catch (e: unknown) {
      showToast((e as Error).message, "error");
    } finally {
      setEditSaving(false);
    }
  }

  const lightboxImage = lightboxIndex !== null ? (images[lightboxIndex] ?? null) : null;

  return (
    <div className="imageLibrary">
      <div className="topbar">
        <div>
          <p className="eyebrow">素材</p>
          <h1>图片库</h1>
        </div>
        <div className="topbarActions">
          <button type="button" className="btn btn-secondary" onClick={() => setShowNewCat(true)}>
            <Plus size={15} /> 新建栏目
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={categories.length === 0}
            onClick={() => {
              setUploadCategoryId(selectedCategoryId ?? categories[0]?.id ?? null);
              setShowUpload(true);
            }}
          >
            <Upload size={15} /> 上传图片
          </button>
        </div>
      </div>

      <div className="imageLibraryLayout">
        <aside className="imageLibrarySidebar">
          {categories.map((cat) => (
            <button
              key={cat.id}
              type="button"
              className={`imageLibraryCatBtn${selectedCategoryId === cat.id ? " active" : ""}`}
              onClick={() => setSelectedCategoryId(cat.id)}
            >
              <div className="imageLibraryCatBtnRow">
                <span className="imageLibraryCatName">{cat.name}</span>
                {selectedCategoryId === cat.id && images.length > 0 && (
                  <span className="imageLibraryCatCount">{images.length}</span>
                )}
              </div>
              <span className="imageLibraryCatBucket">{cat.bucket_name}</span>
            </button>
          ))}
          {categories.length === 0 && (
            <p className="imageLibraryEmpty">暂无栏目，点击「新建栏目」开始</p>
          )}
        </aside>

        <div className="imageLibraryGrid">
          {loading && <p className="imageLibraryLoading">加载中...</p>}
          {!loading && images.length === 0 && (
            <p className="imageLibraryEmpty">暂无图片，上传第一张吧</p>
          )}
          {images.map((img, idx) => (
            <div key={img.id} className="imageLibraryCard">
              <div className="imageLibraryCardImg" onClick={() => setLightboxIndex(idx)}>
                <img src={img.url} alt={img.filename} loading="lazy" />
                <div className="imageLibraryCardOverlay">
                  <span className="imageLibraryCardOverlayName">{img.filename}</span>
                </div>
              </div>
              <div className="imageLibraryCardActions">
                <button
                  type="button"
                  className="imageLibraryMenuBtn"
                  onClick={(e) => { e.stopPropagation(); setMenuOpenId(menuOpenId === img.id ? null : img.id); }}
                >
                  <MoreHorizontal size={16} />
                </button>
                {menuOpenId === img.id && (
                  <div className="imageLibraryDropdown" ref={menuRef}>
                    <button type="button" onClick={() => openEdit(img)}>
                      <Pencil size={13} /> 编辑标签
                    </button>
                    <button type="button" className="danger" onClick={() => handleDelete(img)}>
                      <Trash2 size={13} /> 删除
                    </button>
                  </div>
                )}
              </div>
              <div className="imageLibraryCardInfo">
                <p className="imageLibraryCardName" title={img.filename}>{img.filename}</p>
                {img.tags.length > 0 && (
                  <div className="imageLibraryCardTags">
                    {img.tags.map((tag) => (
                      <span key={tag} className="imageLibraryTag">{tag}</span>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>

      {showNewCat && (
        <div className="modalOverlay" onClick={() => setShowNewCat(false)}>
          <div className="modalCard" onClick={(e) => e.stopPropagation()}>
            <h2>新建栏目</h2>
            <label>
              栏目名称
              <input value={catName} onChange={(e) => setCatName(e.target.value)} placeholder="如：原神" />
            </label>
            <label>
              Bucket 名称
              <input value={catBucket} onChange={(e) => setCatBucket(e.target.value)} placeholder="如：geo-genshin（仅小写字母、数字、连字符）" />
            </label>
            <label>
              描述（选填）
              <input value={catDesc} onChange={(e) => setCatDesc(e.target.value)} placeholder="栏目说明" />
            </label>
            <div className="modalActions">
              <button type="button" className="btn btn-secondary" onClick={() => setShowNewCat(false)}>取消</button>
              <button type="button" className="btn btn-primary" disabled={catSaving || !catName.trim() || !catBucket.trim()} onClick={handleCreateCategory}>
                {catSaving ? "创建中..." : "确认创建"}
              </button>
            </div>
          </div>
        </div>
      )}

      {showUpload && (
        <div className="modalOverlay" onClick={() => setShowUpload(false)}>
          <div className="modalCard" onClick={(e) => e.stopPropagation()}>
            <h2>上传图片{uploading ? ` (${uploadProgress}/${uploadFiles.length})` : ""}</h2>
            <label>
              选择栏目
              <select
                value={uploadCategoryId ?? ""}
                onChange={(e) => setUploadCategoryId(Number(e.target.value))}
              >
                {categories.map((cat) => (
                  <option key={cat.id} value={cat.id}>{cat.name}</option>
                ))}
              </select>
            </label>
            <label>
              选择文件（可多选）
              <input
                ref={fileInputRef}
                type="file"
                multiple
                accept="image/jpeg,image/png,image/webp,image/gif"
                onChange={(e) => setUploadFiles(Array.from(e.target.files ?? []))}
              />
            </label>
            {uploadFiles.length > 0 && (
              <p className="imageUploadPreviewName">已选 {uploadFiles.length} 张图片</p>
            )}
            <label>
              统一标签（选填，逗号分隔）
              <input value={batchTags} onChange={(e) => setBatchTags(e.target.value)} placeholder="如：角色,战斗" />
            </label>
            <label>
              统一描述（选填）
              <input value={batchDesc} onChange={(e) => setBatchDesc(e.target.value)} placeholder="图片内容描述，供 AI 配图参考" />
            </label>
            <div className="modalActions">
              <button type="button" className="btn btn-secondary" onClick={() => setShowUpload(false)} disabled={uploading}>取消</button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={uploading || uploadFiles.length === 0 || uploadCategoryId === null}
                onClick={handleUpload}
              >
                {uploading ? `上传中 ${uploadProgress}/${uploadFiles.length}...` : "上传全部"}
              </button>
            </div>
          </div>
        </div>
      )}

      {editingImage && (
        <div className="modalOverlay" onClick={() => setEditingImage(null)}>
          <div className="modalCard" onClick={(e) => e.stopPropagation()}>
            <h2>编辑标签</h2>
            <p className="imageUploadPreviewName">{editingImage.filename}</p>
            <label>
              标签（逗号分隔）
              <input value={editTags} onChange={(e) => setEditTags(e.target.value)} placeholder="如：角色,战斗" />
            </label>
            <label>
              描述
              <input value={editDesc} onChange={(e) => setEditDesc(e.target.value)} placeholder="图片内容描述" />
            </label>
            <div className="modalActions">
              <button type="button" className="btn btn-secondary" onClick={() => setEditingImage(null)}>取消</button>
              <button type="button" className="btn btn-primary" disabled={editSaving} onClick={handleSaveEdit}>
                {editSaving ? "保存中..." : "保存"}
              </button>
            </div>
          </div>
        </div>
      )}

      {lightboxImage && (
        <div className="lightboxOverlay" onClick={() => setLightboxIndex(null)}>
          <div className="lightboxInner" onClick={(e) => e.stopPropagation()}>
            <button type="button" className="lightboxClose" onClick={() => setLightboxIndex(null)}>
              <X size={20} />
            </button>
            <img className="lightboxImg" src={lightboxImage.url} alt={lightboxImage.filename} />
            <div className="lightboxInfo">
              <p className="lightboxInfoName">{lightboxImage.filename}</p>
              {lightboxImage.width != null && lightboxImage.height != null && (
                <p className="lightboxInfoDim">{lightboxImage.width} × {lightboxImage.height}</p>
              )}
              {lightboxImage.tags.length > 0 && (
                <div className="lightboxInfoTags">
                  {lightboxImage.tags.map((tag) => (
                    <span key={tag} className="lightboxTag">{tag}</span>
                  ))}
                </div>
              )}
              {lightboxImage.description && (
                <p className="lightboxInfoDesc">{lightboxImage.description}</p>
              )}
            </div>
          </div>
          {images.length > 1 && (
            <>
              <button
                type="button"
                className="lightboxArrow lightboxArrowLeft"
                onClick={(e) => {
                  e.stopPropagation();
                  setLightboxIndex((i) => i === null ? null : (i - 1 + images.length) % images.length);
                }}
              >
                <ChevronLeft size={28} />
              </button>
              <button
                type="button"
                className="lightboxArrow lightboxArrowRight"
                onClick={(e) => {
                  e.stopPropagation();
                  setLightboxIndex((i) => i === null ? null : (i + 1) % images.length);
                }}
              >
                <ChevronRight size={28} />
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
