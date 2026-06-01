# AI 格式调整功能 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在文章编辑器中添加「AI 格式调整」按钮，点击后由 LiteLLM 识别正文中应为小标题的段落并自动回写 Tiptap 内容；文章在 AI 处理期间加锁，禁止编辑、删除、发布。

**Architecture:** 后端新增 `ai_format.py` 服务（LiteLLM 调用 + Tiptap JSON 修改）和 `POST /api/articles/{id}/ai-format` 端点（返回 202，后台线程执行）；Article 模型加 `ai_checking`/`ai_checking_started_at` 字段（120s 超时自动解锁）；前端在 `EditorToolbar` 加按钮，`ContentWorkspace` 轮询锁状态并在完成后重载编辑器内容。

**Tech Stack:** LiteLLM, FastAPI, SQLAlchemy/Alembic, React 19, Tiptap, pydantic-settings

**前置条件:** Plan A（toutiao-rich-format）已完成，`heading_level` 字段已在 BodySegment 中存在。

---

## 文件映射

| 操作 | 文件 |
|------|------|
| 修改 | `server/app/modules/articles/models.py` |
| 新建 | `server/app/db/migrations/versions/xxxx_add_ai_checking_to_articles.py` |
| 新建 | `server/app/modules/articles/ai_format.py` |
| 修改 | `server/app/modules/articles/router.py` |
| 修改 | `server/app/modules/articles/schemas.py` |
| 新建 | `server/tests/test_ai_format.py` |
| 修改 | `web/src/api/articles.ts`（或同目录下 articles 相关 API 文件）|
| 修改 | `web/src/features/content/EditorToolbar.tsx` |
| 修改 | `web/src/features/content/ContentWorkspace.tsx` |

---

### Task 1: Article 模型添加锁字段 + Alembic 迁移

**Files:**
- Modify: `server/app/modules/articles/models.py`
- Create: alembic migration

- [ ] **Step 1: 更新 Article 模型**

在 `server/app/modules/articles/models.py` 的 `Article` 类中，在 `is_deleted` 字段后追加：

```python
from datetime import datetime
# （datetime 可能已导入，检查后按需添加）

ai_checking: Mapped[bool] = mapped_column(
    Boolean, nullable=False, default=False, server_default="0"
)
ai_checking_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
```

- [ ] **Step 2: 生成 Alembic 迁移**

```bash
conda activate geo_xzpt
alembic revision --autogenerate -m "add ai_checking to articles"
```

检查生成的迁移文件，确认包含：
```python
op.add_column('articles', sa.Column('ai_checking', sa.Boolean(), server_default='0', nullable=False))
op.add_column('articles', sa.Column('ai_checking_started_at', sa.DateTime(), nullable=True))
```

- [ ] **Step 3: 执行迁移**

```bash
alembic upgrade head
```

- [ ] **Step 4: Commit**

```bash
git add server/app/modules/articles/models.py
git add server/app/db/migrations/versions/
git commit -m "feat: add ai_checking lock fields to Article model"
```

---

### Task 2: 在 ArticleRead schema 暴露 ai_checking 字段

**Files:**
- Modify: `server/app/modules/articles/schemas.py`

- [ ] **Step 1: 在 ArticleRead 中添加字段**

在 `server/app/modules/articles/schemas.py` 的 `ArticleRead`（或等价的响应 schema）类中追加：

```python
ai_checking: bool = False
```

- [ ] **Step 2: 运行后端测试，确认无破坏**

```bash
pytest server/tests/ -q
```

- [ ] **Step 3: Commit**

```bash
git add server/app/modules/articles/schemas.py
git commit -m "feat: expose ai_checking in ArticleRead schema"
```

---

### Task 3: 添加锁检查辅助函数，更新编辑/删除端点

**Files:**
- Modify: `server/app/modules/articles/router.py`

- [ ] **Step 1: 写失败测试**

