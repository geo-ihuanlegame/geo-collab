"""头条号默认发布驱动（DOM 自动化）。

用 Playwright 操作头条号图文发布页的 ProseMirror/SylEditor 富文本编辑器：
逐段插标题 / 加粗 / 正文 / 图片，上传封面，最后两步点「预览并发布」→「确认发布」。
编辑器用的是字节自家设计系统（byte-btn / syl-toolbar-tool，非 Ant Design），
本文件大量函数靠 page.evaluate 注入 JS 直接操作 contenteditable，绕开 ProseMirror
对键盘事件 / inputRule / selection 的各种坑（详见各函数 docstring）。
register() 注册为 toutiao 默认驱动。页内 API 驱动见 toutiao_inpage.py。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from server.app.modules.accounts.secret_files import write_state
from server.app.modules.articles.parser import BodySegment
from server.app.modules.tasks.drivers.base import (
    PublishError,
    PublishPayload,
    PublishResult,
    UserInputRequired,
)
from server.app.modules.tasks.drivers.image_upload import _maybe_resize_for_upload
from server.app.modules.tasks.drivers.toutiao_creator_id import (
    TOUTIAO_ID_LABEL,
    CreatorIdResult,
    normalize_dom_scan_result,
    parse_creator_info_response,
)
from server.app.shared.diagnostics import publish_step, record_publish_diagnostic

logger = logging.getLogger(__name__)

TOUTIAO_PUBLISH_URL = "https://mp.toutiao.com/profile_v4/graphic/publish"
# 创作者平台个人信息页 + 取 media_id 的 creator_center 接口（与 spike 一致，见设计稿 §3）
TOUTIAO_CREATOR_INFO_URL = "https://mp.toutiao.com/profile_v4/personal/info"
TOUTIAO_CREATOR_INFO_API = "/mp/agw/creator_center/user_info"
# best-effort 抽取的导航 / 接口超时（毫秒）；超时即降级返回 None，绝不拖垮登录
_EXTRACT_NAV_TIMEOUT_MS = 30_000


# ── creator-ID 抽取：page.evaluate 用的 JS 片段（sync / async 共用）─────────────

# 在活页上 fetch creator_center user_info（带 cookie），返回 {url,status,text}
_CREATOR_INFO_FETCH_JS = """
async (path) => {
  const response = await fetch(new URL(path, location.origin).href, {
    credentials: 'include',
    cache: 'no-store',
    headers: {'Accept': 'application/json,text/plain,*/*'}
  });
  return {url: response.url, status: response.status, text: await response.text()};
}
"""

# DOM 兜底：找含「头条号ID」label 的节点，向上 5 层内抓首个合法数字
_CREATOR_INFO_DOM_JS = """
(label) => {
  const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const idPattern = /[1-9]\\d{7,29}/;
  for (const node of document.querySelectorAll('body *')) {
    const ownText = normalize(node.innerText || node.textContent || '');
    if (!ownText || !ownText.includes(label)) continue;
    let current = node;
    for (let depth = 0; depth < 5 && current; depth++) {
      const text = normalize(current.innerText || current.textContent || '');
      const match = idPattern.exec(text);
      if (match) return {value: match[0], evidence: text};
      current = current.parentElement;
    }
  }
  const bodyText = normalize(document.body ? document.body.innerText : '');
  const index = bodyText.indexOf(label);
  if (index >= 0) {
    const evidence = bodyText.slice(index, index + 180);
    const match = idPattern.exec(evidence);
    if (match) return {value: match[0], evidence};
  }
  return null;
}
"""


def _redact_url(url: str) -> str:
    """脱敏诊断 URL 里的 token / ticket / session 等敏感 query（与 spike 一致）。"""
    return re.sub(
        r"([?&](?:token|ticket|session|sid|auth|csrf|msToken|X-Bogus)=)[^&#]+",
        r"\1<redacted>",
        url or "",
    )


def _on_creator_info_page(url: str) -> bool:
    low = (url or "").lower()
    return "mp.toutiao.com" in low and "auth/page/login" not in low and "/passport/" not in low


QR_HINTS = ("扫码", "扫一扫", "二维码")
CAPTCHA_HINTS = ("验证码", "安全验证", "图形验证")
LOGIN_REDIRECT_HINTS = ("login", "passport", "sso", "登录")
LOGIN_HINTS = (*QR_HINTS, *CAPTCHA_HINTS, *LOGIN_REDIRECT_HINTS)
PUBLISH_HINTS = ("发布", "标题", "正文", "图文", "文章")


PublishFillResult = PublishResult


@dataclass(frozen=True)
class BodyParagraph:
    """一个逻辑段落：文本 / 标题 / 图片三选一，供逐段插入编辑器。"""

    kind: str  # 类型："text" | "heading" | "image"
    runs: tuple[tuple[str, bool], ...] = ()  # (文本, 是否加粗)
    heading_level: int | None = None
    image_path: Path | None = None
    image_asset_id: str | None = None


def _group_paragraphs(segments: list[BodySegment]) -> list[BodyParagraph]:
    """把扁平 BodySegment 合并成可顺序插入的逻辑段落。"""
    paragraphs: list[BodyParagraph] = []
    current_runs: list[tuple[str, bool]] = []
    current_hlevel: int | None = None

    def _flush() -> None:
        if not current_runs:
            return
        text = "".join(t for t, _ in current_runs)
        if not text.strip():
            current_runs.clear()
            return
        kind = "heading" if current_hlevel is not None else "text"
        paragraphs.append(
            BodyParagraph(kind=kind, runs=tuple(current_runs), heading_level=current_hlevel)
        )
        current_runs.clear()

    for seg in segments:
        if seg.kind == "image":
            _flush()
            current_hlevel = None
            paragraphs.append(
                BodyParagraph(
                    kind="image", image_path=seg.image_path, image_asset_id=seg.image_asset_id
                )
            )
        elif seg.kind == "text" and seg.text == "\n":
            _flush()
            current_hlevel = None
        elif seg.kind == "text":
            if current_runs and current_hlevel != seg.heading_level:
                _flush()
            current_hlevel = seg.heading_level
            current_runs.append((seg.text, seg.bold))

    _flush()
    return paragraphs


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
    """尽力关闭遮挡编辑器的营销 / 帮助弹窗。"""
    workflow_text_re = re.compile(
        r"确认发布|预览并发布|本地上传|已上传|选择封面|裁剪封面|封面设置|发布设置|定时发布"
    )
    close_text_re = re.compile(
        r"关闭|取消|我知道了|稍后再说|暂不|以后再说|跳过|不再提示|×|✕"
        r"|不恢复|放弃草稿|放弃编辑|丢弃|不保留|重新开始|不使用草稿"
    )

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


def _focus_body_editor_end(page: Any) -> None:
    """聚焦正文编辑区并把光标移到文档末尾。

    每次插段前调用：确保新内容追加在已插入内容之后，避免光标残留在中途
    导致后续段落 / 图片插错位置（关键在于 selectNodeContents + collapse(false) 把光标压到末尾）。
    """
    moved = page.evaluate(
        """() => {
            const editor = Array.from(document.querySelectorAll("[contenteditable='true']"))
                .find(el => el.getBoundingClientRect().height >= 80);
            if (!editor) return false;
            editor.focus();
            const range = document.createRange();
            range.selectNodeContents(editor);
            range.collapse(false);
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
            return true;
        }"""
    )
    if not moved:
        raise ToutiaoPublishError("Toutiao body editor not found")


def _fill_body(page: Any, segments: list[BodySegment]) -> None:
    """逐段插入正文：标题用 '# ' inputRule，加粗用 Ctrl+B，图片用原有上传流程。"""
    if not segments:
        raise ToutiaoPublishError("文章正文为空")

    paragraphs = _group_paragraphs(segments)
    if not paragraphs:
        raise ToutiaoPublishError("Article body is empty")

    record_publish_diagnostic(f"body fill: {len(paragraphs)} paragraphs")
    _dismiss_blocking_popups(page)
    _clear_body_editor(page)
    _focus_body_editor_end(page)

    for i, para in enumerate(paragraphs):
        is_last = i == len(paragraphs) - 1
        _focus_body_editor_end(page)

        if para.kind == "image":
            if para.image_path is None:
                raise ToutiaoPublishError(
                    f"正文图片未解析出文件路径: asset_id={para.image_asset_id}"
                )
            _dismiss_blocking_popups(page)
            record_publish_diagnostic(f"body image upload: asset_id={para.image_asset_id}")
            _paste_body_image_path(page, para.image_path, para.image_asset_id)
            if not is_last:
                _focus_body_editor_end(page)
                page.keyboard.press("Enter")
                page.wait_for_timeout(100)
        elif para.kind == "heading":
            _insert_heading_paragraph(page, para.runs)
            if not is_last:
                _focus_body_editor_end(page)
                page.keyboard.press("Enter")
                page.wait_for_timeout(100)
        else:
            _insert_text_paragraph(page, para.runs)
            if not is_last:
                _focus_body_editor_end(page)
                page.keyboard.press("Enter")
                page.wait_for_timeout(100)
    _verify_body_text_complete(page, paragraphs)


def _body_editor_text(page: Any) -> str:
    text = page.evaluate(
        """() => {
            const editor = Array.from(document.querySelectorAll("[contenteditable='true']"))
                .find(el => el.getBoundingClientRect().height >= 80);
            if (!editor) return null;
            return editor.innerText || editor.textContent || "";
        }"""
    )
    if text is None:
        raise ToutiaoPublishError("Toutiao body editor not found")
    return str(text)


def _compact_body_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _verify_body_text_complete(page: Any, paragraphs: list[BodyParagraph]) -> None:
    """校验正文文本是否完整写入：按段落顺序在编辑器实际文本里依次匹配。

    去空白后逐片 find（cursor 单调前移，保证顺序）；任一片缺失即抛错并附截图——
    防止 ProseMirror inputRule / 时序问题悄悄吞掉部分文字却当作成功发布。
    """
    expected_chunks = [
        _compact_body_text("".join(text for text, _ in para.runs))
        for para in paragraphs
        if para.kind in ("text", "heading")
    ]
    expected_chunks = [chunk for chunk in expected_chunks if chunk]
    if not expected_chunks:
        return

    actual = _compact_body_text(_body_editor_text(page))
    cursor = 0
    for chunk in expected_chunks:
        index = actual.find(chunk, cursor)
        if index < 0:
            record_publish_diagnostic(
                f"body verify failed: expected_chunks={len(expected_chunks)} actual_len={len(actual)}"
            )
            raise ToutiaoPublishError(f"正文写入不完整，缺失片段: {chunk[:80]}", _screenshot(page))
        cursor = index + len(chunk)
    record_publish_diagnostic(
        f"body verify ok: expected_chunks={len(expected_chunks)} actual_len={len(actual)}"
    )


def _clear_body_editor(page: Any) -> None:
    """清空正文编辑器，并验证文本与图片节点均已清除（最多重试 3 次）。

    每次尝试：先全选（Ctrl+A），再按 Backspace 删除，然后读取编辑器内剩余文本和图片数。
    如果三次均未清除干净，仍继续发布流程并记录警告——避免因草稿顽固残留而直接失败。
    """
    for attempt in range(3):
        if not _select_body_editor_contents(page):
            raise ToutiaoPublishError("Toutiao body editor not found")
        # Ctrl+A 确保全选（_select_body_editor_contents 已使用 DOM selectNodeContents，
        # 再按键盘 Ctrl+A 可覆盖部分 ProseMirror 版本对 range 的处理差异）
        page.keyboard.press("Control+a")
        page.wait_for_timeout(100)
        page.keyboard.press("Backspace")
        page.wait_for_timeout(400)

        # 验证编辑器是否确实清空（文本 + 图片节点）
        text_left = ""
        images_left = 0
        try:
            text_left = _compact_body_text(_body_editor_text(page))
        except Exception:
            pass
        try:
            images_left = _body_image_count(page)
        except Exception:
            pass

        if not text_left and images_left == 0:
            return

        logger.warning(
            "Body editor not fully cleared (attempt %d/3): text_len=%d images=%d",
            attempt + 1,
            len(text_left),
            images_left,
        )
        page.wait_for_timeout(300)

    logger.warning(
        "Body editor may still contain residual content after 3 clear attempts; proceeding anyway"
    )


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


def _insert_text_atomic(page: Any, text: str) -> None:
    """将一段文本作为单次 DOM 事件原子插入当前光标位置。

    用 document.execCommand('insertText') 代替 keyboard.type(delay=5)：
    - keyboard.type 逐字符派发 keydown/keypress/input 事件；ProseMirror 对每个
      字符运行 inputRule 检测，行首 "1. "（数字+点+空格）会触发有序列表转换，
      导致 "1." 变成 CSS 生成的列表序号，innerText 读不到，verify 失败。
    - execCommand('insertText') 触发单次 input 事件（inputType=insertText），
      ProseMirror 只检查光标前末尾的几个字符，不会误触 orderedList inputRule。
    - 整段一次性写入，不存在字符丢失或 DOM 更新打断的时序问题。
    - 返回 false 时（极少数浏览器限制）回退到逐字符键入。
    """
    ok = page.evaluate("(t) => document.execCommand('insertText', false, t)", text)
    if not ok:
        page.keyboard.type(text, delay=5)
    page.wait_for_timeout(50)


def _insert_runs(page: Any, runs: tuple[tuple[str, bool], ...]) -> None:
    """向编辑器插入文本 run；加粗片段前后切换 Ctrl+B。"""
    for text, is_bold in runs:
        if not text:
            continue
        if is_bold:
            page.keyboard.press("Control+b")
            page.wait_for_timeout(50)
        _insert_text_atomic(page, text)
        if is_bold:
            page.keyboard.press("Control+b")
            page.wait_for_timeout(50)


def _insert_text_paragraph(page: Any, runs: tuple[tuple[str, bool], ...]) -> None:
    _insert_runs(page, runs)


def _insert_heading_paragraph(page: Any, runs: tuple[tuple[str, bool], ...]) -> None:
    """在行首键入 '# ' 触发 ProseMirror inputRule 转为 h1，再插入标题文本。

    用 Markdown 输入规则而非 Ctrl+Alt+1：后者在 Linux/Xvfb+Openbox 环境下被 WM
    拦截为切换工作区，导致 Chromium 失焦，后续粘贴内容全部丢失。
    '# ' 是纯字符输入，跨平台完全可靠。
    """
    page.keyboard.type("# ")
    page.wait_for_timeout(100)
    _insert_runs(page, runs)


def _body_image_count(page: Any) -> int:
    try:
        return page.locator("[contenteditable='true'] img").count()
    except Exception:
        return 0


def _paste_body_image_path(page: Any, image_path: Path, asset_id: str | None) -> None:
    """把一张正文图片经「打开图片抽屉→上传→确认→等插入」完整流程插入编辑器。

    以插入前后的 img 计数变化判定是否真的插入成功；失败时连同 before/after 计数、
    页面是否已关闭、原始异常一起打包成 ToutiaoPublishError 附截图，便于诊断。
    """
    if not image_path.exists():
        raise ToutiaoPublishError(f"正文图片文件不存在: {asset_id or image_path}")

    before_count = _body_image_count(page)
    record_publish_diagnostic(
        f"body image upload start: asset_id={asset_id}; before_count={before_count}"
    )

    try:
        with _maybe_resize_for_upload(image_path) as upload_path:
            _open_body_image_drawer(page)
            _upload_body_image_in_drawer(page, upload_path)
            _confirm_body_image_drawer(page)
            _wait_body_image_inserted(page, before_count)
        record_publish_diagnostic(
            f"body image inserted: asset_id={asset_id}; after_count={_body_image_count(page)}"
        )
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
    """发布前等所有正文图片落到正式 CDN 地址（不再是 blob:/data:/本地临时 URI）。

    要求连续 2 轮状态干净（无临时图、无 pending、无进度条 / 上传中文案）才放行，
    超时仍有临时 URI 则抛错附截图——否则会把指向本地 / blob 的图片发出去坏图。
    """
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
    """上传封面图片。封面图是必填项，路径已由 runner 预先解析。"""
    if not cover_path.exists():
        raise ToutiaoPublishError(f"Cover asset file not found: {cover_asset_id or cover_path}")
    record_publish_diagnostic(
        f"cover upload start: asset_id={cover_asset_id}; path={cover_path.name}"
    )

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
    """探测页面上是否已存在一张已上传的封面（「编辑替换」/「已上传 1 张图片」+ 可见图）。"""
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


def _click_publish_and_wait(
    page: Any, stop_before_publish: bool = False, commit_guard=None
) -> str | None:
    """两步发布：先点"预览并发布"，再点"确认发布"。「确认发布」点击=提交边界。"""
    from server.app.modules.tasks.drivers.base import NOOP_COMMIT_GUARD

    if commit_guard is None:
        commit_guard = NOOP_COMMIT_GUARD
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

    # ── 提交边界：仅「确认发布」点击。点击因网络失败=可能已提交→结果未知 ──
    with commit_guard.committing():
        try:
            confirm_btn = page.get_by_role("button", name="确认发布")
            confirm_btn.wait_for(state="visible", timeout=30000)
            confirm_btn.click()
        except Exception as exc:
            body_hint = _body_text_hint(page)
            screenshot = _screenshot(page)
            raise ToutiaoPublishError(
                f"无法点击「确认发布」按钮: {exc}\n页面内容摘要: {body_hint}", screenshot
            ) from exc

    # ── 守卫之外：发布后轮询检测（原样保留，不改一字）──
    page.wait_for_timeout(500)

    # 发布后轮询：每轮先关闭后置弹窗（作品同步授权、加入创作者计划等），
    # 再等待 URL 跳转（5s 超时），共 6 轮，总计约 30s。
    # 这比单次 30s wait_for_url 更可靠：弹窗会阻止 URL 跳转，必须先关再等。
    for attempt in range(6):
        # 关闭发布后可能出现的各类弹窗
        _dismiss_blocking_popups(page)

        try:
            page.wait_for_url(lambda url: url != before_url, timeout=5000)
            return page.url
        except Exception:
            pass

        # 检查页面正文是否出现明确的成功或失败信号
        try:
            body_text = page.locator("body").inner_text(timeout=2000)
            if any(h in body_text for h in ("发布失败", "提交失败", "操作失败", "网络错误")):
                raise ToutiaoPublishError(f"发布页面报错: {body_text[:300]}")
            if any(h in body_text for h in ("发布成功", "已发布", "审核中", "投稿成功")):
                logger.info(
                    "Publish confirmed by page text after attempt %d (URL did not change)",
                    attempt + 1,
                )
                return page.url
        except ToutiaoPublishError:
            raise
        except Exception:
            pass

    logger.warning(
        "URL change wait failed after publish (all 6 attempts, ~30s total); treating as success"
    )
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


def _install_draft_cleanup_script(page: Any) -> None:
    """注入 init_script，在页面脚本执行前清除头条编辑器的草稿数据。

    使用 add_init_script 确保清理代码在 DOMContentLoaded 之前运行，优先于
    ProseMirror/SylEditor 读取 localStorage 草稿的时机，防止上次发布的内容
    通过自动草稿恢复功能混入当前发布。
    """
    try:
        page.add_init_script(
            """(() => {
                try { sessionStorage.clear(); } catch (_) {}
                try {
                    const remove = [];
                    for (let i = 0; i < localStorage.length; i++) {
                        const k = localStorage.key(i);
                        if (!k) continue;
                        const lk = k.toLowerCase();
                        if (
                            lk.includes('draft') ||
                            lk.includes('editor') ||
                            lk.includes('article') ||
                            lk.includes('content') ||
                            lk.includes('syl') ||
                            lk.includes('pgc') ||
                            lk.includes('autosave') ||
                            lk.includes('local_save') ||
                            lk.includes('offline')
                        ) {
                            remove.push(k);
                        }
                    }
                    remove.forEach(k => { try { localStorage.removeItem(k); } catch (_) {} });
                } catch (_) {}
            })();"""
        )
        logger.debug("Draft cleanup init_script installed")
    except Exception:
        logger.warning("Failed to install draft cleanup init_script", exc_info=True)


def _do_publish(
    page: Any,
    context: Any,
    payload: PublishPayload,
    stop_before_publish: bool,
    commit_guard=None,
    retry_policy=None,
) -> PublishResult:
    """
    核心发布逻辑。

    步骤：
      1. 打开头条发布页 → 2. 填标题 → 3. 上传封面 → 4. 填正文 → 5. 等待图片就绪 → 6. 点击发布
    """
    from server.app.shared.resilience import RetryPolicy, retry_call

    _policy = retry_policy or RetryPolicy()
    # 在页面脚本执行前注入草稿清理代码，防止头条编辑器自动恢复上次的草稿内容
    _install_draft_cleanup_script(page)
    with publish_step("open Toutiao publish page", page=page):
        retry_call(
            lambda: page.goto(TOUTIAO_PUBLISH_URL, wait_until="domcontentloaded", timeout=60000),
            policy=_policy,
        )
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
        publish_url = _click_publish_and_wait(page, stop_before_publish, commit_guard=commit_guard)
    try:
        with publish_step("save storage state"):
            write_state(Path(payload.state_path), context.storage_state())
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
        commit_guard=None,
        retry_policy=None,
    ) -> PublishResult:
        return _do_publish(
            page,
            context,
            payload,
            stop_before_publish,
            commit_guard=commit_guard,
            retry_policy=retry_policy,
        )

    def extract_platform_user_id_sync(self, *, page: Any) -> str | None:
        """同步 Playwright 活页上抽取头条号ID（media_id）。best-effort，失败返回 None。

        导航到创作者个人信息页 → page.evaluate 调 creator_center user_info 接口取 media_id
        → DOM label 兜底。解析全部委托给 toutiao_creator_id 纯函数层。任何异常都记脱敏
        诊断后返回 None，绝不抛出（抽取失败不能拖垮登录态检测）。
        """
        try:
            result = _extract_creator_id_sync(page)
            if result is not None:
                logger.info("Toutiao creator-id extracted (sync): source=%s", result.source)
                return result.value
            return None
        except Exception:
            logger.warning(
                "Toutiao creator-id sync extraction failed for %s",
                _redact_url(getattr(page, "url", "") or ""),
                exc_info=True,
            )
            return None

    async def extract_platform_user_id_async(self, *, page: Any) -> str | None:
        """异步 Playwright 活页上抽取头条号ID（media_id）。best-effort，失败返回 None。

        与 sync 版同源逻辑，只是用 async Playwright API；解析复用同一纯函数层。
        """
        try:
            result = await _extract_creator_id_async(page)
            if result is not None:
                logger.info("Toutiao creator-id extracted (async): source=%s", result.source)
                return result.value
            return None
        except Exception:
            logger.warning(
                "Toutiao creator-id async extraction failed for %s",
                _redact_url(getattr(page, "url", "") or ""),
                exc_info=True,
            )
            return None


# ── creator-ID 抽取 I/O（sync / async 各一份，解析复用纯函数）──────────────────


def _extract_creator_id_sync(page: Any) -> CreatorIdResult | None:
    """sync Playwright：导航 → fetch 接口 → DOM 兜底。返回解析结果或 None。"""
    url = page.url or ""
    if not _on_creator_info_page(url) or "/profile_v4/" not in url.lower():
        try:
            page.goto(
                TOUTIAO_CREATOR_INFO_URL,
                wait_until="domcontentloaded",
                timeout=_EXTRACT_NAV_TIMEOUT_MS,
            )
        except Exception:
            logger.warning("Toutiao creator info navigation failed (sync)", exc_info=True)

    if not _on_creator_info_page(page.url or ""):
        return None

    # 1) creator_center user_info 接口（结构化 media_id）
    try:
        response = page.evaluate(_CREATOR_INFO_FETCH_JS, TOUTIAO_CREATOR_INFO_API)
        source = f"fetch:{response.get('status')}:{_redact_url(response.get('url', ''))}"
        found = parse_creator_info_response(response.get("text") or "", source)
        if found is not None:
            return found
    except Exception:
        logger.warning("Toutiao creator info fetch failed (sync)", exc_info=True)

    # 2) DOM label 兜底
    try:
        dom = page.evaluate(_CREATOR_INFO_DOM_JS, TOUTIAO_ID_LABEL)
        return normalize_dom_scan_result(dom, f"dom:{_redact_url(page.url or '')}")
    except Exception:
        logger.warning("Toutiao creator info DOM scan failed (sync)", exc_info=True)
        return None


async def _extract_creator_id_async(page: Any) -> CreatorIdResult | None:
    """async Playwright：导航 → fetch 接口 → DOM 兜底。返回解析结果或 None。"""
    url = page.url or ""
    if not _on_creator_info_page(url) or "/profile_v4/" not in url.lower():
        try:
            await page.goto(
                TOUTIAO_CREATOR_INFO_URL,
                wait_until="domcontentloaded",
                timeout=_EXTRACT_NAV_TIMEOUT_MS,
            )
        except Exception:
            logger.warning("Toutiao creator info navigation failed (async)", exc_info=True)

    if not _on_creator_info_page(page.url or ""):
        return None

    # 1) creator_center user_info 接口（结构化 media_id）
    try:
        response = await page.evaluate(_CREATOR_INFO_FETCH_JS, TOUTIAO_CREATOR_INFO_API)
        source = f"fetch:{response.get('status')}:{_redact_url(response.get('url', ''))}"
        found = parse_creator_info_response(response.get("text") or "", source)
        if found is not None:
            return found
    except Exception:
        logger.warning("Toutiao creator info fetch failed (async)", exc_info=True)

    # 2) DOM label 兜底
    try:
        dom = await page.evaluate(_CREATOR_INFO_DOM_JS, TOUTIAO_ID_LABEL)
        return normalize_dom_scan_result(dom, f"dom:{_redact_url(page.url or '')}")
    except Exception:
        logger.warning("Toutiao creator info DOM scan failed (async)", exc_info=True)
        return None


# register() 需在 ToutiaoDriver 定义之后调用，故 import 置于文件末尾
from server.app.modules.tasks.drivers import register  # noqa: E402

register(ToutiaoDriver())
