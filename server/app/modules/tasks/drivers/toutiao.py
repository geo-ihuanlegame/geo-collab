from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

from server.app.modules.articles.tiptap_Parser import BodySegment
from server.app.modules.tasks.drivers.driver_Base import PublishError, PublishPayload, PublishResult, UserInputRequired
from server.app.shared.diagnostics import publish_step, record_publish_diagnostic

TOUTIAO_PUBLISH_URL = "https://mp.toutiao.com/profile_v4/graphic/publish"
_MAX_UPLOAD_WIDTH = 1920
_MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 MB
QR_HINTS = ("扫码", "扫一扫", "二维码")
CAPTCHA_HINTS = ("验证码", "安全验证", "图形验证")
LOGIN_REDIRECT_HINTS = ("login", "passport", "sso", "登录")
LOGIN_HINTS = (*QR_HINTS, *CAPTCHA_HINTS, *LOGIN_REDIRECT_HINTS)
PUBLISH_HINTS = ("发布", "标题", "正文", "图文", "文章")


PublishFillResult = PublishResult


@dataclass(frozen=True)
class BodyImageSlot:
    index: int
    marker: str
    image_path: Path
    image_asset_id: str | None


@dataclass(frozen=True)
class BodyFillPlan:
    full_text: str
    image_slots: list[BodyImageSlot]
    text_chars: int


# 头条号发布异常，可附带失败截图
class ToutiaoPublishError(PublishError):
    pass


class ToutiaoUserInputRequired(UserInputRequired, ToutiaoPublishError):
    def __init__(
        self,
        message: str,
        screenshot: bytes | None = None,
        session_id: str | None = None,
        novnc_url: str | None = None,
        error_type: str = "login_required",
    ):
        UserInputRequired.__init__(self, message, screenshot, session_id, novnc_url, error_type)


def _close_ai_drawer(page: Any) -> None:
    """关闭头条号 AI 创作助手抽屉，避免遮挡正文编辑区。"""
    try:
        close_btns = page.locator(".close-btn")
        if close_btns.count() > 0 and close_btns.first.is_visible():
            close_btns.first.click()
            page.wait_for_timeout(200)
    except Exception:
        logger.warning("Failed to close AI drawer", exc_info=True)


def _dismiss_blocking_popups(page: Any) -> None:
    """Best-effort close for marketing/help popups that block the editor."""
    workflow_text_re = re.compile(
        r"确认发布|预览并发布|本地上传|已上传|选择封面|裁剪封面|封面设置|发布设置|定时发布"
    )
    close_text_re = re.compile(r"关闭|取消|我知道了|稍后再说|暂不|以后再说|跳过|不再提示|×|✕")

    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
    except Exception:
        logger.debug("Failed to press Escape while dismissing popups", exc_info=True)

    for _ in range(3):
        try:
            closed = bool(
                page.evaluate(
                    """
                    ({ workflowPattern, closePattern }) => {
                      const workflowRe = new RegExp(workflowPattern);
                      const closeRe = new RegExp(closePattern, "i");
                      const visible = (node) => {
                        if (!node || !node.getBoundingClientRect) return false;
                        const style = window.getComputedStyle(node);
                        const rect = node.getBoundingClientRect();
                        return style.display !== "none" &&
                          style.visibility !== "hidden" &&
                          rect.width > 0 &&
                          rect.height > 0;
                      };
                      const roots = Array.from(document.querySelectorAll([
                        "[role='dialog']",
                        "[aria-modal='true']",
                        "[class*='modal']",
                        "[class*='dialog']",
                        "[class*='popup']",
                        "[class*='popover']",
                        "[class*='drawer']"
                      ].join(","))).filter(visible);
                      for (const root of roots) {
                        const text = String(root.innerText || "");
                        if (workflowRe.test(text)) continue;
                        const candidates = Array.from(root.querySelectorAll([
                          "button",
                          "[role='button']",
                          "a",
                          "span",
                          "i",
                          "svg",
                          "[class*='close']",
                          "[aria-label*='关闭']",
                          "[title*='关闭']"
                        ].join(","))).filter(visible);
                        for (const node of candidates) {
                          const haystack = [
                            node.innerText,
                            node.getAttribute("aria-label"),
                            node.getAttribute("title"),
                            node.getAttribute("class")
                          ].join(" ");
                          if (!closeRe.test(String(haystack || ""))) continue;
                          node.click();
                          return true;
                        }
                      }
                      return false;
                    }
                    """,
                    {
                        "workflowPattern": workflow_text_re.pattern,
                        "closePattern": close_text_re.pattern,
                    },
                )
            )
        except Exception:
            logger.debug("Failed to dismiss blocking popup via DOM", exc_info=True)
            return
        if not closed:
            return
        try:
            page.wait_for_timeout(500)
        except Exception:
            return