```python
# server/tests/test_ai_format.py
import pytest
from server.tests.conftest import build_test_app   # 按项目实际 conftest 路径调整


def test_edit_locked_article_returns_409(monkeypatch):
    with build_test_app(monkeypatch) as (client, _):
        # 创建文章
        r = client.post("/api/articles", json={"title": "test", "content_json": {}, "content_html": "", "plain_text": "", "word_count": 0})
        article_id = r.json()["id"]
        # 手动设置锁
        r2 = client.patch(f"/api/articles/{article_id}", json={"ai_checking": True})  # 直接 DB 操作在此省略，测试通过 DB session 设置
        # 实际测试：调用 PUT 端点时应返回 409
        # （具体实现参考项目现有 build_test_app 用法）
```

> 注意：此测试需参考项目中 `build_test_app` 的实际用法（见 `server/tests/conftest.py`）补全 DB 操作部分。

- [ ] **Step 2: 在 articles.py 添加锁检查函数**

在 `server/app/modules/articles/router.py` 文件顶部的辅助函数区域添加：

```python
from datetime import datetime, timezone

_AI_CHECK_TIMEOUT_SECONDS = 120


def _check_not_ai_locked(article: Any) -> None:
    """如果文章正在 AI 检查且未超时，抛出 ConflictError。"""
    if not article.ai_checking:
        return
    started = article.ai_checking_started_at
    if started is None:
        return
    elapsed = (datetime.now(timezone.utc).replace(tzinfo=None) - started).total_seconds()
    if elapsed >= _AI_CHECK_TIMEOUT_SECONDS:
        return  # 超时视为已解锁
    from server.app.shared.errors import ConflictError
    raise ConflictError("文章正在进行 AI 格式调整，请稍后再试")
```

- [ ] **Step 3: 在 update_article_endpoint 和 delete_article_endpoint 中调用锁检查**

`update_article_endpoint`（PUT `/{article_id}`）在 `_verify_article_ownership` 之后加：

```python
_check_not_ai_locked(article)
```

`delete_article_endpoint`（DELETE `/{article_id}`）同样加：

```python
_check_not_ai_locked(article)
```

- [ ] **Step 4: 运行后端测试**

```bash
pytest server/tests/ -q
```

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/articles/router.py
git commit -m "feat: lock article against edit/delete while ai_checking is active"
```

---

### Task 4: 安装 LiteLLM，创建 ai_format.py 服务

**Files:**
- Create: `server/app/modules/articles/ai_format.py`

- [ ] **Step 1: 安装 LiteLLM**

```bash
conda activate geo_xzpt
pip install litellm
```

将 `litellm` 添加到 `server/requirements.txt`（或等价依赖文件）。

- [ ] **Step 2: 创建 ai_format.py**

```python
# server/app/modules/articles/ai_format.py
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from server.app.core.config import get_settings
from server.app.modules.articles.parser import loads_content_json, dumps_content_json

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a document formatter. Given a numbered list of article paragraphs, "
    "identify which indices (0-based) should be formatted as H1 headings. "
    "Headings are short topic-introducing phrases, typically under 20 characters. "
    'Respond ONLY with valid JSON: {"heading_indices": [0, 3]} '
    'or {"heading_indices": []} if none.'
)


def _top_level_paragraphs(content_json: dict) -> list[tuple[int, dict]]:
    content = content_json.get("content") or []
    return [
        (i, node)
        for i, node in enumerate(content)
        if isinstance(node, dict) and node.get("type") == "paragraph"
    ]


def _paragraph_text(node: dict) -> str:
    return "".join(
        child.get("text", "")
        for child in (node.get("content") or [])
        if isinstance(child, dict) and child.get("type") == "text"
    )


def _to_heading(node: dict, level: int = 1) -> dict:
    return {"type": "heading", "attrs": {"level": level}, "content": node.get("content", [])}


def _apply_headings(content_json: dict, heading_indices: set[int]) -> dict:
    content = list(content_json.get("content") or [])
    for i, node in enumerate(content):
        if i in heading_indices and isinstance(node, dict) and node.get("type") == "paragraph":
            content[i] = _to_heading(node)
    return {**content_json, "content": content}


