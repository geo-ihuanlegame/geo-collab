# AI 生文模块后端实现计划

## 进度总览

| Step | 内容 | 状态 |
|------|------|------|
| 1 | ORM Models（Skill / PromptTemplate / GenerationSession） | ✅ done |
| 2 | Alembic Migration（三张表） | ✅ done |
| 3 | Pydantic Schemas | ✅ done |
| 4 | CRUD 模块（skills / prompt_templates） | ✅ done |
| 5 | API Routes（skills / prompt_templates）+ main.py 注册 | ✅ done |
| 6 | md_converter.py（Markdown → Tiptap / HTML） | ✅ done |
| 7 | LangGraph Pipeline | ✅ done |
| 8 | Generation CRUD + Generation API Route | ✅ done |
| 9 | 依赖 + 配置（requirements.txt / config.py） | ✅ done |
| R | Review（typecheck + pytest） | ✅ done |
| M3 | 前端对接（React 页面） | ✅ done |

状态标记：⬜ pending → 🔄 in_progress → ✅ done

---

## 里程碑

- **M1**：Step 1-5 — Skills & Prompts 数据层 + CRUD API（不依赖 AI，可独立验证）
- **M2**：Step 6-9 — AI 生成引擎（md_converter + LangGraph pipeline + 生成会话 API）
- **Review**：Step R — typecheck + pytest，确认未破坏现有功能

---

## 现有代码规范（已确认）

- **ORM**：`Mapped[type] + mapped_column()` 风格，Base 来自 `server/app/db/base.py`
- **JSON 字段**：存为 `Text`，应用层手动 `json.dumps/loads`
- **Enum 字段**：`String(30) + CheckConstraint`（不用 SQLAlchemy Enum）
- **Migration**：`server/alembic/versions/00XX_*.py`，当前最新 0021，下一个用 **0022**
- **CRUD 函数**：调用 `db.flush()`，不 `commit`；commit 由 `get_db()` 统一管理
- **软删除**：`is_deleted: Mapped[bool]`，CRUD 查询过滤 `is_deleted=False`，delete 只做 `SET is_deleted=True`
- **路由注册**：在 `server/app/main.py:create_app()` 用 `app.include_router()`
- **异步执行**：`threading.Thread + bg_session_factory` 模式（同 tasks.py），生产 worker 轮询

---

## Step 1 — ORM Models

**状态**：⬜ pending

**新建** `server/app/models/skill.py`：

```python
from datetime import datetime
from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from server.app.core.time import utcnow
from server.app.db.base import Base

class Skill(Base):
    __tablename__ = "skills"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_path: Mapped[str] = mapped_column(String(500))   # GeoAppData/skills/{id}/
    file_stats: Mapped[str] = mapped_column(Text, default="{}")  # JSON: {references,skeletons,assets}
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

class PromptTemplate(Base):
    __tablename__ = "prompt_templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
```

**新建** `server/app/models/generation.py`：

```python
from datetime import datetime
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from server.app.core.time import utcnow
from server.app.db.base import Base

class GenerationSession(Base):
    __tablename__ = "generation_sessions"
    __table_args__ = (
        CheckConstraint("status in ('pending','running','done','failed')", name="ck_gen_sessions_status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    skill_id: Mapped[int | None] = mapped_column(ForeignKey("skills.id"), nullable=True)
    prompt_template_id: Mapped[int | None] = mapped_column(ForeignKey("prompt_templates.id"), nullable=True)
    extra_instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    article_ids: Mapped[str] = mapped_column(Text, default="[]")   # JSON array
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
```

**修改** `server/app/models/__init__.py`：追加 `Skill`、`PromptTemplate`、`GenerationSession` 的 import 和 `__all__` 条目。

---

## Step 2 — Alembic Migration

**状态**：⬜ pending  
**依赖**：Step 1

**新建** `server/alembic/versions/0022_ai_generation.py`：

- `down_revision = "0021"`
- `upgrade()`：按顺序 create_table `skills`、`prompt_templates`、`generation_sessions`
  - 每表追加 `mysql_engine="InnoDB", mysql_charset="utf8mb4"`
  - `skills` 和 `prompt_templates` 包含 `is_deleted` 列
- `downgrade()`：逆序 drop 三张表

---

## Step 3 — Pydantic Schemas

**状态**：⬜ pending  
**依赖**：无（纯 Pydantic，不 import ORM）

**新建** `server/app/schemas/skill.py`：
- `SkillRead`：id, name, description, file_stats(dict), is_enabled, created_at
- `SkillPatch`：is_enabled

