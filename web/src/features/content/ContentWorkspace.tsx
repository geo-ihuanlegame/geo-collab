import { useEffect, useMemo, useRef, useState, type MutableRefObject } from "react";
import { EditorContent, NodeViewWrapper, ReactNodeViewRenderer, useEditor } from "@tiptap/react";
import type { NodeViewProps } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import Image from "@tiptap/extension-image";
import Link from "@tiptap/extension-link";
import { TextStyle } from "@tiptap/extension-text-style";
import Color from "@tiptap/extension-color";
import Highlight from "@tiptap/extension-highlight";
import Underline from "@tiptap/extension-underline";
import TextAlign from "@tiptap/extension-text-align";
import { Plus, Save, Search, Trash2, Upload, ChevronRight, Check, Send, ShieldCheck, ListChecks } from "lucide-react";
import { useToast } from "../../components/Toast";
import {
  approveArticle,
  approveGroup,
  createArticle,
  createArticleGroup,
  deleteArticle,
  deleteArticleGroup,
  getArticle,
  listArticleGroups,
  listArticles,
  revokeArticleApproval,
  updateArticle,
  updateArticleCover,
  updateArticleGroup,
  updateArticleGroupItems,
} from "../../api/articles";
import { listAccounts } from "../../api/accounts";
import { uploadAsset as uploadAssetRequest } from "../../api/assets";
import { assetSrc, assetThumbSrc, countWords, emptyDoc, newClientRequestId, singleFlight, withAssetToken } from "../../api/core";
import type { Account, Article, ArticleCreatePayload, ArticleGroup, ArticleGroupUpdateItemsPayload, ArticleSummary, ArticleUpdatePayload, Draft, ReviewStatus } from "../../types";
import { formatDateTime } from "../../utils/dateFormat";
import { EditorToolbar } from "../../components/editor/EditorToolbar";
import { ImageSaveDialog } from "../../components/editor/ImageSaveDialog";
import { ArticleListItem, ReviewBadge } from "../../components/ArticleListItem";
import { Modal } from "../../components/Modal";
import { Pagination } from "../../components/Pagination";
import { DistributeModal, type DistributeTarget } from "./DistributeModal";

function makeEmptyDraft(): Draft {
  return {
    id: null,
    title: "",
    author: "",
    cover_asset_id: null,
    status: "draft",
    version: null,
    stock_category_ids: [],
  };
}