def run_ai_format(article_id: int) -> None:
    """AI 格式检查主函数，在后台线程中调用。完成后自动解锁文章。"""
    from server.app.db.session import SessionLocal

    db = SessionLocal()
    try:
        from server.app.modules.articles.service import get_article

        article = get_article(db, article_id)
        if article is None or article.is_deleted:
            return

        content_json = loads_content_json(article.content_json)
        paragraphs = _top_level_paragraphs(content_json)
        if not paragraphs:
            return

        listing = "\n".join(f"{i}: {_paragraph_text(node)}" for i, node in paragraphs)

        settings = get_settings()
        from litellm import completion

        response = completion(
            model=settings.ai_model,
            api_key=settings.ai_api_key or None,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": listing},
            ],
            temperature=0,
        )

        raw = (response.choices[0].message.content or "").strip()
        parsed = json.loads(raw)
        heading_indices = set(parsed.get("heading_indices", []))

        if heading_indices:
            new_content_json = _apply_headings(content_json, heading_indices)
            article.content_json = dumps_content_json(new_content_json)
            article.version += 1
            article.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.commit()
            logger.info("ai_format applied %d headings to article %s", len(heading_indices), article_id)

    except Exception:
        logger.exception("ai_format failed for article %s", article_id)
    finally:
        try:
            from server.app.modules.articles.service import get_article as _get

            article = _get(db, article_id)
            if article is not None:
                article.ai_checking = False
                article.ai_checking_started_at = None
                db.commit()
        except Exception:
            logger.exception("ai_format unlock failed for article %s", article_id)
        db.close()
```

- [ ] **Step 3: 写单元测试（隔离 LiteLLM 调用）**

```python
# server/tests/test_ai_format.py（追加）
import json
from unittest.mock import MagicMock, patch
from server.app.modules.articles.ai_format import (
    _top_level_paragraphs, _paragraph_text, _apply_headings
)


def test_top_level_paragraphs_returns_only_paragraphs():
    doc = {
        "type": "doc",
        "content": [
            {"type": "heading", "attrs": {"level": 1}, "content": []},
            {"type": "paragraph", "content": [{"type": "text", "text": "Hello"}]},
        ],
    }
    result = _top_level_paragraphs(doc)
    assert len(result) == 1
    assert result[0][0] == 1  # index in doc content


def test_paragraph_text_joins_text_nodes():
    node = {"type": "paragraph", "content": [
        {"type": "text", "text": "Hello "},
        {"type": "text", "text": "World"},
    ]}
    assert _paragraph_text(node) == "Hello World"


def test_apply_headings_converts_paragraph_to_h1():
    doc = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Title"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "Body"}]},
        ],
    }
    result = _apply_headings(doc, heading_indices={0})
    assert result["content"][0]["type"] == "heading"
    assert result["content"][0]["attrs"]["level"] == 1
    assert result["content"][1]["type"] == "paragraph"
```

- [ ] **Step 4: 运行测试，确认通过**

```bash
pytest server/tests/test_ai_format.py -v
```

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/articles/ai_format.py server/tests/test_ai_format.py
git commit -m "feat: add ai_format service for heading detection via LiteLLM"
```

---

### Task 5: 添加 POST /api/articles/{id}/ai-format 端点

**Files:**
- Modify: `server/app/modules/articles/router.py`

- [ ] **Step 1: 在 articles.py 末尾追加端点**

```python
import threading
from datetime import datetime, timezone


@router.post("/{article_id}/ai-format", status_code=202)
def trigger_ai_format_endpoint(
    article_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    article = _verify_article_ownership(get_article(db, article_id), current_user)
    _check_not_ai_locked(article)

    article.ai_checking = True
    article.ai_checking_started_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()

    def _run() -> None:
        from server.app.modules.articles.ai_format import run_ai_format
        run_ai_format(article_id)

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started"}
```

- [ ] **Step 2: 运行后端测试**

```bash
pytest server/tests/ -q
```

- [ ] **Step 3: 手动冒烟测试（可选）**

启动后端 `uvicorn server.app.main:app --reload --host 127.0.0.1 --port 8000`，确认端点可达。

- [ ] **Step 4: Commit**

```bash
git add server/app/modules/articles/router.py
git commit -m "feat: add POST /api/articles/{id}/ai-format endpoint"
```

---

### Task 6: 前端 — 添加 triggerAiFormat API 函数

**Files:**
- Modify: `web/src/api/articles.ts`（根据实际文件路径调整）

- [ ] **Step 1: 在 articles API 文件中添加函数和类型**