def _fill_title(page: Any, title: str) -> None:
    """填充文章标题（最多 50 字）。"""
    try:
        field = page.get_by_role("textbox", name="请输入文章标题")
        field.wait_for(state="visible", timeout=20000)
        field.fill(title[:50])
        return
    except Exception:
        logger.warning("Title field not found via textbox role, trying CSS fallback", exc_info=True)
    try:
        field = page.locator("input[placeholder*='标题']").first
        field.wait_for(state="visible", timeout=5000)
        field.fill(title[:50])
        return
    except Exception:
        logger.warning("Title field not found via CSS fallback", exc_info=True)
    raise ToutiaoPublishError("Toutiao title field not found")


def _focus_body_editor(page: Any) -> None:
    """聚焦正文编辑区，不移动光标。

    用 JS .focus() 而非 Playwright .click()：click() 会把 ProseMirror 的 selection
    anchor 强制移到被点击元素处（总是 .first 段落 = 文档开头），导致插图后光标跳回
    第一段，后续文字/图片全部插在已插图片的上方，图片"沉底"。
    .focus() 只恢复键盘焦点，ProseMirror 会按自己存储的 selection 恢复光标位置。
    """
    focused = page.evaluate(
        """() => {
            const editor = Array.from(document.querySelectorAll("[contenteditable='true']"))
                .find(el => el.getBoundingClientRect().height >= 80);
            if (editor) { editor.focus(); return true; }
            return false;
        }"""
    )
    if not focused:
        raise ToutiaoPublishError("Toutiao body editor not found")


def _fill_body(page: Any, segments: list[BodySegment]) -> None:
    """Fill body in document order using text markers for image slots."""
    if not segments:
        raise ToutiaoPublishError("文章正文为空")

    plan = _build_body_fill_plan(segments)
    if not plan.full_text.strip() and not plan.image_slots:
        raise ToutiaoPublishError("Article body is empty")

    record_publish_diagnostic(
        f"body fill plan: segments={len(segments)}; text_chars={plan.text_chars}; image_slots={len(plan.image_slots)}"
    )
    _dismiss_blocking_popups(page)
    _clear_body_editor(page)
    if plan.full_text:
        _insert_body_text(page, plan.full_text)
        page.wait_for_timeout(300)

    for slot in plan.image_slots:
        _dismiss_blocking_popups(page)
        record_publish_diagnostic(
            f"body image slot upload start: index={slot.index}; asset_id={slot.image_asset_id}"
        )
        _replace_body_marker_with_image(page, slot)

    _assert_no_body_markers(page)


def _build_body_fill_plan(segments: list[BodySegment]) -> BodyFillPlan:
    parts: list[str] = []
    image_slots: list[BodyImageSlot] = []
    text_chars = 0
    for segment in segments:
        if segment.kind == "text":
            if segment.text:
                parts.append(segment.text)
                text_chars += len(segment.text)
            continue
        if segment.kind != "image":
            continue
        if segment.image_path is None:
            raise ToutiaoPublishError(f"Body image path is not resolved: {segment.image_asset_id}")
        index = len(image_slots) + 1
        marker = f"__GEO_IMAGE_SLOT_{index:04d}__"
        parts.append(marker)
        image_slots.append(
            BodyImageSlot(
                index=index,
                marker=marker,
                image_path=segment.image_path,
                image_asset_id=segment.image_asset_id,
            )
        )
    return BodyFillPlan(full_text="".join(parts), image_slots=image_slots, text_chars=text_chars)


def _clear_body_editor(page: Any) -> None:
    if not _select_body_editor_contents(page):
        raise ToutiaoPublishError("Toutiao body editor not found")
    page.keyboard.press("Backspace")
    page.wait_for_timeout(200)