**新建** `server/app/schemas/prompt_template.py`：
- `PromptTemplateCreate`：name, content
- `PromptTemplateUpdate`：name, content
- `PromptTemplateRead`：id, name, content, is_enabled, created_at, updated_at
- `PromptTemplatePatch`：is_enabled

**新建** `server/app/schemas/generation.py`：
- `GenerationSessionCreate`：skill_id, prompt_template_id, extra_instruction(optional)
- `GenerationSessionRead`：id, status, article_ids(list[int]), error_message, created_at, completed_at

---

## Step 4 — CRUD 模块

**状态**：⬜ pending  
**依赖**：Step 1

**新建** `server/app/modules/skills/__init__.py`（空）

**新建** `server/app/modules/skills/skill_Crud.py`：
- `list_skills(db) -> list[Skill]`：过滤 `is_deleted=False`
- `get_skill(db, skill_id) -> Skill | None`：过滤 `is_deleted=False`
- `create_skill(db, name, description, storage_path, file_stats) -> Skill`
- `patch_skill(db, skill, *, is_enabled) -> Skill`：`db.flush()`
- `delete_skill(db, skill) -> None`：`skill.is_deleted = True; db.flush()`（软删除，不删磁盘文件）

**新建** `server/app/modules/prompt_templates/__init__.py`（空）

**新建** `server/app/modules/prompt_templates/prompt_template_Crud.py`：
- `list_prompt_templates(db) -> list[PromptTemplate]`：过滤 `is_deleted=False`
- `get_prompt_template(db, template_id) -> PromptTemplate | None`：过滤 `is_deleted=False`
- `create_prompt_template(db, name, content) -> PromptTemplate`
- `update_prompt_template(db, template, name, content) -> PromptTemplate`
- `patch_prompt_template(db, template, *, is_enabled) -> PromptTemplate`
- `delete_prompt_template(db, template) -> None`：软删除

---

## Step 5 — API Routes

**状态**：⬜ pending  
**依赖**：Step 3（schemas）、Step 4（CRUD）

**新建** `server/app/api/routes/skills.py`：

```
POST   /api/skills          上传 ZIP（multipart UploadFile）
                            → 解压到 GeoAppData/skills/{id}/
                            → 解析 SKILL.md frontmatter（python-frontmatter 或正则）
                            → create_skill() → 返回 SkillRead
GET    /api/skills          list_skills() → list[SkillRead]
PATCH  /api/skills/{id}     patch_skill(is_enabled) → SkillRead
DELETE /api/skills/{id}     delete_skill()（软删除）→ 204
```

**新建** `server/app/api/routes/prompt_templates.py`：

```
GET    /api/prompt-templates          list
POST   /api/prompt-templates          create
PUT    /api/prompt-templates/{id}     update
PATCH  /api/prompt-templates/{id}     patch is_enabled
DELETE /api/prompt-templates/{id}     软删除 → 204
```

**修改** `server/app/main.py`：在 `create_app()` 注册两个路由，加 `Depends(get_current_user)`。

---

## Step 6 — md_converter.py

**状态**：⬜ pending  
**依赖**：无（纯工具函数）

**新建** `server/app/modules/ai_generation/__init__.py`（空）

**新建** `server/app/modules/ai_generation/md_converter.py`：

```python
def markdown_to_html(md: str) -> str:
    """MD → HTML，用 python-markdown 库"""
    import markdown
    return markdown.markdown(md, extensions=["extra"])

def markdown_to_tiptap(md: str) -> dict:
    """MD → Tiptap JSON doc，支持 paragraph / heading(1-3) / bulletList / listItem"""
    # 用 html.parser 将 markdown_to_html() 结果映射为 Tiptap 节点树
    # 返回 {"type": "doc", "content": [...]}
```

---

## Step 7 — LangGraph Pipeline

**状态**：⬜ pending  
**依赖**：Step 6（md_converter）、Step 8（generation_Crud + save_article_tool 签名）

**新建** `server/app/modules/ai_generation/pipeline.py`：

**图结构（三节点线性）**：
```
START → planner → parallel_write → finalize → END
```

**State 定义**：
```python
class PipelineState(TypedDict):
    session_id: int
    user_id: int
    skill_path: str          # GeoAppData/skills/{id}/
    prompt_content: str
    extra_instruction: str
    task_specs: list[dict]   # planner 输出
    article_ids: list[int]   # parallel_write 输出
    errors: list[str]
```

**Node 1 — planner**（顺序 LiteLLM 调用）：
- 读取 `{skill_path}/SKILL.md` + `{skill_path}/references/` 内所有 .md 构建 system prompt
- 调用 `litellm.completion()` 要求输出 JSON array 格式的 task_specs
- `task_spec: {title, topic, angle, skeleton_hint}`

