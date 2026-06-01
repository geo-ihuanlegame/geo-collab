# Toutiao 富格式发布 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 ToutiaoDriver 在发布时保留 Tiptap 编辑器里的标题（H1/H2 → `h1.pgc-h-forward-slash`）和加粗格式，不再把所有内容打平成纯文本。

**Architecture:** 分两层改动。第一层：扩展 `BodySegment` 携带 `heading_level` 和 `bold` 元数据，更新 `_append_segments` 在解析时传递标题上下文，更新 `_compact` 不跨格式边界合并。第二层：将 ToutiaoDriver 的正文插入策略从"一次性粘贴全文"改为"逐段插入"，对标题段落按 Ctrl+Alt+1，对加粗文字夹 Ctrl+B，图片按原有上传流程就地插入。

**Tech Stack:** Python dataclasses, Playwright, ProseMirror (Toutiao 用 Syllepsis 封装), pytest

---

## 文件映射

| 操作 | 文件 |
|------|------|
| 修改 | `server/app/modules/articles/parser.py` |
| 新建 | `server/tests/test_tiptap_parser.py` |
| 修改 | `server/app/modules/tasks/drivers/toutiao.py` |
| 新建 | `server/tests/test_toutiao_group_paragraphs.py` |

---

### Task 1: 为 BodySegment 添加 bold 和 heading_level 字段

**Files:**
- Modify: `server/app/modules/articles/parser.py:10-15`
- Create: `server/tests/test_tiptap_parser.py`

- [ ] **Step 1: 写失败测试**

```python
# server/tests/test_tiptap_parser.py
import json
import pytest
from server.app.modules.articles.parser import BodySegment, parse_body_segments


def _article(content_json: dict):
    class A:
        content_html = ""
        plain_text = ""
    a = A()
    a.content_json = json.dumps(content_json)
    return a


def test_heading_segment_has_heading_level():
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "heading",
                "attrs": {"level": 1},
                "content": [{"type": "text", "text": "My Title"}],
            }
        ],
    }
    segs = parse_body_segments(_article(doc))
    text_segs = [s for s in segs if s.kind == "text" and s.text != "\n"]
    assert text_segs[0].heading_level == 1

def test_bold_mark_sets_bold_true():
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "bold", "marks": [{"type": "bold"}]},
                ],
            }
        ],
    }
    segs = parse_body_segments(_article(doc))
    text_segs = [s for s in segs if s.kind == "text" and s.text != "\n"]
    assert text_segs[0].bold is True
```

- [ ] **Step 2: 运行测试，确认失败**

```
pytest server/tests/test_tiptap_parser.py -v
```

预期：`AttributeError: 'BodySegment' object has no attribute 'heading_level'`

- [ ] **Step 3: 更新 BodySegment**

将 `server/app/modules/articles/parser.py` 第 10-15 行替换为：

```python
@dataclass(frozen=True)
class BodySegment:
    kind: str                           # "text" | "image"
    text: str = ""
    bold: bool = False                  # text 节点有 bold mark
    heading_level: int | None = None    # 来自 heading 节点时为 1 或 2
    image_path: Path | None = None
    image_asset_id: str | None = None
```

- [ ] **Step 4: 运行测试，确认仍失败**（字段有了但值还没赋）

```
pytest server/tests/test_tiptap_parser.py -v
```