def _select_body_editor_contents(page: Any) -> bool:
    return bool(
        page.evaluate(
            """() => {
                const editor = Array.from(document.querySelectorAll("[contenteditable='true']"))
                    .find(el => el.getBoundingClientRect().height >= 80);
                if (!editor) return false;
                editor.focus();
                const range = document.createRange();
                range.selectNodeContents(editor);
                const selection = window.getSelection();
                selection.removeAllRanges();
                selection.addRange(range);
                return true;
            }"""
        )
    )


def _replace_body_marker_with_image(page: Any, slot: BodyImageSlot) -> None:
    if not _select_body_marker(page, slot.marker):
        raise ToutiaoPublishError(f"Body image marker not found: {slot.marker}")
    _paste_body_image_path(page, slot.image_path, slot.image_asset_id)
    page.wait_for_timeout(300)
    if _body_marker_exists(page, slot.marker):
        raise ToutiaoPublishError(f"Body image marker was not replaced: {slot.marker}")


def _select_body_marker(page: Any, marker: str) -> bool:
    return bool(
        page.evaluate(
            """marker => {
                const editor = Array.from(document.querySelectorAll("[contenteditable='true']"))
                    .find(el => el.getBoundingClientRect().height >= 80);
                if (!editor) return false;
                const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT);
                let node;
                while ((node = walker.nextNode())) {
                    const index = node.nodeValue.indexOf(marker);
                    if (index < 0) continue;
                    editor.focus();
                    const range = document.createRange();
                    range.setStart(node, index);
                    range.setEnd(node, index + marker.length);
                    const selection = window.getSelection();
                    selection.removeAllRanges();
                    selection.addRange(range);
                    return true;
                }
                return false;
            }""",
            marker,
        )
    )


def _body_marker_exists(page: Any, marker: str) -> bool:
    return bool(
        page.evaluate(
            """marker => {
                const editor = Array.from(document.querySelectorAll("[contenteditable='true']"))
                    .find(el => el.getBoundingClientRect().height >= 80);
                return Boolean(editor && editor.textContent && editor.textContent.includes(marker));
            }""",
            marker,
        )
    )


def _assert_no_body_markers(page: Any) -> None:
    remaining = page.evaluate(
        """() => {
            const editor = Array.from(document.querySelectorAll("[contenteditable='true']"))
                .find(el => el.getBoundingClientRect().height >= 80);
            if (!editor || !editor.textContent) return [];
            return editor.textContent.match(/__GEO_IMAGE_SLOT_\\d{4}__/g) || [];
        }"""
    )
    if remaining:
        raise ToutiaoPublishError(f"Body image markers remain: {remaining}")


def _insert_body_text(page: Any, text: str) -> None:
    if not text:
        return
    if text == "\n":
        page.keyboard.press("Enter")
        page.wait_for_timeout(80)
        return
    page.evaluate("text => navigator.clipboard.writeText(text)", text)
    page.wait_for_timeout(50)
    page.keyboard.press("Control+v")
    page.wait_for_timeout(100)


def _body_image_count(page: Any) -> int:
    try:
        return page.locator("[contenteditable='true'] img").count()
    except Exception:
        return 0