找到 `web/src/api/` 目录下处理文章请求的文件，添加：

```typescript
// 在 ArticleDetail 或等价接口中追加字段
export interface ArticleDetail {
  // ...existing fields...
  ai_checking: boolean;
}

export async function triggerAiFormat(articleId: number): Promise<void> {
  await fetch(`/api/articles/${articleId}/ai-format`, {
    method: "POST",
    credentials: "include",
  });
}
```

- [ ] **Step 2: 运行 TypeScript 类型检查**

```bash
pnpm --filter @geo/web typecheck
```

预期：无新错误

- [ ] **Step 3: Commit**

```bash
git add web/src/api/
git commit -m "feat: add triggerAiFormat API client function"
```

---

### Task 7: 前端 — EditorToolbar 添加 AI 格式调整按钮

**Files:**
- Modify: `web/src/features/content/EditorToolbar.tsx`

- [ ] **Step 1: 在 EditorToolbar 添加按钮**

在 `EditorToolbar.tsx` 中，参考现有 H1/H2 按钮的样式，在工具栏末尾或适当位置追加：

```tsx
interface EditorToolbarProps {
  // ...existing props...
  articleId: number;
  aiChecking: boolean;
  onAiFormat: () => void;
}

// 在工具栏 JSX 中添加（参考已有按钮风格）:
<button
  onClick={onAiFormat}
  disabled={aiChecking}
  title="AI 格式调整"
  className={/* 参考已有 button className */}
>
  {aiChecking ? "AI 调整中…" : "AI 格式"}
</button>
```

- [ ] **Step 2: 运行类型检查**

```bash
pnpm --filter @geo/web typecheck
```

- [ ] **Step 3: Commit**

```bash
git add web/src/features/content/EditorToolbar.tsx
git commit -m "feat: add AI format button to editor toolbar"
```

---

### Task 8: 前端 — ContentWorkspace 接入轮询、锁状态、内容重载

**Files:**
- Modify: `web/src/features/content/ContentWorkspace.tsx`

- [ ] **Step 1: 添加轮询逻辑和锁状态**

在 `ContentWorkspace.tsx` 中：

```tsx
import { triggerAiFormat } from "../../api/articles";

// 在组件 state 中添加
const [aiChecking, setAiChecking] = useState(article.ai_checking ?? false);

// 轮询：ai_checking 为 true 时每 2s 请求一次文章状态
useEffect(() => {
  if (!aiChecking) return;
  const interval = setInterval(async () => {
    const res = await fetch(`/api/articles/${article.id}`, { credentials: "include" });
    const data = await res.json();
    if (!data.ai_checking) {
      setAiChecking(false);
      // 重载编辑器内容
      editor?.commands.setContent(data.content_json);
    }
  }, 2000);
  return () => clearInterval(interval);
}, [aiChecking, article.id, editor]);

// 触发函数
const handleAiFormat = async () => {
  await triggerAiFormat(article.id);
  setAiChecking(true);
};
```

- [ ] **Step 2: 传 props 给 EditorToolbar**

```tsx
<EditorToolbar
  // ...existing props...
  articleId={article.id}
  aiChecking={aiChecking}
  onAiFormat={handleAiFormat}
/>
```

- [ ] **Step 3: 锁定状态下禁用编辑器**

在编辑器区域，当 `aiChecking` 为 true 时显示遮罩或 `editable={false}`：

```tsx
// 在 useEditor 的 editable 选项中
editable: !aiChecking,
```

- [ ] **Step 4: 运行类型检查和构建**

```bash
pnpm --filter @geo/web typecheck
pnpm --filter @geo/web build
```

- [ ] **Step 5: 启动前端验证功能**

```bash
pnpm --filter @geo/web dev
```

在浏览器中打开文章编辑器，点击「AI 格式」按钮，确认：
1. 按钮变为「AI 调整中…」并禁用
2. 编辑器进入只读模式
3. 约数秒后（LiteLLM 返回）编辑器内容刷新，出现标题格式
4. 按钮恢复正常

- [ ] **Step 6: Commit**

```bash
git add web/src/features/content/ContentWorkspace.tsx
git commit -m "feat: add ai_checking state, polling, and editor lock to ContentWorkspace"
```
