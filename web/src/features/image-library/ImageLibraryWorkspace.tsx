import { useEffect, useRef, useState } from "react";
import { MoreHorizontal, Plus, Trash2, Upload, Pencil, ChevronLeft, ChevronRight, X, Images, Search, ArrowUpDown } from "lucide-react";
import { createCategory, deleteImage, listCategories, listImages, searchImages, updateCategory, updateImage, uploadImage } from "../../api/image-library";
import type { ImageSearchResult, StockCategory, StockImage } from "../../types";
import { useToast } from "../../components/Toast";

type CategorySort = "created" | "name_asc" | "name_desc" | "latest_image";

type PendingJump = {
  kind: "main" | "companion";
  categoryId: number;
  imageId: number;
};

export function ImageLibraryWorkspace() {
  const { toast: showToast } = useToast();
  const [categories, setCategories] = useState<StockCategory[]>([]);
  const [selectedCategoryId, setSelectedCategoryId] = useState<number | null>(null);
  const [images, setImages] = useState<StockImage[]>([]);
  const [loading, setLoading] = useState(false);
  const [kindTab, setKindTab] = useState<"main" | "companion">("companion");

  const [showNewCat, setShowNewCat] = useState(false);
  const [catName, setCatName] = useState("");
  const [catBucket, setCatBucket] = useState("");
  const [catDesc, setCatDesc] = useState("");
  const [catUrl, setCatUrl] = useState("");
  const [catSaving, setCatSaving] = useState(false);

  const [editingCategory, setEditingCategory] = useState<StockCategory | null>(null);
  const [editCatName, setEditCatName] = useState("");
  const [editCatKind, setEditCatKind] = useState<"main" | "companion">("companion");
  const [editCatDesc, setEditCatDesc] = useState("");
  const [editCatUrl, setEditCatUrl] = useState("");
  const [editCatSaving, setEditCatSaving] = useState(false);

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

  // Search state
  const [searchInput, setSearchInput] = useState("");
  const [searchResults, setSearchResults] = useState<ImageSearchResult[]>([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const searchDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const searchBoxRef = useRef<HTMLLabelElement>(null);
  const searchOverlayRef = useRef<HTMLDivElement>(null);

  // Pending jump: set when user clicks a search result
  const [pendingJump, setPendingJump] = useState<PendingJump | null>(null);
  // Track highlighted card id
  const [highlightedImageId, setHighlightedImageId] = useState<number | null>(null);

  // Category sort state (does NOT reset when switching tabs)
  const [categorySort, setCategorySort] = useState<CategorySort>("created");
  // Sort menu open/closed (React state, not DOM classList)
  const [sortMenuOpen, setSortMenuOpen] = useState(false);
  const sortMenuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpenId(null);
      }
      // Close search overlay on outside click
      if (
        searchOpen &&
        searchBoxRef.current &&
        !searchBoxRef.current.contains(e.target as Node) &&
        searchOverlayRef.current &&
        !searchOverlayRef.current.contains(e.target as Node)
      ) {
        setSearchOpen(false);
      }
      // Close sort menu on outside click
      if (sortMenuOpen && sortMenuRef.current && !sortMenuRef.current.contains(e.target as Node)) {
        setSortMenuOpen(false);
      }
    }
    document.addEventListener("click", handleClickOutside);
    return () => document.removeEventListener("click", handleClickOutside);
  }, [searchOpen, sortMenuOpen]);

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

  // ESC closes search overlay and sort menu (when not in lightbox)
  useEffect(() => {
    if (!searchOpen && !sortMenuOpen) return;
    function handleKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        setSearchOpen(false);
        setSortMenuOpen(false);
      }
    }
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [searchOpen, sortMenuOpen]);

  useEffect(() => {
    // 切换主推/陪衬 tab 时按 kind 重新拉取栏目；若有 pendingJump 且目标在新列表里，选中它；否则选首项。
    //
    // Why pendingJump is intentionally omitted from deps:
    // This effect must ONLY re-run when kindTab changes — not every time pendingJump updates
    // (which would re-fetch categories on every search-result click). Correctness is guaranteed
    // because handleSearchResultClick always calls setPendingJump(...) and setKindTab(...) in the
    // same synchronous event handler: React batches both state updates into a single re-render, so
    // by the time this effect fires (after the re-render), the .then closure already captures the
    // updated pendingJump value.
    listCategories(kindTab)
      .then((cats) => {
        setCategories(cats);
        if (pendingJump && cats.some((c) => c.id === pendingJump.categoryId)) {
          setSelectedCategoryId(pendingJump.categoryId);
        } else {
          setSelectedCategoryId(cats.length > 0 ? cats[0].id : null);
          // If pendingJump was for a category not in this tab, clear it
          if (pendingJump) {
            setPendingJump(null);
          }
        }
      })
      .catch(() => showToast("加载栏目失败", "error"));
  }, [kindTab]); // eslint-disable-line react-hooks/exhaustive-deps

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
  }, [selectedCategoryId]); // eslint-disable-line react-hooks/exhaustive-deps

  // After images load, if there's a pending jump targeting an image in this list, scroll + highlight
  useEffect(() => {
    if (!pendingJump || loading) return;
    const found = images.find((img) => img.id === pendingJump.imageId);
    if (!found) return;

    const el = document.getElementById(`il-card-${found.id}`);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      setHighlightedImageId(found.id);
      setTimeout(() => {
        setHighlightedImageId(null);
      }, 1500);
    }
    setPendingJump(null);
  }, [images, pendingJump, loading]);

  // Debounced search
  useEffect(() => {
    if (searchDebounceRef.current) clearTimeout(searchDebounceRef.current);
    const trimmed = searchInput.trim();
    if (!trimmed) {
      setSearchResults([]);
      setSearchOpen(false);
      setSearchError(false);
      return;
    }
    // Guard against stale in-flight responses: if the input changes or the effect
    // re-runs before the promise resolves, the cleanup sets cancelled=true so the
    // stale .then/.catch won't overwrite results from the newer query.
    let cancelled = false;
    searchDebounceRef.current = setTimeout(async () => {
      setSearchLoading(true);
      setSearchError(false);
      setSearchOpen(true);
      try {
        const results = await searchImages(trimmed);
        if (!cancelled) setSearchResults(results);
      } catch {
        if (!cancelled) {
          setSearchError(true);
          setSearchResults([]);
        }
      } finally {
        if (!cancelled) setSearchLoading(false);
      }
    }, 300);
    return () => {
      cancelled = true;
      if (searchDebounceRef.current) clearTimeout(searchDebounceRef.current);
    };
  }, [searchInput]);

  function handleSearchResultClick(item: ImageSearchResult) {
    // Record pending jump
    setPendingJump({ kind: item.kind, categoryId: item.category_id, imageId: item.id });
    // Close overlay + clear input
    setSearchOpen(false);
    setSearchInput("");
    setSearchResults([]);
    // Switch tab if needed — kindTab effect will handle category selection using pendingJump
    if (item.kind !== kindTab) {
      setKindTab(item.kind);
    } else {
      // Same tab: directly select the category; images effect will scroll
      setSelectedCategoryId(item.category_id);
    }
  }

  // Compute sorted categories
  const sortedCategories = [...categories].sort((a, b) => {
    if (categorySort === "name_asc") {
      return a.name.localeCompare(b.name, "zh-Hans-CN");
    }
    if (categorySort === "name_desc") {
      return b.name.localeCompare(a.name, "zh-Hans-CN");
    }
    if (categorySort === "created") {
      return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
    }
    if (categorySort === "latest_image") {
      const aTime = a.latest_image_at ? new Date(a.latest_image_at).getTime() : -Infinity;
      const bTime = b.latest_image_at ? new Date(b.latest_image_at).getTime() : -Infinity;
      return bTime - aTime;
    }
    return 0;
  });

  async function handleCreateCategory() {
    if (!catName.trim() || !catBucket.trim()) return;
    setCatSaving(true);
    try {
      const cat = await createCategory({
        name: catName.trim(),
        bucket_name: catBucket.trim(),
        kind: kindTab,
        description: catDesc.trim() || null,
        official_url: catUrl.trim() || null,
      });
      setCategories((prev) => [cat, ...prev]);
      setSelectedCategoryId(cat.id);
      setShowNewCat(false);
      setCatName(""); setCatBucket(""); setCatDesc(""); setCatUrl("");
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

  function openCategoryEdit(category: StockCategory) {
    setEditingCategory(category);
    setEditCatName(category.name);
    setEditCatKind(category.kind);
    setEditCatDesc(category.description ?? "");
    setEditCatUrl(category.official_url ?? "");
  }

  async function handleSaveCategoryEdit() {
    if (!editingCategory || !editCatName.trim()) return;
    const movedKind = editCatKind !== editingCategory.kind;
    setEditCatSaving(true);
    try {
      const updated = await updateCategory(editingCategory.id, {
        name: editCatName.trim(),
        kind: editCatKind,
        description: editCatDesc.trim() || null,
        official_url: editCatUrl.trim() || null,
      });
      if (updated.kind !== kindTab) {
        // 归属已改到另一 tab —— 从当前列表移除，必要时清空选中。
        setCategories((prev) => prev.filter((cat) => cat.id !== updated.id));
        setSelectedCategoryId((prev) => (prev === updated.id ? null : prev));
      } else {
        setCategories((prev) => prev.map((cat) => (cat.id === updated.id ? updated : cat)));
      }
      setEditingCategory(null);
      showToast(
        movedKind
          ? `已移到${updated.kind === "main" ? "主推游戏" : "陪衬游戏"}`
          : "栏目已更新",
        "success",
      );
    } catch (e: unknown) {
      showToast((e as Error).message, "error");
    } finally {
      setEditCatSaving(false);
    }
  }

  const lightboxImage = lightboxIndex !== null ? (images[lightboxIndex] ?? null) : null;
  const selectedCategory = categories.find((cat) => cat.id === selectedCategoryId) ?? null;

  return (
    <div className="imageLibrary">
      <div className="topbar">
        <div>
          <p className="eyebrow">素材</p>
          <h1>图片库</h1>
        </div>
      </div>

      <div className="reviewTabs">
        <button
          type="button"
          className={`reviewTabBtn${kindTab === "main" ? " active" : ""}`}
          onClick={() => setKindTab("main")}
        >
          主推游戏
        </button>
        <button
          type="button"
          className={`reviewTabBtn${kindTab === "companion" ? " active" : ""}`}
          onClick={() => setKindTab("companion")}
        >
          陪衬游戏
        </button>

        <div className="imageLibraryTabActions">
          <div className="imageLibrarySearchWrap">
            <label className="imageLibrarySearch" ref={searchBoxRef}>
              <Search size={15} aria-hidden />
              <input
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                placeholder="在图片库中搜索"
                aria-label="在图片库中搜索"
                onFocus={() => {
                  if (searchInput.trim() && (searchResults.length > 0 || searchError)) {
                    setSearchOpen(true);
                  }
                }}
              />
              {searchInput && (
                <button
                  type="button"
                  className="imageLibrarySearchClear"
                  aria-label="清空搜索"
                  onClick={() => { setSearchInput(""); setSearchOpen(false); setSearchResults([]); }}
                >
                  <X size={12} />
                </button>
              )}
            </label>
            {searchOpen && (
              <div className="imageLibrarySearchOverlay" ref={searchOverlayRef}>
                {searchLoading && (
                  <p className="imageLibrarySearchStatus">搜索中…</p>
                )}
                {!searchLoading && searchError && (
                  <p className="imageLibrarySearchStatus imageLibrarySearchError">搜索出错，请重试</p>
                )}
                {!searchLoading && !searchError && searchResults.length === 0 && (
                  <p className="imageLibrarySearchStatus">无匹配</p>
                )}
                {!searchLoading && !searchError && searchResults.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    className="imageLibrarySearchRow"
                    onClick={() => handleSearchResultClick(item)}
                  >
                    <img
                      className="imageLibrarySearchThumb"
                      src={item.url}
                      alt={item.filename}
                      loading="lazy"
                    />
                    <span className="imageLibrarySearchFilename" title={item.filename}>{item.filename}</span>
                    <span className="imageLibrarySearchChip">{item.category_name}</span>
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* 排序下拉：替换原「筛选」按钮 */}
          <div className="imageLibrarySortWrap" ref={sortMenuRef}>
            <button
              type="button"
              className="secondaryButton imageLibrarySortBtn"
              onClick={(e) => {
                e.stopPropagation();
                setSortMenuOpen((prev) => !prev);
              }}
            >
              <ArrowUpDown size={15} /> 排序
            </button>
            <div className={`imageLibrarySortMenu${sortMenuOpen ? " imageLibrarySortMenuOpen" : ""}`}>
              {(
                [
                  { value: "created", label: "创建时间（默认）" },
                  { value: "name_asc", label: "栏目名 A → Z" },
                  { value: "name_desc", label: "栏目名 Z → A" },
                  { value: "latest_image", label: "最新图片时间" },
                ] as { value: CategorySort; label: string }[]
              ).map((opt) => (
                <button
                  key={opt.value}
                  type="button"
                  className={`imageLibrarySortOption${categorySort === opt.value ? " active" : ""}`}
                  onClick={(e) => {
                    e.stopPropagation();
                    setCategorySort(opt.value);
                    setSortMenuOpen(false);
                  }}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          </div>

          <button type="button" className="secondaryButton" onClick={() => setShowNewCat(true)}>
            <Plus size={15} /> 新建栏目
          </button>
          <button
            type="button"
            className="secondaryButton"
            disabled={!selectedCategory}
            onClick={() => { if (selectedCategory) openCategoryEdit(selectedCategory); }}
          >
            <Pencil size={15} /> 编辑栏目
          </button>
          <button
            type="button"
            className="primaryButton"
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
          {sortedCategories.map((cat) => (
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
          {loading && Array.from({ length: 8 }).map((_, i) => (
            <div key={i} className="imageLibraryCardSkeleton" />
          ))}
          {!loading && images.length === 0 && selectedCategoryId !== null && (
            <div className="imageLibraryEmptyState">
              <Images size={40} strokeWidth={1.2} />
              <p className="imageLibraryEmptyTitle">这个栏目还没有图片</p>
              <p>点击右上角「上传图片」开始添加</p>
            </div>
          )}
          {!loading && images.map((img, idx) => (
            <div
              key={img.id}
              id={`il-card-${img.id}`}
              className={`imageLibraryCard${highlightedImageId === img.id ? " imageLibraryCardHighlight" : ""}`}
            >
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
            <label>
              官网 URL（选填）
              <input type="url" value={catUrl} onChange={(e) => setCatUrl(e.target.value)} placeholder="https://example.com" />
            </label>
            <div className="modalActions">
              <button type="button" className="secondaryButton" onClick={() => setShowNewCat(false)}>取消</button>
              <button type="button" className="primaryButton" disabled={catSaving || !catName.trim() || !catBucket.trim()} onClick={handleCreateCategory}>
                {catSaving ? "创建中..." : "确认创建"}
              </button>
            </div>
          </div>
        </div>
      )}

      {editingCategory && (
        <div className="modalOverlay" onClick={() => setEditingCategory(null)}>
          <div className="modalCard" onClick={(e) => e.stopPropagation()}>
            <h2>编辑栏目</h2>
            <label>
              栏目名称
              <input value={editCatName} onChange={(e) => setEditCatName(e.target.value)} placeholder="如：原神" />
            </label>
            <label>
              归属
              <select value={editCatKind} onChange={(e) => setEditCatKind(e.target.value as "main" | "companion")}>
                <option value="main">主推游戏</option>
                <option value="companion">陪衬游戏</option>
              </select>
            </label>
            <label>
              Bucket 名称
              <input value={editingCategory.bucket_name} disabled />
            </label>
            <label>
              描述（选填）
              <input value={editCatDesc} onChange={(e) => setEditCatDesc(e.target.value)} placeholder="栏目说明" />
            </label>
            <label>
              官网 URL（选填）
              <input type="url" value={editCatUrl} onChange={(e) => setEditCatUrl(e.target.value)} placeholder="https://example.com" />
            </label>
            <div className="modalActions">
              <button type="button" className="secondaryButton" onClick={() => setEditingCategory(null)}>取消</button>
              <button type="button" className="primaryButton" disabled={editCatSaving || !editCatName.trim()} onClick={handleSaveCategoryEdit}>
                {editCatSaving ? "保存中..." : "保存"}
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
              <button type="button" className="secondaryButton" onClick={() => setShowUpload(false)} disabled={uploading}>取消</button>
              <button
                type="button"
                className="primaryButton"
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
              <button type="button" className="secondaryButton" onClick={() => setEditingImage(null)}>取消</button>
              <button type="button" className="primaryButton" disabled={editSaving} onClick={handleSaveEdit}>
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