@contextmanager
def _maybe_resize_for_upload(image_path: Path) -> Iterator[Path]:
    """Yield a possibly-resized copy of image_path for Toutiao upload.

    If the image exceeds 1920 px wide or 2 MB, a downscaled JPEG temp file is
    yielded and cleaned up on exit.  Falls back to the original path silently
    on any PIL error so as not to block the publish flow.
    """
    tmp_path: Path | None = None
    try:
        try:
            from PIL import Image as _PILImage

            stat_size = image_path.stat().st_size
            with _PILImage.open(image_path) as _probe:
                orig_width, orig_height = _probe.width, _probe.height
            needs_resize = orig_width > _MAX_UPLOAD_WIDTH or stat_size > _MAX_UPLOAD_BYTES

            if needs_resize:
                import tempfile as _tempfile

                tmp = _tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                tmp_path = Path(tmp.name)
                tmp.close()
                with _PILImage.open(image_path) as _img:
                    if orig_width > _MAX_UPLOAD_WIDTH:
                        ratio = _MAX_UPLOAD_WIDTH / orig_width
                        _img = _img.resize(
                            (_MAX_UPLOAD_WIDTH, int(orig_height * ratio)), _PILImage.LANCZOS
                        )
                    _img.convert("RGB").save(tmp_path, "JPEG", quality=85)
                record_publish_diagnostic(
                    f"image resized for upload: {image_path.name} "
                    f"({orig_width}px / {stat_size // 1024}KB) → JPEG 1920px"
                )
                yield tmp_path
                return
        except Exception:
            logger.warning("Image resize failed, uploading original: %s", image_path, exc_info=True)

        yield image_path
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _paste_body_image_path(page: Any, image_path: Path, asset_id: str | None) -> None:
    if not image_path.exists():
        raise ToutiaoPublishError(f"正文图片文件不存在: {asset_id or image_path}")

    before_count = _body_image_count(page)
    record_publish_diagnostic(f"body image upload start: asset_id={asset_id}; before_count={before_count}")

    try:
        with _maybe_resize_for_upload(image_path) as upload_path:
            _open_body_image_drawer(page)
            _upload_body_image_in_drawer(page, upload_path)
            _confirm_body_image_drawer(page)
            _wait_body_image_inserted(page, before_count)
        record_publish_diagnostic(f"body image inserted: asset_id={asset_id}; after_count={_body_image_count(page)}")
    except Exception as exc:
        after_count = _body_image_count(page)
        page_closed = _page_is_closed(page)
        screenshot = _screenshot(page)
        raise ToutiaoPublishError(
            (
                f"正文图片未能插入编辑器: {asset_id or image_path}; "
                f"before={before_count}; after={after_count}; "
                f"page_closed={page_closed}; error={type(exc).__name__}: {exc}"
            ),
            screenshot,
        ) from exc


def _open_body_image_drawer(page: Any) -> None:
    candidates = [
        "div.syl-toolbar-tool.image.static",
        ".syl-toolbar-tool.image",
        "[class*='syl-toolbar-tool'][class*='image']",
    ]
    last_error: Exception | None = None
    for selector in candidates:
        try:
            button = page.locator(selector).first
            button.wait_for(state="visible", timeout=5000)
            button.click(timeout=5000)
            page.locator(".mp-ic-img-drawer").wait_for(state="visible", timeout=10000)
            return
        except Exception as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    raise ToutiaoPublishError("未找到正文图片上传入口")


def _upload_body_image_in_drawer(page: Any, image_path: Path) -> None:
    drawer = page.locator(".mp-ic-img-drawer").last
    file_input = drawer.locator("input[type='file'][accept*='image']").first
    file_input.wait_for(state="attached", timeout=10000)
    file_input.set_input_files(str(image_path))
    try:
        drawer.get_by_text(re.compile(r"已上传\s*\d+\s*张图片")).wait_for(timeout=60000)
    except Exception as exc:
        raise ToutiaoPublishError(f"正文图片上传超时（60s）: {exc}") from exc


def _confirm_body_image_drawer(page: Any) -> None:
    drawer = page.locator(".mp-ic-img-drawer").last
    candidates = [
        drawer.get_by_role("button", name="确定"),
        drawer.locator("button:has-text('确定')").last,
        page.locator(".byte-drawer button:has-text('确定')").last,
    ]
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            candidate.wait_for(state="visible", timeout=10000)
            candidate.click(timeout=5000)
            try:
                page.locator(".mp-ic-img-drawer").last.wait_for(state="hidden", timeout=5000)
            except Exception:
                page.wait_for_timeout(300)
            return
        except Exception as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    raise ToutiaoPublishError("未找到正文图片确认按钮")


def _wait_body_image_inserted(page: Any, before_count: int, timeout_ms: int = 30000) -> None:
    page.wait_for_function(
        "count => document.querySelectorAll(\"[contenteditable='true'] img\").length > count",
        arg=before_count,
        timeout=timeout_ms,
    )
    # 600ms: ProseMirror 在 img 出现后还需要 onUpdate/updateState/selection 更新
    page.wait_for_timeout(600)


def _wait_body_image_ready(page: Any, before_count: int, timeout_ms: int = 30000) -> None:
    page.wait_for_function(
        "count => document.querySelectorAll(\"[contenteditable='true'] img\").length > count",
        arg=before_count,
        timeout=timeout_ms,
    )
    page.wait_for_function(
        """
        count => {
          const images = Array.from(document.querySelectorAll("[contenteditable='true'] img"));
          return images.length > count &&
            images.every((img) => img.complete && img.naturalWidth > 0);
        }
        """,
        arg=before_count,
        timeout=timeout_ms,
    )
    page.wait_for_timeout(500)


