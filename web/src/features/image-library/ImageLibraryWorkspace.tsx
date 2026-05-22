import { useEffect, useRef, useState } from "react";
import { createCategory, deleteImage, listCategories, listImages, uploadImage } from "../../api/image-library";
import type { StockCategory, StockImage } from "../../types";
import { useToast } from "../../components/Toast";

export function ImageLibraryWorkspace() {
  const { toast: showToast } = useToast();
  const [categories, setCategories] = useState<StockCategory[]>([]);
  const [selectedCategoryId, setSelectedCategoryId] = useState<number | null>(null);
  const [images, setImages] = useState<StockImage[]>([]);
  const [loading, setLoading] = useState(false);

  // New category dialog
  const [showNewCat, setShowNewCat] = useState(false);
  const [catName, setCatName] = useState("");
  const [catBucket, setCatBucket] = useState("");
  const [catDesc, setCatDesc] = useState("");
  const [catSaving, setCatSaving] = useState(false);

  // Upload dialog
  const [showUpload, setShowUpload] = useState(false);
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadTags, setUploadTags] = useState("");
  const [uploadDesc, setUploadDesc] = useState("");
  const [uploadCategoryId, setUploadCategoryId] = useState<number | null>(null);
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

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
    if (!uploadFile || uploadCategoryId === null) return;
    setUploading(true);
    try {
      const img = await uploadImage({
        category_id: uploadCategoryId,
        tags: uploadTags.trim() || undefined,
        description: uploadDesc.trim() || undefined,
        file: uploadFile,
      });
      if (img.category_id === selectedCategoryId) {
        setImages((prev) => [img, ...prev]);
      }
      setShowUpload(false);
      setUploadFile(null); setUploadTags(""); setUploadDesc("");
      showToast("图片上传成功", "success");
    } catch (e: unknown) {
      showToast((e as Error).message, "error");
    } finally {
      setUploading(false);
    }
  }

  async function handleDelete(img: StockImage) {
    if (!window.confirm(`确定删除图片「${img.filename}」？`)) return;
    try {
      await deleteImage(img.id);
      setImages((prev) => prev.filter((i) => i.id !== img.id));
      showToast("已删除", "success");
    } catch (e: unknown) {
      showToast((e as Error).message, "error");
    }
  }

  return (
    <div className="imageLibrary">
      <div className="topbar">
        <div>
          <p className="eyebrow">素材</p>
          <h1>图片库</h1>
        </div>
        <div className="topbarActions">
          <button type="button" className="btn btn-secondary" onClick={() => setShowNewCat(true)}>
            + 新建栏目
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
            上传图片
          </button>
        </div>
      </div>

      <div className="imageLibraryLayout">
        {/* 栏目侧边栏 */}
        <aside className="imageLibrarySidebar">
          {categories.map((cat) => (
            <button
              key={cat.id}
              type="button"
              className={`imageLibraryCatBtn${selectedCategoryId === cat.id ? " active" : ""}`}
              onClick={() => setSelectedCategoryId(cat.id)}
            >
              <span className="imageLibraryCatName">{cat.name}</span>
              <span className="imageLibraryCatBucket">{cat.bucket_name}</span>
            </button>
          ))}
          {categories.length === 0 && (
            <p className="imageLibraryEmpty">暂无栏目，点击「新建栏目」开始</p>
          )}
        </aside>

        {/* 图片网格 */}
        <div className="imageLibraryGrid">
          {loading && <p className="imageLibraryLoading">加载中...</p>}
          {!loading && images.length === 0 && (
            <p className="imageLibraryEmpty">暂无图片，上传第一张吧</p>
          )}
          {images.map((img) => (
            <div key={img.id} className="imageLibraryCard">
              <div className="imageLibraryCardImg">
                <img src={img.url} alt={img.filename} loading="lazy" />
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
                <button
                  type="button"
                  className="imageLibraryDeleteBtn"
                  onClick={() => handleDelete(img)}
                >
                  删除
                </button>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* 新建栏目弹窗 */}
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

      {/* 上传图片弹窗 */}
      {showUpload && (
        <div className="modalOverlay" onClick={() => setShowUpload(false)}>
          <div className="modalCard" onClick={(e) => e.stopPropagation()}>
            <h2>上传图片</h2>
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
              图片文件
              <input
                ref={fileInputRef}
                type="file"
                accept="image/jpeg,image/png,image/webp,image/gif"
                onChange={(e) => setUploadFile(e.target.files?.[0] ?? null)}
              />
            </label>
            {uploadFile && (
              <p className="imageUploadPreviewName">{uploadFile.name}</p>
            )}
            <label>
              标签（逗号分隔，选填）
              <input value={uploadTags} onChange={(e) => setUploadTags(e.target.value)} placeholder="如：角色,战斗" />
            </label>
            <label>
              描述（选填）
              <input value={uploadDesc} onChange={(e) => setUploadDesc(e.target.value)} placeholder="图片内容描述，供 AI 配图参考" />
            </label>
            <div className="modalActions">
              <button type="button" className="btn btn-secondary" onClick={() => setShowUpload(false)}>取消</button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={uploading || !uploadFile || uploadCategoryId === null}
                onClick={handleUpload}
              >
                {uploading ? "上传中..." : "确认上传"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