预期：`assert None == 1`（heading_level 默认还是 None）

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/articles/parser.py server/tests/test_tiptap_parser.py
git commit -m "feat: add bold and heading_level fields to BodySegment"
```

---

### Task 2: 更新 _append_segments 传递标题上下文

**Files:**
- Modify: `server/app/modules/articles/parser.py:78-114`

- [ ] **Step 1: 重写 _append_segments**

用下面的实现完整替换 `_append_segments` 函数（78-114 行）：

```python
def _append_segments(
    node: Any, segments: list[BodySegment], depth: int = 0, _hlevel: int | None = None
) -> None:
    if isinstance(node, list):
        for child in node:
            _append_segments(child, segments, depth, _hlevel)
        return
    if not isinstance(node, dict):
        return

    node_type = node.get("type")

    if node_type == "text":
        text = node.get("text")
        if isinstance(text, str) and text:
            marks = node.get("marks") or []
            is_bold = any(isinstance(m, dict) and m.get("type") == "bold" for m in marks)
            segments.append(BodySegment(kind="text", text=text, bold=is_bold, heading_level=_hlevel))
        return

    if node_type == "hardBreak":
        segments.append(BodySegment(kind="text", text="\n"))
        return

    if node_type == "image":
        asset_id = _asset_id_from_image_node(node)
        if asset_id:
            segments.append(BodySegment(kind="image", image_asset_id=asset_id))
        return

    if node_type == "heading":
        level = int((node.get("attrs") or {}).get("level", 1))
        content = node.get("content")
        if isinstance(content, list):
            for child in content:
                _append_segments(child, segments, depth, _hlevel=level)
        segments.append(BodySegment(kind="text", text="\n"))
        return

    content = node.get("content")
    if isinstance(content, list):
        for child in content:
            _append_segments(
                child,
                segments,
                depth + (1 if node_type in ("orderedList", "bulletList") else 0),
                _hlevel=None,
            )

    if node_type == "paragraph":
        segments.append(BodySegment(kind="text", text="\n"))
```

- [ ] **Step 2: 运行测试，确认通过**

```
pytest server/tests/test_tiptap_parser.py -v
```

预期：2 passed

- [ ] **Step 3: Commit**

```bash
git add server/app/modules/articles/parser.py
git commit -m "feat: propagate heading level context in _append_segments"
```

---

### Task 3: 更新 _compact 不跨 bold/heading_level 边界合并

**Files:**
- Modify: `server/app/modules/articles/parser.py:117-135`
- Modify: `server/tests/test_tiptap_parser.py`

- [ ] **Step 1: 补充测试**

在 `server/tests/test_tiptap_parser.py` 末尾追加：

```python
from server.app.modules.articles.parser import _compact


def test_compact_does_not_merge_across_bold_boundary():
    segs = [
        BodySegment(kind="text", text="a", bold=False),
        BodySegment(kind="text", text="b", bold=True),
        BodySegment(kind="text", text="c", bold=False),
    ]
    result = _compact(segs)
    assert len(result) == 3


def test_compact_merges_same_bold_adjacent():
    segs = [
        BodySegment(kind="text", text="a", bold=True),
        BodySegment(kind="text", text="b", bold=True),
    ]
    result = _compact(segs)
    assert len(result) == 1
    assert result[0].text == "ab"
    assert result[0].bold is True


def test_compact_does_not_merge_across_heading_level_boundary():
    segs = [
        BodySegment(kind="text", text="a", heading_level=1),
        BodySegment(kind="text", text="b", heading_level=None),
    ]
    result = _compact(segs)
    assert len(result) == 2
```

- [ ] **Step 2: 运行，确认失败**

```
pytest server/tests/test_tiptap_parser.py::test_compact_does_not_merge_across_bold_boundary -v
```

预期：FAIL（当前 compact 只判断 kind 和 text!="\n"，会错误合并）

- [ ] **Step 3: 重写 _compact**

替换 `_compact` 函数（117-135 行）：

```python
def _compact(segments: list[BodySegment]) -> list[BodySegment]:
    compacted: list[BodySegment] = []
    for seg in segments:
        if seg.kind == "text":
            if not seg.text:
                continue
            if seg.text == "\n":
                compacted.append(seg)
                continue
            if (
                compacted
                and compacted[-1].kind == "text"
                and compacted[-1].text != "\n"
                and compacted[-1].bold == seg.bold
                and compacted[-1].heading_level == seg.heading_level
            ):
                prev = compacted.pop()
                compacted.append(
                    BodySegment(
                        kind="text",
                        text=prev.text + seg.text,
                        bold=prev.bold,
                        heading_level=prev.heading_level,
                    )
                )
            else:
                compacted.append(seg)
        else:
            compacted.append(seg)
    while compacted and compacted[-1].kind == "text" and not compacted[-1].text.strip():
        compacted.pop()
    return compacted
```

- [ ] **Step 4: 运行全部测试，确认通过**

```
pytest server/tests/test_tiptap_parser.py -v
```

预期：全部 passed

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/articles/parser.py server/tests/test_tiptap_parser.py
git commit -m "feat: compact respects bold and heading_level boundaries"
```