def _wait_publish_images_ready(page: Any, timeout_ms: int = 60000) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    stable_rounds = 0
    last_state: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        state = _publish_image_state(page)
        last_state = state
        if (
            state["invalid_count"] == 0
            and state["pending_count"] == 0
            and not state["has_progress"]
            and not state["has_uploading_text"]
        ):
            stable_rounds += 1
            if stable_rounds >= 2:
                page.wait_for_timeout(200)
                return
        else:
            stable_rounds = 0
        page.wait_for_timeout(500)

    screenshot = _screenshot(page)
    raise ToutiaoPublishError(f"正文图片上传未完成，仍存在临时图片 URI: {last_state}", screenshot)


def _publish_image_state(page: Any) -> dict[str, Any]:
    try:
        return page.evaluate(
        """
        () => {
          const editables = Array.from(document.querySelectorAll("[contenteditable='true']"));
          const images = editables.flatMap((node) => Array.from(node.querySelectorAll("img")));
          const isVisible = (node) => {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.display !== "none" &&
              style.visibility !== "hidden" &&
              rect.width > 0 &&
              rect.height > 0;
          };
          const isTemporarySrc = (src) => {
            if (!src) return true;
            const value = String(src);
            return value.startsWith("blob:") ||
              value.startsWith("data:") ||
              value.startsWith("file:") ||
              value.includes("127.0.0.1") ||
              value.includes("localhost") ||
              value.includes("/api/assets/");
          };
          const states = images.map((img, index) => {
            const src = img.currentSrc || img.src || img.getAttribute("src") || "";
            return {
              index,
              src: String(src).slice(0, 180),
              complete: Boolean(img.complete),
              naturalWidth: Number(img.naturalWidth || 0),
              temporary: isTemporarySrc(src),
            };
          });
          const progressSelectors = [
            "[role='progressbar']",
            ".byte-progress",
            ".semi-progress",
            "[class*='progress']",
            "[class*='Progress']",
            "[class*='uploading']",
            "[class*='Uploading']"
          ];
          const progressNodes = editables.flatMap((node) =>
            progressSelectors.flatMap((selector) => Array.from(node.querySelectorAll(selector)))
          );
          const bodyText = document.body?.innerText || "";
          return {
            image_count: states.length,
            invalid_count: states.filter((item) => item.temporary).length,
            pending_count: states.filter((item) => !item.complete || item.naturalWidth <= 0).length,
            invalid_sources: states.filter((item) => item.temporary).map((item) => item.src),
            pending_sources: states
              .filter((item) => !item.complete || item.naturalWidth <= 0)
              .map((item) => item.src),
            has_progress: progressNodes.some(isVisible),
            has_uploading_text: /上传中|正在上传|图片处理中|加载中|处理中/.test(bodyText),
          };
        }
        """
        )
    except Exception:
        logger.warning("Failed to evaluate publish image state", exc_info=True)
        return {
            "image_count": 0,
            "invalid_count": 0,
            "pending_count": 0,
            "invalid_sources": [],
            "pending_sources": [],
            "has_progress": False,
            "has_uploading_text": False,
        }


def _handle_cover(page: Any, cover_path: Path, cover_asset_id: str | None) -> None:
    """上传封面图片。封面图是必填项，路径已由 publish_runner 预先解析。"""
    if not cover_path.exists():
        raise ToutiaoPublishError(f"Cover asset file not found: {cover_asset_id or cover_path}")
    record_publish_diagnostic(f"cover upload start: asset_id={cover_asset_id}; path={cover_path.name}")

    try:
        _click_cover_upload_entry(page)
    except Exception as exc:
        body_hint = _body_text_hint(page)
        screenshot = _screenshot(page)
        raise ToutiaoPublishError(
            f"无法点击封面上传按钮: {exc}\n页面内容摘要: {body_hint}",
            screenshot,
        ) from exc

    try:
        with _maybe_resize_for_upload(cover_path) as upload_path:
            with page.expect_file_chooser(timeout=5000) as fc_info:
                page.get_by_role("button", name="本地上传").click()
            fc_info.value.set_files(str(upload_path))

            try:
                page.get_by_text(re.compile(r"已上传\s*1\s*张图片")).wait_for(timeout=60000)
            except Exception as exc:
                raise ToutiaoPublishError(f"封面上传超时（60s）: {exc}") from exc

            try:
                page.get_by_role("button", name="确定").click()
                page.wait_for_timeout(300)
            except Exception as exc:
                raise ToutiaoPublishError(f"无法点击封面确认按钮: {exc}") from exc
    except ToutiaoPublishError:
        raise
    except Exception as exc:
        raise ToutiaoPublishError(f"封面文件选择失败: {exc}") from exc