type EditorDoc = Record<string, unknown>;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function extractLocalAssetId(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const marker = "/api/assets/";
  const markerIndex = value.indexOf(marker);
  if (markerIndex < 0) return null;
  const tail = value.slice(markerIndex + marker.length);
  const assetId = tail.split(/[?#/]/)[0];
  return assetId || null;
}

function extractStockImageId(value: unknown): number | null {
  if (typeof value !== "string") return null;
  const match = value.match(/\/api\/stock-images\/(\d+)\/file/);
  return match ? Number(match[1]) : null;
}

function normalizeEditorDocument(value: unknown, mode: "display" | "save"): EditorDoc {
  const normalized = normalizeEditorNode(value, mode);
  const doc = isRecord(normalized) ? normalized : { ...emptyDoc };
  return isEmptyEditorDocument(doc) ? { ...emptyDoc } : doc;
}

function normalizeEditorNode(value: unknown, mode: "display" | "save"): unknown {
  if (Array.isArray(value)) {
    return value.map((item) => normalizeEditorNode(item, mode));
  }
  if (!isRecord(value)) {
    return value;
  }

  const next: Record<string, unknown> = {};
  for (const [key, child] of Object.entries(value)) {
    if (key === "content") {
      next.content = Array.isArray(child) ? child.map((item) => normalizeEditorNode(item, mode)) : child;
    } else if (key === "attrs") {
      const attrs = cleanEditorAttrs(child);
      if (Object.keys(attrs).length > 0) next.attrs = attrs;
    } else {
      next[key] = child;
    }
  }

  if (next.type === "image") {
    const attrs = isRecord(next.attrs) ? { ...next.attrs } : {};
    const assetId =
      (typeof attrs.assetId === "string" && attrs.assetId) ||
      (typeof attrs.asset_id === "string" && attrs.asset_id) ||
      (typeof attrs.dataAssetId === "string" && attrs.dataAssetId) ||
      extractLocalAssetId(attrs.src);
    if (assetId) {
      attrs.assetId = assetId;
      attrs.src = mode === "display" ? assetSrc(assetId) ?? `/api/assets/${assetId}` : `/api/assets/${assetId}`;
      next.attrs = attrs;
    } else {
      const stockImageId =
        (typeof attrs.stockImageId === "number" && attrs.stockImageId) ||
        (typeof attrs.stockImageId === "string" && Number(attrs.stockImageId)) ||
        (typeof attrs.stock_image_id === "number" && attrs.stock_image_id) ||
        (typeof attrs.stock_image_id === "string" && Number(attrs.stock_image_id)) ||
        extractStockImageId(attrs.src);
      if (stockImageId) {
        attrs.stockImageId = stockImageId;
        attrs.src = `/api/stock-images/${stockImageId}/file`;
        next.attrs = attrs;
      }
    }
  }

  return next;
}

function cleanEditorAttrs(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) return {};
  return Object.entries(value).reduce<Record<string, unknown>>((acc, [key, child]) => {
    if (child !== null && child !== undefined && child !== "") acc[key] = child;
    return acc;
  }, {});
}

function isEmptyEditorDocument(value: EditorDoc): boolean {
  if (value.type !== "doc") return false;
  const content = value.content;
  if (!Array.isArray(content) || content.length === 0) return true;
  return content.every((node) => {
    if (!isRecord(node) || node.type !== "paragraph") return false;
    const paragraphContent = node.content;
    const attrs = isRecord(node.attrs) ? node.attrs : {};
    return (!Array.isArray(paragraphContent) || paragraphContent.length === 0) && Object.keys(attrs).length === 0;
  });
}

function editorBodyState(editor: { getJSON: () => unknown }): string {
  return stableStringify(normalizeEditorDocument(editor.getJSON(), "save"));
}

function cleanLocalAssetUrlsInHtml(html: string): string {
  return html.replace(/(src=["'])\/api\/assets\/([^"'?/]+)(?:\?[^"']*)?(["'])/g, "$1/api/assets/$2$3");
}

function stableStringify(value: unknown): string {
  return JSON.stringify(sortJsonValue(value));
}

function sortJsonValue(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(sortJsonValue);
  }
  if (!isRecord(value)) {
    return value;
  }
  return Object.keys(value).sort().reduce<Record<string, unknown>>((acc, key) => {
    acc[key] = sortJsonValue(value[key]);
    return acc;
  }, {});
}

const EMPTY_BODY_STATE = stableStringify(normalizeEditorDocument(emptyDoc, "save"));

const CustomTextStyle = TextStyle.extend({
  addAttributes() {
    return {
      ...this.parent?.(),
      fontSize: {
        default: null,
        parseHTML: (el: HTMLElement) => el.style.fontSize || null,
        renderHTML: (attrs: Record<string, unknown>) =>
          attrs.fontSize ? { style: `font-size: ${attrs.fontSize}` } : {},
      },
    };
  },
});

function ImageResizeView({ node, updateAttributes, selected }: NodeViewProps) {
  const attrs = node.attrs as {
    src: string;
    alt: string;
    title: string;
    assetId: string | null;
    stockImageId: number | null;
    width: string;
    progress: number | null;
  };
  const imgRef = useRef<HTMLImageElement>(null);
  const cleanupRef = useRef<(() => void) | null>(null);
  const [imgError, setImgError] = useState(false);

  const isPending = typeof attrs.assetId === "string" && attrs.assetId.startsWith("pending-");

  useEffect(() => {
    setImgError(false);
  }, [attrs.src]);

  useEffect(() => {
    return () => {
      cleanupRef.current?.();
    };
  }, []);

  function startResize(e: React.MouseEvent) {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = imgRef.current?.offsetWidth ?? 300;
    const containerWidth = imgRef.current?.parentElement?.offsetWidth || 600;

    function onMove(ev: MouseEvent) {
      const pct = Math.min(
        100,
        Math.max(10, Math.round(((startWidth + ev.clientX - startX) / containerWidth) * 100)),
      );
      updateAttributes({ width: `${pct}%` });
    }
    function onUp() {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      cleanupRef.current = null;
    }
    cleanupRef.current = () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }

  return (
    <NodeViewWrapper style={{ display: "block", position: "relative", width: attrs.width ?? "100%" }}>
      {isPending && imgError ? (
        <div className="imgUploadingPlaceholder">
          <span>{attrs.progress != null ? `上传中 ${attrs.progress}%` : "上传中…"}</span>
          {attrs.progress != null && (
            <div className="imgUploadProgress">
              <div className="imgUploadProgressBar" style={{ width: `${attrs.progress}%` }} />
            </div>
          )}
        </div>
      ) : (
        <img
          ref={imgRef}
          src={attrs.src}
          alt={attrs.alt ?? ""}
          title={attrs.title ?? ""}
          data-asset-id={attrs.assetId ?? undefined}
          data-stock-image-id={attrs.stockImageId ?? undefined}
          style={{ width: "100%", display: "block", borderRadius: "var(--r)" }}
          draggable={false}
          onError={() => { if (isPending) setImgError(true); }}
          onLoad={() => setImgError(false)}
        />
      )}
      {selected && <div className="imgResizeHandle" onMouseDown={startResize} />}
    </NodeViewWrapper>
  );
}

const CustomImage = Image.extend({
  addAttributes() {
    return {
      ...this.parent?.(),
      assetId: {
        default: null,
        parseHTML: (el) => el.getAttribute("data-asset-id"),
        renderHTML: (attrs) => (attrs.assetId ? { "data-asset-id": attrs.assetId } : {}),
      },
      stockImageId: {
        default: null,
        parseHTML: (el) => {
          const value = el.getAttribute("data-stock-image-id");
          return value ? Number(value) : null;
        },
        renderHTML: (attrs) => (attrs.stockImageId ? { "data-stock-image-id": attrs.stockImageId } : {}),
      },
      width: {
        default: "100%",
        parseHTML: (el) => el.style.width || "100%",
        renderHTML: (attrs) => ({ style: `width: ${attrs.width ?? "100%"}` }),
      },
      progress: {
        default: null,
        parseHTML: () => null,
        renderHTML: () => ({}),
      },
    };
  },
  addNodeView() {
    return ReactNodeViewRenderer(ImageResizeView);
  },
});

const LIST_PAGE_SIZE = 10;
const ARTICLE_FETCH_LIMIT = 200;

type UnifiedListItem =
  | { type: "article"; article: ArticleSummary; sortTime: number }
  | { type: "group"; group: ArticleGroup; sortTime: number };

interface Props {
  dirtyCheckRef?: MutableRefObject<() => boolean>;
  isActive?: boolean;
}

export function ContentWorkspace({ dirtyCheckRef, isActive }: Props = {}) {
  const { toast } = useToast();
  const [articles, setArticles] = useState<ArticleSummary[]>([]);
  const [groups, setGroups] = useState<ArticleGroup[]>([]);
  const [selectedArticle, setSelectedArticle] = useState<Article | null>(null);
  const [selectedArticleIds, setSelectedArticleIds] = useState<number[]>([]);
  const [query, setQuery] = useState("");
  const [articlePage, setArticlePage] = useState(0);
  const [draft, setDraft] = useState<Draft>(makeEmptyDraft);
  const [loading, setLoading] = useState(false);
  const [statusText, setStatusText] = useState("");
  const [pendingCoverUrl, setPendingCoverUrl] = useState<string | null>(null);
  const [groupName, setGroupName] = useState("");
  const [editingGroupId, setEditingGroupId] = useState<number | null>(null);
  const [expandedGroupIds, setExpandedGroupIds] = useState<Set<number>>(new Set());
  const [groupPickerArticle, setGroupPickerArticle] = useState<ArticleSummary | null>(null);
  const [saveImageSrc, setSaveImageSrc] = useState<string | null>(null);
  const [groupPickerSelectedId, setGroupPickerSelectedId] = useState<number | null>(null);
  const [confirmDeleteArticle, setConfirmDeleteArticle] = useState(false);
  const [confirmDeleteGroup, setConfirmDeleteGroup] = useState(false);
  const [confirmUnsavedNew, setConfirmUnsavedNew] = useState(false);
  const [reviewTab, setReviewTab] = useState<ReviewStatus>("pending");
  const [reviewBusyId, setReviewBusyId] = useState<number | null>(null);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [distributeTarget, setDistributeTarget] = useState<DistributeTarget | null>(null);

  const pasteImageRef = useRef<(file: File) => void>(() => {});
  const isInitialMountRef = useRef(true);
  const [charCount, setCharCount] = useState(0);
  const [imageUploading, setImageUploading] = useState(0);
  const pendingBlobsRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    const blobs = pendingBlobsRef.current;
    return () => { blobs.forEach(URL.revokeObjectURL); };
  }, []);

  const editor = useEditor({
    extensions: [
      StarterKit,
      Link.configure({ openOnClick: false }),
      CustomImage.configure({ allowBase64: false }),
      CustomTextStyle,
      Color,
      Highlight.configure({ multicolor: true }),
      Underline,
      TextAlign.configure({ types: ["heading", "paragraph"] }),
    ],
    content: emptyDoc,
    onUpdate({ editor }) {
      setCharCount(editor.getText().replace(/\s/g, "").length);
    },
    editorProps: {
      attributes: {
        class: "editorSurface",
      },
      transformPastedHTML(html) {
        return html.replace(/ style="[^"]*"/gi, "");
      },
      handlePaste(_, event) {
        const items = Array.from(event.clipboardData?.items ?? []);
        const imageItem = items.find((item) => item.type.startsWith("image/"));
        if (!imageItem) return false;
        const file = imageItem.getAsFile();
        if (!file) return false;
        pasteImageRef.current(file);
        return true;
      },
    },
  });

  const latestDraft = useRef(draft);
  latestDraft.current = draft;
  const latestEditor = useRef(editor);
  latestEditor.current = editor;
  const savedStateRef = useRef<{ title: string; author: string; cover_asset_id: number | string | null; bodyState: string } | null>(null);

  if (dirtyCheckRef) {
    dirtyCheckRef.current = () => {
      const d = latestDraft.current;
      const e = latestEditor.current;
      const s = savedStateRef.current;
      const currentBodyState = e ? editorBodyState(e) : EMPTY_BODY_STATE;
      if (!s) {
        return d.title.trim() !== "" || d.author.trim() !== "" || d.cover_asset_id !== null || currentBodyState !== EMPTY_BODY_STATE;
      }
      return (
        d.title.trim() !== s.title ||
        d.author.trim() !== s.author ||
        d.cover_asset_id !== s.cover_asset_id ||
        currentBodyState !== s.bodyState
      );
    };
  }

  const groupedArticleIdSet = useMemo(() => {
    const ids = new Set<number>();
    for (const g of groups) for (const item of g.items) ids.add(item.article_id);
    return ids;
  }, [groups]);

  const articleById = useMemo(() => Object.fromEntries(articles.map((a) => [a.id, a])), [articles]);

  function groupReviewCounts(group: ArticleGroup): { total: number; approved: number } {
    if (group.review_summary) return group.review_summary;
    // Fallback: derive from loaded member articles when the backend omits the summary.
    let total = 0;
    let approved = 0;
    for (const item of group.items) {
      const article = articleById[item.article_id];
      if (!article) continue;
      total += 1;
      if (article.review_status === "approved") approved += 1;
    }
    return { total, approved };
  }

  // 组在某标签是否可见：有该状态成员就出现（混合组两个标签都在）。空组(total=0)算 pending。
  function groupHasStatus(group: ArticleGroup, status: ReviewStatus): boolean {
    const counts = groupReviewCounts(group);
    if (counts.total === 0) return status === "pending";
    const pendingCount = counts.total - counts.approved;
    return status === "approved" ? counts.approved > 0 : pendingCount > 0;
  }

  // Tab counts: standalone (ungrouped) articles + groups, split by (derived) review status.
  const reviewCounts = useMemo(() => {
    let pending = 0;
    let approved = 0;
    for (const article of articles) {
      if (groupedArticleIdSet.has(article.id)) continue;
      if (article.review_status === "approved") approved += 1;
      else pending += 1;
    }
    for (const group of groups) {
      if (groupHasStatus(group, "pending")) pending += 1;
      if (groupHasStatus(group, "approved")) approved += 1;
    }
    return { pending, approved };
  }, [articles, groups, groupedArticleIdSet, articleById]);

  const unifiedList = useMemo(() => {
    const items: UnifiedListItem[] = [];
    for (const article of articles) {
      if (groupedArticleIdSet.has(article.id)) continue;
      if (article.review_status !== reviewTab) continue;
      items.push({ type: "article", article, sortTime: new Date(article.created_at).getTime() });
    }
    // 混合组双标签可见：当前标签有对应状态成员就纳入。
    for (const group of groups) {
      if (!groupHasStatus(group, reviewTab)) continue;
      if (!query || group.name.toLowerCase().includes(query.toLowerCase()) || group.items.some((item) => articleById[item.article_id])) {
        items.push({ type: "group", group, sortTime: new Date(group.created_at).getTime() });
      }
    }
    return items.sort((a, b) => b.sortTime - a.sortTime);
  }, [articles, groups, groupedArticleIdSet, query, reviewTab, articleById]);

  const totalArticlePages = Math.max(1, Math.ceil(unifiedList.length / LIST_PAGE_SIZE));
  const pagedUnifiedList = unifiedList.slice(articlePage * LIST_PAGE_SIZE, (articlePage + 1) * LIST_PAGE_SIZE);

  useEffect(() => {
    if (articlePage >= totalArticlePages) {
      setArticlePage(totalArticlePages - 1);
    }
  }, [articlePage, totalArticlePages]);

  async function refreshArticles(nextQuery = query, nextPage = articlePage) {
    try {
      const allArticles: ArticleSummary[] = [];
      for (let skip = 0; ; skip += ARTICLE_FETCH_LIMIT) {
        const params = new URLSearchParams({ skip: String(skip), limit: String(ARTICLE_FETCH_LIMIT) });
        if (nextQuery) params.set("q", nextQuery);
        const batch = await listArticles(params);
        allArticles.push(...batch);
        if (batch.length < ARTICLE_FETCH_LIMIT) break;
      }
      setArticles(allArticles);
      setArticlePage(nextPage);
    } catch {
      toast("加载文章列表失败", "error");
    }
  }

  async function refreshGroups() {
    try {
      const data = await listArticleGroups();
      setGroups(data);
    } catch {
      toast("加载分组列表失败", "error");
    }
  }

  useEffect(() => {
    void refreshArticles();
    void refreshGroups();
    listAccounts().then(setAccounts).catch(() => {});
  }, []);

  useEffect(() => {
    if (isInitialMountRef.current) {
      isInitialMountRef.current = false;
      return;
    }
    if (!isActive) return;
    void refreshArticles();
    void refreshGroups();
  }, [isActive]);

  function resetDraft() {
    setDraft(makeEmptyDraft());
    setSelectedArticle(null);
    setPendingCoverUrl((url) => { if (url) URL.revokeObjectURL(url); return null; });
    editor?.commands.setContent(emptyDoc);
    setSelectedArticleIds([]);
    savedStateRef.current = null;
  }

  async function loadArticle(article: ArticleSummary) {
    setPendingCoverUrl((url) => { if (url) URL.revokeObjectURL(url); return null; });
    // Pre-populate title/metadata immediately from the list summary (no API wait)
    setDraft({
      id: article.id,
      title: article.title,
      author: article.author ?? "",
      cover_asset_id: article.cover_asset_id,
      status: article.status,
      version: article.version,
      stock_category_ids: [],
    });
    setLoading(true);
    setStatusText("加载中");
    try {
      const detail = await getArticle(article.id);
      setSelectedArticle(detail);
      setDraft({
        id: detail.id,
        title: detail.title,
        author: detail.author ?? "",
        cover_asset_id: detail.cover_asset_id,
        status: detail.status,
        version: detail.version,
        stock_category_ids: detail.stock_category_ids ?? [],
      });
      const displayDoc = normalizeEditorDocument(detail.content_json || emptyDoc, "display");
      editor?.commands.setContent(displayDoc);
      const bodyState = editor
        ? editorBodyState(editor)
        : stableStringify(normalizeEditorDocument(detail.content_json || emptyDoc, "save"));
      savedStateRef.current = {
        title: detail.title?.trim() ?? "",
        author: detail.author?.trim() ?? "",
        cover_asset_id: detail.cover_asset_id,
        bodyState,
      };
    } catch (error) {
      toast(error instanceof Error ? error.message : "加载文章失败", "error");
    } finally {
      setLoading(false);
      setStatusText("");
    }
  }

  function applySavedArticle(saved: Article, contentJson?: Record<string, unknown>) {
    setSelectedArticle(saved);
    setDraft({
      id: saved.id,
      title: saved.title,
      author: saved.author?.trim() ?? "",
      cover_asset_id: saved.cover_asset_id,
      status: saved.status,
      version: saved.version,
      stock_category_ids: saved.stock_category_ids ?? [],
    });
    savedStateRef.current = {
      title: saved.title?.trim() ?? "",
      author: saved.author ?? "",
      cover_asset_id: saved.cover_asset_id,
      bodyState: contentJson
        ? stableStringify(contentJson)
        : stableStringify(normalizeEditorDocument(saved.content_json || emptyDoc, "save")),
    };
  }

  async function persistArticle({ quiet = false }: { quiet?: boolean } = {}): Promise<Article | null> {
    if (!editor || !draft.title.trim()) {
      if (!quiet) toast("标题不能为空", "error");
      return null;
    }
    if (imageUploading > 0) {
      if (!quiet) toast("请等待图片上传完成再保存", "error");
      return null;
    }
    setLoading(true);
    setStatusText("保存中");
    try {
      const contentJson = normalizeEditorDocument(editor.getJSON(), "save");
      const base = {
        title: draft.title.trim(),
        author: draft.author.trim() || null,
        cover_asset_id: draft.cover_asset_id,
        content_json: contentJson,
        content_html: cleanLocalAssetUrlsInHtml(editor.getHTML()),
        plain_text: editor.getText(),
        word_count: countWords(editor.getText()),
        status: draft.status,
        version: draft.version,
        stock_category_ids: draft.stock_category_ids,
      };
      const articleId = draft.id;
      const saved = articleId
        ? await singleFlight(`article-save-${articleId}`, () => {
            const payload: ArticleUpdatePayload = { ...base };
            return updateArticle(articleId, payload);
          })
        : await singleFlight("article-create", () => {
            const payload: ArticleCreatePayload = { ...base, client_request_id: newClientRequestId("article") };
            return createArticle(payload);
          });
      if (!saved) return null;
      applySavedArticle(saved, contentJson);
      setArticles((prev) =>
        prev.some((a) => a.id === saved.id)
          ? prev.map((a) => (a.id === saved.id ? { ...a, ...saved, published_count: a.published_count } : a))
          : [saved, ...prev],
      );
      if (!quiet) toast("文章已保存", "success");
      return saved;
    } catch (error) {
      if (!quiet) toast(error instanceof Error ? error.message : "保存失败", "error");
      return null;
    } finally {
      setLoading(false);
      setStatusText("");
    }
  }

  async function handleCoverUpload(file: File | null) {
    if (!file) return;
    const blobUrl = URL.createObjectURL(file);
    setPendingCoverUrl(blobUrl);
    setImageUploading((n) => n + 1);
    try {
      const asset = await uploadAssetRequest(file);
      setPendingCoverUrl(null);
      URL.revokeObjectURL(blobUrl);
      setDraft((current) => ({ ...current, cover_asset_id: asset.id }));
      if (draft.id) {
        const saved = await updateArticleCover(draft.id, { cover_asset_id: asset.id, version: draft.version });
        setSelectedArticle(saved);
        setDraft((current) => ({ ...current, cover_asset_id: saved.cover_asset_id, version: saved.version }));
        if (savedStateRef.current) {
          savedStateRef.current = { ...savedStateRef.current, cover_asset_id: saved.cover_asset_id };
        }
        setArticles((prev) =>
          prev.map((a) => (a.id === saved.id ? { ...a, cover_asset_id: saved.cover_asset_id, version: saved.version } : a)),
        );
        toast("封面已上传并保存", "success");
      } else {
        toast("封面已上传，保存文章后生效", "success");
      }
    } catch (error) {
      setPendingCoverUrl(null);
      URL.revokeObjectURL(blobUrl);
      toast(error instanceof Error ? error.message : "封面上传失败", "error");
    } finally {
      setImageUploading((n) => n - 1);
    }
  }

  async function handleBodyImageUploadSingle(file: File) {
    if (!editor) return;

    const blobUrl = URL.createObjectURL(file);
    const tempAssetId = `pending-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    pendingBlobsRef.current.add(blobUrl);
    editor.chain().focus().insertContent({
      type: "image",
      attrs: { src: blobUrl, alt: file.name, title: file.name, assetId: tempAssetId },
    }).run();

    setImageUploading((n) => n + 1);
    try {
      function updateNodeProgress(percent: number) {
        const { state, view } = editor!;
        let tr = state.tr;
        state.doc.descendants((node, pos) => {
          if (node.type.name === "image" && node.attrs.assetId === tempAssetId) {
            tr = tr.setNodeMarkup(pos, undefined, { ...node.attrs, progress: percent });
            return false;
          }
        });
        if (tr.docChanged) view.dispatch(tr);
      }

      const asset = await uploadAssetRequest(file, updateNodeProgress);
      const realSrc = withAssetToken(asset.url);

      const { state, view } = editor;
      let tr = state.tr;
      state.doc.descendants((node, pos) => {
        if (node.type.name === "image" && node.attrs.assetId === tempAssetId) {
          tr = tr.setNodeMarkup(pos, undefined, { ...node.attrs, src: realSrc, assetId: asset.id, progress: null });
          return false;
        }
      });
      if (tr.docChanged) view.dispatch(tr);

      pendingBlobsRef.current.delete(blobUrl);
      URL.revokeObjectURL(blobUrl);
    } catch (error) {
      const { state, view } = editor;
      let posFound = -1, sizeFound = 0;
      state.doc.descendants((node, pos) => {
        if (node.type.name === "image" && node.attrs.assetId === tempAssetId) {
          posFound = pos; sizeFound = node.nodeSize; return false;
        }
      });
      if (posFound >= 0) view.dispatch(state.tr.delete(posFound, posFound + sizeFound));
      pendingBlobsRef.current.delete(blobUrl);
      URL.revokeObjectURL(blobUrl);
      toast(error instanceof Error ? error.message : "正文图片上传失败", "error");
    } finally {
      setImageUploading((n) => n - 1);
    }
  }

  async function handleBodyImageUpload(files: File[]) {
    if (!files.length || !editor) return;
    await Promise.all(files.map((f) => handleBodyImageUploadSingle(f).catch(() => {})));
    toast("正文图片已插入，请保存文章", "success");
  }
  pasteImageRef.current = (file: File) => void handleBodyImageUploadSingle(file);

  async function saveArticle() {
    await persistArticle();
  }

  async function deleteCurrentArticle() {
    if (!draft.id) return;
    setLoading(true);
    setStatusText("删除中");
    try {
      const deletedId = draft.id;
      await deleteArticle(deletedId);
      resetDraft();
      setArticles((prev) => prev.filter((a) => a.id !== deletedId));
      toast("文章已删除", "success");
    } catch (error) {
      toast(error instanceof Error ? error.message : "删除失败", "error");
    } finally {
      setLoading(false);
      setStatusText("");
    }
  }

  async function saveGroupFromSelection() {
    const name = groupName.trim();
    if (!name) {
      toast("请输入分组名称", "error");
      return;
    }
    setLoading(true);
    try {
      let group = editingGroupId
        ? await updateArticleGroup(editingGroupId, { name, version: groups.find((item) => item.id === editingGroupId)?.version })
        : await createArticleGroup({ name });
      if (!editingGroupId && selectedArticleIds.length > 0) {
        const payload: ArticleGroupUpdateItemsPayload = {
          items: selectedArticleIds.map((articleId, index) => ({ article_id: articleId, sort_order: index })),
        };
        group = await updateArticleGroupItems(group.id, { ...payload, version: group.version });
      }
      const isEditing = Boolean(editingGroupId);
      setGroupName("");
      setEditingGroupId(null);
      setSelectedArticleIds([]);
      setGroups((prev) => (isEditing ? prev.map((g) => (g.id === group.id ? group : g)) : [group, ...prev]));
      toast(isEditing ? "分组已更新" : "分组已创建", "success");
    } catch (error) {
      toast(error instanceof Error ? error.message : "保存分组失败", "error");
    } finally {
      setLoading(false);
    }
  }

  async function deleteEditingGroup() {
    if (!editingGroupId) return;
    setLoading(true);
    try {
      const deletedGroupId = editingGroupId;
      await deleteArticleGroup(deletedGroupId);
      setEditingGroupId(null);
      setGroupName("");
      setSelectedArticleIds([]);
      setGroups((prev) => prev.filter((g) => g.id !== deletedGroupId));
      toast("分组已删除", "success");
    } catch (error) {
      toast(error instanceof Error ? error.message : "删除分组失败", "error");
    } finally {
      setLoading(false);
    }
  }

  function loadGroup(group: ArticleGroup) {
    setEditingGroupId(group.id);
    setGroupName(group.name);
    setSelectedArticleIds([]);
  }

  function toggleSelectedArticle(articleId: number) {
    setSelectedArticleIds((current) =>
      current.includes(articleId) ? current.filter((id) => id !== articleId) : [...current, articleId],
    );
  }

  async function searchArticles() {
    setArticlePage(0);
    await refreshArticles(query, 0);
  }

  async function addArticleToGroup() {
    if (!groupPickerArticle || !groupPickerSelectedId) return;
    const group = groups.find((item) => item.id === groupPickerSelectedId);
    if (!group) return;
    setLoading(true);
    try {
      const payload: ArticleGroupUpdateItemsPayload = {
        items: [
          ...group.items.map((item) => ({ article_id: item.article_id, sort_order: item.sort_order })),
          { article_id: groupPickerArticle.id, sort_order: group.items.length },
        ],
      };
      const updated = await updateArticleGroupItems(group.id, { ...payload, version: group.version });
      setGroupPickerArticle(null);
      setGroupPickerSelectedId(null);
      setGroups((prev) => prev.map((g) => (g.id === updated.id ? updated : g)));
      toast("文章已加入分组", "success");
    } catch (error) {
      toast(error instanceof Error ? error.message : "加入分组失败", "error");
    } finally {
      setLoading(false);
    }
  }

  async function removeArticleFromGroup(group: ArticleGroup, articleId: number) {
    setLoading(true);
    try {
      const payload: ArticleGroupUpdateItemsPayload = {
        items: group.items
          .filter((item) => item.article_id !== articleId)
          .map((item, index) => ({ article_id: item.article_id, sort_order: index })),
      };
      const updated = await updateArticleGroupItems(group.id, { ...payload, version: group.version });
      setGroups((prev) => prev.map((g) => (g.id === updated.id ? updated : g)));
      toast("文章已移出分组", "success");
    } catch (error) {
      toast(error instanceof Error ? error.message : "移出分组失败", "error");
    } finally {
      setLoading(false);
    }
  }

  async function changeArticlePage(nextPage: number) {
    setArticlePage(Math.min(Math.max(nextPage, 0), totalArticlePages - 1));
  }

  function applyReviewedArticle(updated: Article) {
    setArticles((prev) =>
      prev.map((a) => (a.id === updated.id ? { ...a, review_status: updated.review_status, version: updated.version } : a)),
    );
    setSelectedArticle((prev) => (prev && prev.id === updated.id ? { ...prev, review_status: updated.review_status, version: updated.version } : prev));
    setDraft((prev) => (prev.id === updated.id ? { ...prev, version: updated.version } : prev));
  }

  function selectReviewTab(tab: ReviewStatus) {
    setReviewTab(tab);
    setArticlePage(0);
    setSelectedArticleIds([]);
  }

  async function approveOne(articleId: number) {
    setReviewBusyId(articleId);
    try {
      const updated = await approveArticle(articleId);
      applyReviewedArticle(updated);
      toast("文章已通过审核", "success");
    } catch (error) {
      toast(error instanceof Error ? error.message : "审核失败", "error");
    } finally {
      setReviewBusyId(null);
    }
  }

  async function revokeOne(articleId: number) {
    setReviewBusyId(articleId);
    try {
      const updated = await revokeArticleApproval(articleId);
      applyReviewedArticle(updated);
      toast("已撤销审核", "success");
    } catch (error) {
      toast(error instanceof Error ? error.message : "撤销审核失败", "error");
    } finally {
      setReviewBusyId(null);
    }
  }

  async function approveWholeGroup(group: ArticleGroup) {
    setReviewBusyId(-group.id);
    try {
      const updated = await approveGroup(group.id);
      setGroups((prev) => prev.map((g) => (g.id === updated.id ? updated : g)));
      // Reflect approval on the member articles already loaded.
      const memberIds = new Set(updated.items.map((item) => item.article_id));
      setArticles((prev) => prev.map((a) => (memberIds.has(a.id) ? { ...a, review_status: "approved" } : a)));
      setSelectedArticle((prev) => (prev && memberIds.has(prev.id) ? { ...prev, review_status: "approved" } : prev));
      toast("分组已全部通过审核", "success");
    } catch (error) {
      toast(error instanceof Error ? error.message : "分组审核失败", "error");
    } finally {
      setReviewBusyId(null);
    }
  }

  function groupArticleSummaries(group: ArticleGroup): ArticleSummary[] {
    return group.items
      .slice()
      .sort((a, b) => a.sort_order - b.sort_order)
      .map((gi) => articleById[gi.article_id])
      .filter((a): a is ArticleSummary => a !== undefined);
  }

  function openDistributeForGroup(group: ArticleGroup) {
    setDistributeTarget({ kind: "group", groupId: group.id, name: group.name, articles: groupArticleSummaries(group) });
  }

  function openDistributeForSelection() {
    const selected = selectedArticleIds
      .map((id) => articleById[id])
      .filter((a): a is ArticleSummary => a !== undefined);
    if (selected.length === 0) return;
    setDistributeTarget({ kind: "selection", articles: selected });
  }

  function openDistributeForCurrent() {
    if (!selectedArticle) return;
    setDistributeTarget({ kind: "article", article: selectedArticle });
  }

  const currentReviewStatus: ReviewStatus = selectedArticle?.review_status ?? "approved";

  return (
    <>
      <header className="topbar">
        <div>
          <p className="eyebrow">内容管理</p>
          <h1>图文工作台</h1>
        </div>
        <div className="topActions">
          <div className="statusHints">
            {imageUploading > 0 && <span className="statusHint">图片传输中</span>}
            {loading && statusText ? <span className="statusHint">{statusText}</span> : null}
          </div>
          <button className="dangerButton" disabled={!draft.id || loading} type="button" onClick={() => setConfirmDeleteArticle(true)}>
            <Trash2 size={16} />
            删除
          </button>
          <button className="primaryButton" disabled={loading || imageUploading > 0} type="button" onClick={() => void saveArticle()}>
            <Save size={16} />
            保存
          </button>
          <button className="secondaryButton" disabled={loading} type="button" onClick={() => { if (dirtyCheckRef?.current?.() ?? false) { setConfirmUnsavedNew(true); } else { resetDraft(); } }}>
            <Plus size={16} />
            新建
          </button>
        </div>
      </header>

      <section className="contentGrid">
        <aside className="listPane">
          <div className="reviewTabs">
            <button
              type="button"
              className={`reviewTabBtn ${reviewTab === "pending" ? "active" : ""}`}
              onClick={() => selectReviewTab("pending")}
            >
              未审核
              <span className="reviewTabCount">{reviewCounts.pending}</span>
            </button>
            <button
              type="button"
              className={`reviewTabBtn ${reviewTab === "approved" ? "active" : ""}`}
              onClick={() => selectReviewTab("approved")}
            >
              已审核
              <span className="reviewTabCount">{reviewCounts.approved}</span>
            </button>
          </div>

          {reviewTab === "approved" && selectedArticleIds.length > 0 ? (
            <div className="bulkBar">
              <div className="bulkBarLeft">
                <ListChecks size={16} />
                <span>已选 {selectedArticleIds.length} 篇</span>
              </div>
              <div className="bulkBarRight">
                <button type="button" className="bulkClearBtn" onClick={() => setSelectedArticleIds([])}>
                  取消选择
                </button>
                <button type="button" className="bulkDistributeBtn" onClick={openDistributeForSelection}>
                  <Send size={15} />
                  自动分发
                </button>
              </div>
            </div>
          ) : null}

          <div className="searchRow">
            <Search size={16} />
            <input value={query} placeholder="搜索标题或作者" onChange={(event) => setQuery(event.target.value)} />
            <button type="button" onClick={searchArticles}>
              搜索
            </button>
          </div>

          <div className="articleList contentArticleList">
            {pagedUnifiedList.map((item) => {
              if (item.type === "article") {
                return (
                  <div className="articleRowWithAction" key={`a-${item.article.id}`}>
                    <ArticleListItem
                      article={item.article}
                      draftId={draft.id}
                      selectedIds={selectedArticleIds}
                      onToggle={toggleSelectedArticle}
                      onSelect={(article) => void loadArticle(article)}
                    />
                    <div className="articleRowActions">
                      {item.article.review_status === "pending" ? (
                        <button
                          className="inlineMiniButton approveMiniButton"
                          type="button"
                          disabled={reviewBusyId === item.article.id}
                          onClick={() => void approveOne(item.article.id)}
                        >
                          <Check size={13} />
                          通过审核
                        </button>
                      ) : null}
                      <button
                        className="inlineMiniButton"
                        type="button"
                        onClick={() => {
                          setGroupPickerArticle(item.article);
                          setGroupPickerSelectedId(groups[0]?.id ?? null);
                        }}
                      >
                        加入分组
                      </button>
                    </div>
                  </div>
                );
              }
              const { group } = item;
              const isExpanded = expandedGroupIds.has(group.id);
              const counts = groupReviewCounts(group);
              const fullyApproved = counts.total > 0 && counts.approved === counts.total;
              // 只列当前标签状态的文章；另一侧成员数用于跨标签提示。
              const groupArticles = groupArticleSummaries(group).filter(
                (a) => a.review_status === reviewTab,
              );
              const otherCount =
                reviewTab === "pending" ? counts.approved : counts.total - counts.approved;
              return (
                <div className="groupRowItem" key={`g-${group.id}`}>
                  <div className={`groupRowHeader ${group.id === editingGroupId ? "selected" : ""}`}>
                    <button
                      className="groupRowToggle"
                      type="button"
                      onClick={() =>
                        setExpandedGroupIds((prev) => {
                          const next = new Set(prev);
                          if (next.has(group.id)) next.delete(group.id);
                          else next.add(group.id);
                          return next;
                        })
                      }
                    >
                      <ChevronRight size={18} className={`groupRowChevron${isExpanded ? " open" : ""}`} />
                      <span className="groupRowName">{group.name}</span>
                      <small className="groupRowCount">{group.items.length} 篇</small>
                    </button>
                    <div className="groupRowReview">
                      {counts.total > 0 ? (
                        <span className={`badge ${fullyApproved ? "succeeded" : "waiting_manual_publish"}`}>
                          {fullyApproved ? "全部已审核" : `${counts.approved}/${counts.total} 已审核`}
                        </span>
                      ) : null}
                      {reviewTab === "approved" && fullyApproved ? (
                        <button
                          type="button"
                          className="inlineMiniButton distributeMiniButton"
                          onClick={() => openDistributeForGroup(group)}
                        >
                          <Send size={13} />
                          自动分发
                        </button>
                      ) : reviewTab === "pending" && counts.total - counts.approved > 0 ? (
                        <button
                          type="button"
                          className="inlineMiniButton approveMiniButton"
                          disabled={reviewBusyId === -group.id}
                          onClick={() => void approveWholeGroup(group)}
                        >
                          全部通过
                        </button>
                      ) : null}
                    </div>
                    <button className="groupRowEdit" type="button" onClick={() => loadGroup(group)}>
                      编辑
                    </button>
                  </div>
                  {isExpanded ? (
                    <div className="groupRowArticles">
                      {otherCount > 0 ? (
                        <p className="groupCrossTabHint">
                          另有 {otherCount} 篇{reviewTab === "pending" ? "已审核" : "待审核"}，
                          切到「{reviewTab === "pending" ? "已审核" : "未审核"}」标签查看
                        </p>
                      ) : null}
                      {groupArticles.map((article) => (
                        <article
                          className={`articleItem ${article.id === draft.id ? "selected" : ""}`}
                          key={article.id}
                        >
                          <label className="checkLine">
                            <input
                              checked={selectedArticleIds.includes(article.id)}
                              type="checkbox"
                              onChange={() => toggleSelectedArticle(article.id)}
                            />
                          </label>
                          <button type="button" onClick={() => void loadArticle(article)}>
                            <strong>{article.title}</strong>
                            <span>{article.author || "未填写作者"}</span>
                            <small>
                              {formatDateTime(article.updated_at)}
                              {article.published_count > 0 ? <span style={{ color: "#16a34a", marginLeft: 6 }}>· 已发布 {article.published_count} 次</span> : null}
                            </small>
                          </button>
                          <div className="articleItemBadge">
                            <ReviewBadge status={article.review_status} />
                          </div>
                          <button
                            className="inlineMiniButton removeFromGroupButton"
                            type="button"
                            onClick={() => void removeArticleFromGroup(group, article.id)}
                          >
                            移出
                          </button>
                        </article>
                      ))}
                      {groupArticles.length === 0 && otherCount === 0 ? <p className="emptyText">分组暂无文章</p> : null}
                    </div>
                  ) : null}
                </div>
              );
            })}
            {unifiedList.length === 0 ? <p className="emptyText">暂无文章</p> : null}
          </div>

          <Pagination
            page={articlePage}
            totalPages={totalArticlePages}
            loading={loading}
            onPrev={() => void changeArticlePage(articlePage - 1)}
            onNext={() => void changeArticlePage(articlePage + 1)}
          />

          <section className="groupBox">
            <h2>文章分组</h2>
            <p style={{ fontSize: 12, color: "#64748b", margin: "0 0 8px" }}>
              {editingGroupId ? "勾选左侧文章后点「更新」可增减分组内文章" : "勾选左侧文章后填写名称，点「创建」"}
            </p>
            <div className="groupCreate">
              <input value={groupName} placeholder="分组名称" onChange={(event) => setGroupName(event.target.value)} />
              <button type="button" onClick={saveGroupFromSelection}>
                {editingGroupId ? "更新" : "创建"}
              </button>
            </div>
            {editingGroupId ? (
              <div className="groupEditActions">
                <button type="button" onClick={() => { setEditingGroupId(null); setGroupName(""); setSelectedArticleIds([]); }}>
                  取消编辑
                </button>
                <button type="button" onClick={() => setConfirmDeleteGroup(true)}>
                  删除分组
                </button>
              </div>
            ) : null}
          </section>
        </aside>

        <section className="editorPane">
          <div className="formRow split">
            <label>
              标题
              <input value={draft.title} onChange={(event) => setDraft({ ...draft, title: event.target.value })} />
            </label>
            <label>
              作者
              <input value={draft.author} onChange={(event) => setDraft({ ...draft, author: event.target.value })} />
            </label>
            <label>
              状态
              <select value={draft.status} onChange={(event) => setDraft({ ...draft, status: event.target.value })}>
                <option value="draft">草稿</option>
                <option value="ready">待发布</option>
                <option value="archived">归档</option>
              </select>
            </label>
          </div>

          {selectedArticle ? (
            <div className={`reviewStrip ${currentReviewStatus === "approved" ? "approved" : "pending"}`}>
              <div className="reviewStripLeft">
                <ShieldCheck size={18} />
                <div className="reviewStripText">
                  <strong>{currentReviewStatus === "approved" ? "审核状态：已通过" : "审核状态"}</strong>
                  <span>{currentReviewStatus === "approved" ? "该文章已可进入发布" : "通过审核后该文章方可进入发布"}</span>
                </div>
              </div>
              <div className="reviewStripActions">
                <ReviewBadge status={currentReviewStatus} />
                {currentReviewStatus === "approved" ? (
                  <>
                    <button
                      type="button"
                      className="secondaryButton reviewStripBtn"
                      disabled={reviewBusyId === selectedArticle.id}
                      onClick={() => void revokeOne(selectedArticle.id)}
                    >
                      撤销审核
                    </button>
                    <button type="button" className="reviewStripDistribute" onClick={openDistributeForCurrent}>
                      <Send size={15} />
                      自动分发
                    </button>
                  </>
                ) : (
                  <button
                    type="button"
                    className="reviewStripApprove"
                    disabled={reviewBusyId === selectedArticle.id}
                    onClick={() => void approveOne(selectedArticle.id)}
                  >
                    <Check size={15} />
                    通过审核
                  </button>
                )}
              </div>
            </div>
          ) : null}

          <section className="coverRow">
            <div className="coverPreview">
              {(pendingCoverUrl ?? assetSrc(draft.cover_asset_id)) ? <img alt="封面" src={pendingCoverUrl ?? assetThumbSrc(draft.cover_asset_id) ?? assetSrc(draft.cover_asset_id)!} /> : <span>封面</span>}
            </div>
            <label className="fileButton">
              <Upload size={16} />
              上传封面
              <input accept="image/*" type="file" onChange={(event) => { void handleCoverUpload(event.target.files?.[0] ?? null); event.currentTarget.value = ""; }} />
            </label>
            {selectedArticle ? <span className="metaText">正文图片 {selectedArticle.body_assets.length} 张</span> : null}
          </section>

          <EditorToolbar
            editor={editor}
            onImageUpload={handleBodyImageUpload}
            imageSelected={!!editor?.isActive("image")}
            onSaveImage={() => {
              const src = editor?.getAttributes("image").src as string | undefined;
              if (src) setSaveImageSrc(src);
              else toast("请先选中正文中的图片", "error");
            }}
          />
          <div className="editorWrap">
            <EditorContent editor={editor} />
          </div>
          <div style={{ display: "flex", justifyContent: "flex-end", alignItems: "center", gap: 8, padding: "4px 8px", fontSize: 12, color: charCount < 300 ? "#e67e22" : "#888" }}>
            <span>正文字数：{charCount} 字</span>
            {charCount < 300 && <span>（建议不少于 300 字）</span>}
          </div>

        </section>
      </section>

      {distributeTarget ? (
        <DistributeModal
          target={distributeTarget}
          accounts={accounts}
          onClose={() => setDistributeTarget(null)}
          onDistributed={() => setSelectedArticleIds([])}
        />
      ) : null}

      {saveImageSrc ? (
        <ImageSaveDialog
          imageSrc={saveImageSrc}
          onClose={() => setSaveImageSrc(null)}
          onSaved={(msg) => toast(msg, "success")}
          onError={(msg) => toast(msg, "error")}
        />
      ) : null}

      {groupPickerArticle ? (
        <Modal
          title="加入分组"
          onClose={() => setGroupPickerArticle(null)}
          footer={
            <>
              <button type="button" onClick={() => setGroupPickerArticle(null)}>
                取消
              </button>
              <button type="button" disabled={!groupPickerSelectedId || loading} onClick={() => void addArticleToGroup()}>
                加入
              </button>
            </>
          }
        >
          <p style={{ margin: "0 0 12px", fontSize: 13, color: "#64748b" }}>{groupPickerArticle.title}</p>
          <div className="groupPickerList">
            {groups.map((group) => (
              <label className="groupPickerOption" key={group.id}>
                <input
                  checked={groupPickerSelectedId === group.id}
                  name="targetGroup"
                  type="radio"
                  onChange={() => setGroupPickerSelectedId(group.id)}
                />
                <span>{group.name}</span>
                <small>{group.items.length} 篇</small>
              </label>
            ))}
            {groups.length === 0 ? <p className="emptyText">暂无分组</p> : null}
          </div>
        </Modal>
      ) : null}

      {confirmDeleteArticle ? (
        <Modal
          title="确认删除文章？"
          onClose={() => setConfirmDeleteArticle(false)}
          footer={
            <>
              <button type="button" onClick={() => setConfirmDeleteArticle(false)}>取消</button>
              <button type="button" className="dangerButton" disabled={loading} onClick={() => { setConfirmDeleteArticle(false); void deleteCurrentArticle(); }}>确认删除</button>
            </>
          }
        >
          <p>《{draft.title}》删除后不可恢复</p>
        </Modal>
      ) : null}

      {confirmDeleteGroup ? (
        <Modal
          title="确认删除分组？"
          onClose={() => setConfirmDeleteGroup(false)}
          footer={
            <>
              <button type="button" onClick={() => setConfirmDeleteGroup(false)}>取消</button>
              <button type="button" className="dangerButton" disabled={loading} onClick={() => { setConfirmDeleteGroup(false); void deleteEditingGroup(); }}>确认删除</button>
            </>
          }
        >
          <p>分组下文章的关联将被移除，文章本身不受影响</p>
        </Modal>
      ) : null}

      {confirmUnsavedNew ? (
        <Modal
          title="当前有未保存的内容，确认放弃？"
          onClose={() => setConfirmUnsavedNew(false)}
          footer={
            <>
              <button type="button" onClick={() => setConfirmUnsavedNew(false)}>取消</button>
              <button type="button" className="primaryButton" onClick={() => { setConfirmUnsavedNew(false); resetDraft(); }}>确认放弃</button>
            </>
          }
        >
          {null}
        </Modal>
      ) : null}
    </>
  );
}