---

### Task 4: 在 toutiao.py 添加 BodyParagraph 和 _group_paragraphs

**Files:**
- Modify: `server/app/modules/tasks/drivers/toutiao.py`
- Create: `server/tests/test_toutiao_group_paragraphs.py`

- [ ] **Step 1: 写失败测试**

```python
# server/tests/test_toutiao_group_paragraphs.py
from pathlib import Path
from server.app.modules.articles.parser import BodySegment
from server.app.modules.tasks.drivers.toutiao import BodyParagraph, _group_paragraphs


def test_heading_becomes_heading_paragraph():
    segs = [
        BodySegment(kind="text", text="Title", heading_level=1),
        BodySegment(kind="text", text="\n"),
        BodySegment(kind="text", text="Body"),
        BodySegment(kind="text", text="\n"),
    ]
    paras = _group_paragraphs(segs)
    assert len(paras) == 2
    assert paras[0].kind == "heading"
    assert paras[0].heading_level == 1
    assert paras[0].runs == (("Title", False),)
    assert paras[1].kind == "text"
    assert paras[1].runs == (("Body", False),)


def test_bold_runs_preserved_in_paragraph():
    segs = [
        BodySegment(kind="text", text="plain ", bold=False),
        BodySegment(kind="text", text="bold", bold=True),
        BodySegment(kind="text", text="\n"),
    ]
    paras = _group_paragraphs(segs)
    assert len(paras) == 1
    assert paras[0].runs == (("plain ", False), ("bold", True))


def test_image_flushes_text_and_becomes_own_paragraph():
    segs = [
        BodySegment(kind="text", text="Before"),
        BodySegment(kind="text", text="\n"),
        BodySegment(kind="image", image_asset_id="abc", image_path=Path("/tmp/img.jpg")),
        BodySegment(kind="text", text="\n"),
        BodySegment(kind="text", text="After"),
    ]
    paras = _group_paragraphs(segs)
    assert len(paras) == 3
    assert paras[0].kind == "text"
    assert paras[1].kind == "image"
    assert paras[1].image_asset_id == "abc"
    assert paras[2].kind == "text"


def test_blank_only_paragraph_is_skipped():
    segs = [
        BodySegment(kind="text", text="   "),
        BodySegment(kind="text", text="\n"),
        BodySegment(kind="text", text="Real"),
    ]
    paras = _group_paragraphs(segs)
    assert len(paras) == 1
    assert paras[0].runs == (("Real", False),)
```

- [ ] **Step 2: 运行，确认失败**

```
pytest server/tests/test_toutiao_group_paragraphs.py -v
```

预期：`ImportError: cannot import name 'BodyParagraph'`

- [ ] **Step 3: 在 toutiao.py 中添加 BodyParagraph 和 _group_paragraphs**

在 `toutiao.py` 第 27 行（`PublishFillResult = PublishResult` 之后）插入：

```python
@dataclass(frozen=True)
class BodyParagraph:
    kind: str  # "text" | "heading" | "image"
    runs: tuple[tuple[str, bool], ...] = ()   # (text, is_bold)
    heading_level: int | None = None
    image_path: Path | None = None
    image_asset_id: str | None = None


def _group_paragraphs(segments: list[BodySegment]) -> list[BodyParagraph]:
    """Group flat BodySegments into logical paragraphs for sequential insertion."""
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
                BodyParagraph(kind="image", image_path=seg.image_path, image_asset_id=seg.image_asset_id)
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
```

- [ ] **Step 4: 运行测试，确认通过**

```
pytest server/tests/test_toutiao_group_paragraphs.py -v
```