def _cover_already_present(page: Any) -> bool:
    try:
        return bool(
            page.evaluate(
                """
                () => {
                  const bodyText = document.body?.innerText || "";
                  if (!/(编辑替换|已上传\\s*1\\s*张图片)/.test(bodyText)) return false;
                  const visibleImage = Array.from(document.querySelectorAll("img")).some((img) => {
                    if (img.closest("[contenteditable='true']")) return false;
                    const rect = img.getBoundingClientRect();
                    const style = window.getComputedStyle(img);
                    return img.complete &&
                      img.naturalWidth > 0 &&
                      rect.width >= 40 &&
                      rect.height >= 40 &&
                      style.display !== "none" &&
                      style.visibility !== "hidden";
                  });
                  return visibleImage || /已上传\\s*1\\s*张图片/.test(bodyText);
                }
                """
            )
        )
    except Exception:
        logger.warning("Failed to detect existing Toutiao cover", exc_info=True)
        return False


def _click_cover_upload_entry(page: Any) -> None:
    candidates = [
        page.get_by_text("编辑替换").first,
        page.get_by_text("添加封面").first,
        page.locator("[class*='cover'] .add-icon").first,
        page.locator(".add-icon").first,
    ]
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            candidate.wait_for(state="visible", timeout=3000)
            candidate.scroll_into_view_if_needed(timeout=3000)
            candidate.click(timeout=3000)
            page.get_by_role("button", name="本地上传").wait_for(state="visible", timeout=7000)
            return
        except Exception as exc:
            last_error = exc
            _dismiss_cover_candidate_side_effect(page)
            continue
    if last_error is not None:
        raise last_error
    raise ToutiaoPublishError("未找到封面上传入口")


def _dismiss_cover_candidate_side_effect(page: Any) -> None:
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
    except Exception:
        logger.debug("Failed to dismiss failed cover entry side effect", exc_info=True)


def _click_publish_and_wait(page: Any, stop_before_publish: bool = False) -> str | None:
    """两步发布：先点"预览并发布"，再点"确认发布"。"""
    before_url = page.url

    try:
        _dismiss_blocking_popups(page)
        page.get_by_role("button", name="预览并发布").click()
    except Exception as exc:
        raise ToutiaoPublishError(f"无法点击「预览并发布」按钮: {exc}") from exc

    page.wait_for_timeout(300)
    _dismiss_blocking_popups(page)

    if stop_before_publish:
        return None

    try:
        confirm_btn = page.get_by_role("button", name="确认发布")
        confirm_btn.wait_for(state="visible", timeout=30000)
        confirm_btn.click()
    except Exception as exc:
        body_hint = _body_text_hint(page)
        screenshot = _screenshot(page)
        raise ToutiaoPublishError(f"无法点击「确认发布」按钮: {exc}\n页面内容摘要: {body_hint}", screenshot) from exc

    page.wait_for_timeout(300)

    try:
        ok_btn = page.get_by_role("button", name="确定")
        if ok_btn.count() and ok_btn.is_visible(timeout=3000):
            ok_btn.click()
            page.wait_for_timeout(300)
    except Exception:
        logger.warning("Failed to dismiss post-publish popup", exc_info=True)

    try:
        page.wait_for_url(lambda url: url != before_url, timeout=30000)
        return page.url
    except Exception:
        logger.warning("URL change wait failed after publish", exc_info=True)

    try:
        body_text = page.locator("body").inner_text(timeout=3000)
        if any(h in body_text for h in ("发布失败", "提交失败", "操作失败", "网络错误")):
            raise ToutiaoPublishError(f"发布页面报错: {body_text[:300]}")
        if any(h in body_text for h in ("发布成功", "已发布", "审核中", "投稿成功")):
            return page.url
    except ToutiaoPublishError:
        raise
    except Exception:
        logger.warning("Failed to read body text after publish", exc_info=True)

    return page.url


