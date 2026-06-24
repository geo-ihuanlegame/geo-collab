import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDownAZ,
  Check,
  ChevronLeft,
  Clock,
  Folder,
  FolderPlus,
  Pencil,
  Search,
  SlidersHorizontal,
  Trash2,
  X,
} from "lucide-react";
import {
  createCategory,
  deleteCategory,
  deleteImage,
  listCategories,
  listImages,
  searchImages,
  updateImage,
  uploadImage,
} from "../../api/image-library";
import type { ImageSearchResult, StockCategory, StockImage } from "../../types";
import { Modal } from "../Modal";

const KIND_LABEL: Record<"main" | "companion", string> = {
  main: "主推游戏",
  companion: "陪衬游戏",
};

// 「筛选」=排序：名称 / 修改日期 各正倒序，共 4 项。修改日期取栏目 latest_image_at（空则 created_at）。
const SORT_OPTIONS = [
  { key: "name-asc", label: "名称 · 正序（A→Z）", dim: "name" },
  { key: "name-desc", label: "名称 · 倒序（Z→A）", dim: "name" },
  { key: "date-desc", label: "修改日期 · 倒序（新→旧）", dim: "date" },
  { key: "date-asc", label: "修改日期 · 正序（旧→新）", dim: "date" },
] as const;
type SortKey = (typeof SORT_OPTIONS)[number]["key"];

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

  // 搜索 / 图片级管理（删除、改描述）状态
  const [query, setQuery] = useState("");
  const [searchResults, setSearchResults] = useState<ImageSearchResult[] | null>(null); // null = 非搜索态
  const [searching, setSearching] = useState(false);
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editDescVal, setEditDescVal] = useState("");
  const [savingDesc, setSavingDesc] = useState(false);
  // 从搜索结果点选跨类别文件夹时：先切 kind，待该类 folders 加载后再选中
  const [pendingFolderId, setPendingFolderId] = useState<number | null>(null);

  // 排序（前端 UI 上叫「筛选」）：作用于文件夹网格、搜索命中栏目、文件夹内图片
  const [sort, setSort] = useState<SortKey>("name-asc");
  const [sortOpen, setSortOpen] = useState(false);
  const sortRef = useRef<HTMLDivElement>(null);

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

  // 搜索结果里点了别的类别的文件夹：切 kind 触发 folders 重载，到位后把它设为当前文件夹（=保存目标）
  useEffect(() => {
    if (pendingFolderId == null) return;
    const f = folders.find((x) => x.id === pendingFolderId);
    if (f) {
      setCurrentFolder(f);
      setPendingFolderId(null);
    }
  }, [folders, pendingFolderId]);

  // 点空白处关闭「筛选」下拉
  useEffect(() => {
    if (!sortOpen) return;
    function onDown(e: MouseEvent) {
      if (sortRef.current && !sortRef.current.contains(e.target as Node)) setSortOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [sortOpen]);

  // 全库跨栏目搜索（debounce 300ms），复用图片库 /search 接口
  useEffect(() => {
    const q = query.trim();
    if (!q) {
      setSearchResults(null);
      setSearching(false);
      return;
    }
    setSearching(true);
    let cancelled = false;
    const timer = setTimeout(() => {
      searchImages(q, 200)
        .then((data) => {
          if (!cancelled) setSearchResults(data);
        })
        .catch(() => {
          if (!cancelled) setSearchResults([]);
        })
        .finally(() => {
          if (!cancelled) setSearching(false);
        });
    }, 300);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [query]);

  // 搜索命中 → 去重出「所属栏目（文件夹）」列表，仍以文件夹形式展示，点一下即选中为保存目标。
  // 来源：① /search 命中图片所属栏目（带匹配张数）② 当前类别下名字命中 query 的文件夹（兜住空文件夹）。
  const matchedFolders = useMemo(() => {
    const map = new Map<
      number,
      { id: number; name: string; kind: "main" | "companion"; count: number }
    >();
    for (const r of searchResults ?? []) {
      const g = map.get(r.category_id) ?? {
        id: r.category_id,
        name: r.category_name,
        kind: r.kind,
        count: 0,
      };
      g.count += 1;
      map.set(r.category_id, g);
    }
    const q = query.trim().toLowerCase();
    if (q) {
      for (const f of folders) {
        if (!map.has(f.id) && f.name.toLowerCase().includes(q)) {
          map.set(f.id, { id: f.id, name: f.name, kind: f.kind, count: 0 });
        }
      }
    }
    const arr = Array.from(map.values());
    if (sort.startsWith("name")) {
      const sign = sort.endsWith("asc") ? 1 : -1;
      arr.sort((a, b) => a.name.localeCompare(b.name, "zh") * sign);
    } else {
      // 搜索结果无时间戳，「修改日期」退化为相关度（匹配张数）降序、其次名称
      arr.sort((a, b) => b.count - a.count || a.name.localeCompare(b.name, "zh"));
    }
    return arr;
  }, [searchResults, folders, query, sort]);

  // 文件夹网格按当前排序：名称用中文 localeCompare，修改日期用 latest_image_at（空则 created_at）
  const sortedFolders = useMemo(() => {
    const sign = sort.endsWith("asc") ? 1 : -1;
    const byName = sort.startsWith("name");
    return [...folders].sort((a, b) => {
      const r = byName
        ? a.name.localeCompare(b.name, "zh")
        : (a.latest_image_at ?? a.created_at).localeCompare(b.latest_image_at ?? b.created_at);
      return r * sign;
    });
  }, [folders, sort]);

  // 文件夹内图片同样套用排序：名称→filename，修改日期→created_at
  const sortedImages = useMemo(() => {
    const sign = sort.endsWith("asc") ? 1 : -1;
    const byName = sort.startsWith("name");
    return [...images].sort((a, b) => {
      const r = byName
        ? a.filename.localeCompare(b.filename, "zh")
        : a.created_at.localeCompare(b.created_at);
      return r * sign;
    });
  }, [images, sort]);

  async function refreshFolders(selectId?: number) {
    const data = await listCategories(kind);
    setFolders(data);
    if (selectId != null) {
      setCurrentFolder(data.find((f) => f.id === selectId) ?? null);
    }
  }

  // 从搜索结果点选一个栏目：进入它 = 设为保存目标，并退出搜索态。
  // 跨类别（命中的是另一类的文件夹）则先切 kind，等 folders 重载后由 pendingFolderId effect 选中。
  function openFolderFromSearch(f: { id: number; name: string; kind: "main" | "companion" }) {
    setQuery("");
    setSearchResults(null);
    setSearching(false);
    setCreating(false);
    if (f.kind !== kind) {
      setPendingFolderId(f.id);
      setKind(f.kind);
      return;
    }
    const real = folders.find((x) => x.id === f.id);
    setCurrentFolder(
      real ?? {
        id: f.id,
        name: f.name,
        kind: f.kind,
        bucket_name: "",
        description: null,
        official_url: null,
        created_at: "",
        latest_image_at: null,
      },
    );
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

  // 删除图片（文件夹态 / 搜索态共用，只需 id）。后端是物理删，需二次确认。
  async function handleDeleteImage(img: { id: number; filename: string }) {
    if (
      !window.confirm(`确定删除图片「${img.filename}」？删除后引用它的文章会显示裂图，且无法恢复。`)
    ) {
      return;
    }
    setDeletingId(img.id);
    setError(null);
    try {
      await deleteImage(img.id);
      setImages((prev) => prev.filter((i) => i.id !== img.id));
      onSaved("已删除图片");
    } catch (e) {
      const msg = e instanceof Error ? e.message : "删除失败";
      setError(msg);
      onError?.(msg);
    } finally {
      setDeletingId(null);
    }
  }

  function startEditDesc(id: number, current: string) {
    setEditingId(id);
    setEditDescVal(current);
  }

  async function handleSaveDesc(id: number) {
    setSavingDesc(true);
    setError(null);
    try {
      const updated = await updateImage(id, { description: editDescVal.trim() || null });
      setImages((prev) =>
        prev.map((i) => (i.id === id ? { ...i, description: updated.description } : i)),
      );
      setEditingId(null);
      onSaved("描述已更新");
    } catch (e) {
      const msg = e instanceof Error ? e.message : "更新失败";
      setError(msg);
      onError?.(msg);
    } finally {
      setSavingDesc(false);
    }
  }

  // 文件夹内的图片卡片：hover 浮出删除 + 编辑描述。
  function renderImageCard(card: {
    id: number;
    url: string;
    filename: string;
    description?: string | null;
  }) {
    const isEditing = editingId === card.id;
    const busy = deletingId === card.id;
    return (
      <div key={card.id} className="imgSaveImgCard">
        <div className="imgSaveImgThumb">
          <img src={card.url} alt={card.filename} loading="lazy" />
          <div className="imgSaveImgOps">
            <button
              type="button"
              title="编辑描述"
              disabled={busy}
              onClick={() => startEditDesc(card.id, card.description ?? "")}
            >
              <Pencil size={13} />
            </button>
            <button
              type="button"
              className="danger"
              title="删除图片"
              disabled={busy}
              onClick={() => void handleDeleteImage({ id: card.id, filename: card.filename })}
            >
              <Trash2 size={13} />
            </button>
          </div>
        </div>
        {isEditing ? (
          <div className="imgSaveDescEdit">
            <input
              autoFocus
              value={editDescVal}
              placeholder="图片描述"
              disabled={savingDesc}
              onChange={(e) => setEditDescVal(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void handleSaveDesc(card.id);
                if (e.key === "Escape") setEditingId(null);
              }}
            />
            <button
              type="button"
              className="imgSaveDescBtn"
              title="保存"
              disabled={savingDesc}
              onClick={() => void handleSaveDesc(card.id)}
            >
              <Check size={14} />
            </button>
            <button
              type="button"
              className="imgSaveDescBtn"
              title="取消"
              disabled={savingDesc}
              onClick={() => setEditingId(null)}
            >
              <X size={14} />
            </button>
          </div>
        ) : (
          <span className="imgSaveImgName" title={card.filename}>
            {card.filename}
          </span>
        )}
      </div>
    );
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
            <div className="imgSaveSearchBar">
              <Search size={15} className="imgSaveSearchIcon" />
              <input
                value={query}
                placeholder="在图片库中搜索"
                title="搜索栏目，或按图片文件名 / 描述 / 标签定位栏目（全库）"
                onChange={(e) => setQuery(e.target.value)}
              />
              {query && (
                <button
                  type="button"
                  className="imgSaveSearchClear"
                  title="清空搜索"
                  onClick={() => setQuery("")}
                >
                  <X size={14} />
                </button>
              )}
            </div>
            <div className="imgSaveSortWrap" ref={sortRef}>
              <button
                type="button"
                className={`imgSaveSortBtn${sortOpen ? " active" : ""}`}
                onClick={() => setSortOpen((v) => !v)}
                title="排序方式"
              >
                <SlidersHorizontal size={14} /> 筛选
              </button>
              {sortOpen && (
                <div className="imgSaveSortMenu" role="menu">
                  {SORT_OPTIONS.map((o) => (
                    <button
                      key={o.key}
                      type="button"
                      role="menuitemradio"
                      aria-checked={sort === o.key}
                      className={`imgSaveSortItem${sort === o.key ? " active" : ""}`}
                      onClick={() => {
                        setSort(o.key);
                        setSortOpen(false);
                      }}
                    >
                      {o.dim === "name" ? <ArrowDownAZ size={14} /> : <Clock size={14} />}
                      <span>{o.label}</span>
                      {sort === o.key && <Check size={14} className="imgSaveSortCheck" />}
                    </button>
                  ))}
                </div>
              )}
            </div>
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
            {searchResults != null ? (
              searching ? (
                <p className="emptyText">搜索中…</p>
              ) : matchedFolders.length === 0 ? (
                <p className="emptyText">没有匹配「{query.trim()}」的栏目或图片</p>
              ) : (
                matchedFolders.map((f) => (
                  <button
                    key={f.id}
                    type="button"
                    className="imgSaveFolderCard"
                    onClick={() => openFolderFromSearch(f)}
                  >
                    <Folder size={40} strokeWidth={1.3} />
                    <span className="imgSaveFolderName">{f.name}</span>
                    <span className="imgSaveFolderMeta">
                      {KIND_LABEL[f.kind]}
                      {f.count > 0 ? ` · ${f.count} 张匹配` : ""}
                    </span>
                  </button>
                ))
              )
            ) : currentFolder == null ? (
              sortedFolders.length === 0 ? (
                <p className="emptyText">该类别下暂无文件夹，点「新建文件夹」开始</p>
              ) : (
                sortedFolders.map((f) => (
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
            ) : sortedImages.length === 0 ? (
              <p className="emptyText">这个文件夹还没有图片</p>
            ) : (
              sortedImages.map((img) =>
                renderImageCard({
                  id: img.id,
                  url: img.url,
                  filename: img.filename,
                  description: img.description,
                }),
              )
            )}
          </div>
        </div>

        {error ? <p className="imageSaveError">{error}</p> : null}
      </div>
    </Modal>
  );
}