**Node 2 — parallel_write**（ThreadPoolExecutor max_workers=4）：
- 每个 task_spec 调用 `_write_one(spec, state)` → LiteLLM 生成 MD 正文
- 内部调用 `save_article_tool()` → `md_converter` → `create_article()`
- 收集所有 article_id，写入 state.article_ids

**Node 3 — finalize**：
- 调用 `update_session_status(db, session_id, status="done", article_ids=[...])`

**LiteLLM 调用约定**：
```python
import litellm
response = litellm.completion(
    model=settings.ai_model,   # GEO_AI_MODEL，如 "claude-3-5-sonnet-20241022"
    messages=[{"role": "system", "content": ...}, {"role": "user", "content": ...}],
)
```

---

## Step 8 — Generation CRUD + Route

**状态**：⬜ pending  
**依赖**：Step 1（GenerationSession model）、Step 3（schemas）

**新建** `server/app/modules/ai_generation/generation_Crud.py`：
- `create_session(db, user_id, skill_id, prompt_template_id, extra_instruction) -> GenerationSession`
- `get_session(db, session_id) -> GenerationSession | None`
- `update_session_status(db, session_id, status, article_ids, error_message) -> None`

**新建** `server/app/api/routes/generation.py`：

```
POST  /api/generation/sessions      → 202 {session_id, status: "pending"}
GET   /api/generation/sessions/{id} → {status, article_ids, error_message, completed_at}
```

异步执行用 `threading.Thread + bg_session_factory`，与 `tasks.py` 完全一致。

**修改** `server/app/main.py`：注册 generation 路由，prefix `/api/generation`。

---

## Step 9 — 依赖 + 配置

**状态**：⬜ pending  
**依赖**：无（可最先做）

**修改** `requirements.txt`，追加：
```
litellm
langgraph
markdown
python-frontmatter
```

**修改** `server/app/core/config.py`，追加：
```python
ai_model: str = "claude-3-5-sonnet-20241022"   # GEO_AI_MODEL
ai_api_key: str = ""                             # GEO_AI_API_KEY（通过 litellm 环境变量注入）
```

---

## Step R — Review

**状态**：⬜ pending  
**依赖**：Step 1-9 全部完成

```bash
pnpm --filter @geo/web typecheck          # 前端类型检查（确认无破坏）
pytest server/tests/ -q                   # 全量 pytest（确认现有用例通过）
alembic upgrade head                      # 确认三张表创建成功
```

手动冒烟：
1. POST `/api/skills`（上传 ZIP）→ 验证 `skills` 表有记录
2. GET `/api/skills` → 确认返回 SkillRead 列表
3. DELETE `/api/skills/{id}` → 确认 `is_deleted=True`，记录仍在库
4. POST `/api/prompt-templates` / PUT / DELETE 同上
5. POST `/api/generation/sessions` → 轮询 GET 直到 `status=done` → 验证 `articles` 表有新记录

---

## 关键文件清单

### 新建
| 文件 | 说明 |
|------|------|
| `server/app/models/skill.py` | Skill, PromptTemplate ORM（含 is_deleted） |
| `server/app/models/generation.py` | GenerationSession ORM |
| `server/alembic/versions/0022_ai_generation.py` | 三张表 migration |
| `server/app/schemas/skill.py` | Skill schemas |
| `server/app/schemas/prompt_template.py` | PromptTemplate schemas |
| `server/app/schemas/generation.py` | GenerationSession schemas |
| `server/app/modules/skills/__init__.py` | |
| `server/app/modules/skills/skill_Crud.py` | Skill CRUD（软删除） |
| `server/app/modules/prompt_templates/__init__.py` | |
| `server/app/modules/prompt_templates/prompt_template_Crud.py` | PromptTemplate CRUD（软删除） |
| `server/app/modules/ai_generation/__init__.py` | |
| `server/app/modules/ai_generation/md_converter.py` | Markdown → Tiptap/HTML |
| `server/app/modules/ai_generation/pipeline.py` | LangGraph 三节点管道 |
| `server/app/modules/ai_generation/generation_Crud.py` | 会话 CRUD |
| `server/app/api/routes/skills.py` | Skills API |
| `server/app/api/routes/prompt_templates.py` | Prompts API |
| `server/app/api/routes/generation.py` | Generation API |

### 修改
| 文件 | 改动 |
|------|------|
| `server/app/models/__init__.py` | 追加三个新 Model |
| `server/app/main.py` | 注册三个新路由 |
| `server/app/core/config.py` | 追加 ai_model, ai_api_key |
| `requirements.txt` | 追加四个新依赖 |