预期：4 passed

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/tasks/drivers/toutiao.py server/tests/test_toutiao_group_paragraphs.py
git commit -m "feat: add BodyParagraph and _group_paragraphs to toutiao driver"
```

---

### Task 5: 添加逐段插入辅助函数

**Files:**
- Modify: `server/app/modules/tasks/drivers/toutiao.py`

- [ ] **Step 1: 在 toutiao.py 中添加三个辅助函数**

在 `_insert_body_text` 函数（当前第 342 行）**之后**插入：

```python
def _insert_runs(page: Any, runs: tuple[tuple[str, bool], ...]) -> None:
    """逐段插入文字，支持 bold 切换。"""
    for text, is_bold in runs:
        if not text:
            continue
        if is_bold:
            page.keyboard.press("Control+b")
            page.wait_for_timeout(50)
        page.evaluate("text => navigator.clipboard.writeText(text)", text)
        page.wait_for_timeout(50)
        page.keyboard.press("Control+v")
        page.wait_for_timeout(100)
        if is_bold:
            page.keyboard.press("Control+b")
            page.wait_for_timeout(50)


def _insert_text_paragraph(page: Any, runs: tuple[tuple[str, bool], ...]) -> None:
    _insert_runs(page, runs)


def _insert_heading_paragraph(page: Any, runs: tuple[tuple[str, bool], ...]) -> None:
    """在当前空行应用小标题格式（Ctrl+Alt+1），再插入文字。"""
    page.keyboard.press("Control+Alt+1")
    page.wait_for_timeout(150)
    _insert_runs(page, runs)
```

- [ ] **Step 2: 运行已有测试，确认没有破坏**

```
pytest server/tests/test_toutiao_group_paragraphs.py server/tests/test_tiptap_parser.py -v
```

预期：全部 passed

- [ ] **Step 3: Commit**

```bash
git add server/app/modules/tasks/drivers/toutiao.py
git commit -m "feat: add paragraph insertion helpers to toutiao driver"
```

---

### Task 6: 重写 _fill_body，移除旧的标记替换逻辑

**Files:**
- Modify: `server/app/modules/tasks/drivers/toutiao.py:197-352`

- [ ] **Step 1: 替换 _fill_body 函数（197-222 行）**

```python
def _fill_body(page: Any, segments: list[BodySegment]) -> None:
    """逐段插入正文：标题用 Ctrl+Alt+1，加粗用 Ctrl+B，图片用原有上传流程。"""
    if not segments:
        raise ToutiaoPublishError("文章正文为空")

    paragraphs = _group_paragraphs(segments)
    if not paragraphs:
        raise ToutiaoPublishError("Article body is empty")

    record_publish_diagnostic(f"body fill: {len(paragraphs)} paragraphs")
    _dismiss_blocking_popups(page)
    _clear_body_editor(page)
    _focus_body_editor(page)

    for i, para in enumerate(paragraphs):
        is_last = i == len(paragraphs) - 1

        if para.kind == "image":
            _dismiss_blocking_popups(page)
            record_publish_diagnostic(f"body image upload: asset_id={para.image_asset_id}")
            _paste_body_image_path(page, para.image_path, para.image_asset_id)
            if not is_last:
                _focus_body_editor(page)
                page.keyboard.press("Enter")
                page.wait_for_timeout(100)
        elif para.kind == "heading":
            _insert_heading_paragraph(page, para.runs)
            if not is_last:
                page.keyboard.press("Enter")
                page.wait_for_timeout(100)
        else:
            _insert_text_paragraph(page, para.runs)
            if not is_last:
                page.keyboard.press("Enter")
                page.wait_for_timeout(100)
```

- [ ] **Step 2: 删除以下旧函数**（不再调用，安全删除）

- `_build_body_fill_plan`（225-250 行）
- `_replace_body_marker_with_image`（279-285 行）
- `_select_body_marker`（288-313 行）
- `_body_marker_exists`（316-326 行）
- `_assert_no_body_markers`（329-339 行）
- `_insert_body_text`（342-352 行）
- `BodyFillPlan` dataclass（38-43 行）
- `BodyImageSlot` dataclass（31-36 行）

- [ ] **Step 3: 运行全部相关测试**

```
pytest server/tests/test_tiptap_parser.py server/tests/test_toutiao_group_paragraphs.py -v
```

预期：全部 passed

- [ ] **Step 4: 运行后端完整测试套件**

```bash
conda activate geo_xzpt
pytest server/tests/ -q
```

预期：已有测试无新失败

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/tasks/drivers/toutiao.py
git commit -m "feat: rewrite toutiao body insertion to support headings and bold formatting"
```