def _body_text_hint(page: Any, limit: int = 600) -> str:
    try:
        text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return "无法读取页面内容"
    compact = re.sub(r"\s+", " ", text).strip()
    return compact[:limit] if compact else "页面正文为空"


def _ensure_publish_page(page: Any) -> None:
    """确认当前页面是头条号发布页，且已登录。"""
    body = page.locator("body").inner_text(timeout=3000)
    haystack = f"{page.url}\n{page.title()}\n{body}"
    if any(hint in haystack for hint in LOGIN_HINTS):
        if any(hint in haystack for hint in QR_HINTS):
            error_type = "qr_scan_required"
        elif any(hint in haystack for hint in CAPTCHA_HINTS):
            error_type = "captcha_required"
        else:
            error_type = "login_required"
        raise ToutiaoUserInputRequired(
            "Toutiao account appears logged out; user login or verification is required",
            error_type=error_type,
        )
    if "mp.toutiao.com" not in page.url or not any(hint in haystack for hint in PUBLISH_HINTS):
        raise ToutiaoPublishError("Toutiao publish page not detected")


def _screenshot(page: Any) -> bytes | None:
    """截取当前页面全屏截图（用于失败诊断）。"""
    try:
        return page.screenshot(full_page=True)
    except Exception:
        logger.warning("Failed to capture screenshot", exc_info=True)
        return None


def _page_is_closed(page: Any) -> bool | str:
    try:
        return bool(page.is_closed())
    except Exception as exc:
        return f"unknown: {type(exc).__name__}: {exc}"


def _do_publish(page: Any, context: Any, payload: PublishPayload, stop_before_publish: bool) -> PublishResult:
    """
    核心发布逻辑。

    步骤：
      1. 打开头条发布页 → 2. 填标题 → 3. 上传封面 → 4. 填正文 → 5. 等待图片就绪 → 6. 点击发布
    """
    with publish_step("open Toutiao publish page", page=page):
        page.goto(TOUTIAO_PUBLISH_URL, wait_until="domcontentloaded", timeout=60000)
    try:
        page.get_by_role("textbox", name="请输入文章标题").wait_for(state="visible", timeout=20000)
    except Exception:
        pass
    with publish_step("ensure publish page", page=page):
        _ensure_publish_page(page)
    with publish_step("prepare editor", page=page):
        _close_ai_drawer(page)
        _dismiss_blocking_popups(page)
    with publish_step("fill title", page=page):
        _fill_title(page, payload.title)
        _dismiss_blocking_popups(page)
    with publish_step("upload cover", page=page):
        _handle_cover(page, payload.cover_asset_path, None)
        _dismiss_blocking_popups(page)
    with publish_step("fill body", page=page):
        _fill_body(page, payload.body_segments)
        _dismiss_blocking_popups(page)
    with publish_step("wait body images ready", page=page):
        _wait_publish_images_ready(page)
    with publish_step("click publish", page=page):
        publish_url = _click_publish_and_wait(page, stop_before_publish)
    try:
        with publish_step("save storage state"):
            context.storage_state(path=str(payload.state_path))
    except Exception:
        logger.warning("Failed to save storage state after publish", exc_info=True)
    message = "已进入发布预览，等待手动确认" if stop_before_publish else f"发布成功: {publish_url}"
    return PublishResult(
        url=publish_url,
        title=payload.title,
        message=message,
    )


class ToutiaoDriver:
    code = "toutiao"
    name = "头条号"
    home_url = "https://mp.toutiao.com"
    publish_url = TOUTIAO_PUBLISH_URL

    def detect_logged_in(self, *, url: str, title: str, body: str) -> bool:
        if any(hint in url for hint in ("login", "passport", "sso")):
            return False
        page_text = f"{title}\n{body}"
        if any(hint in page_text for hint in (*QR_HINTS, *CAPTCHA_HINTS)):
            return False
        return "mp.toutiao.com" in url and ("profile_v4" in url or "头条号" in title)

    def publish(
        self,
        *,
        page: Any,
        context: Any,
        payload: PublishPayload,
        stop_before_publish: bool,
    ) -> PublishResult:
        return _do_publish(page, context, payload, stop_before_publish)


from server.app.modules.tasks.drivers import register
register(ToutiaoDriver())
