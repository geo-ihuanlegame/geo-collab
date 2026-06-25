# `/goal` Loop Skill 主推栏目 id 发现工具 · 设计

- 状态：设计稿（v0），待 review 后进入 writing-plans 阶段
- 日期：2026-06-25
- 上游：[`2026-06-24-loop-skill-distribution-design.md`](./2026-06-24-loop-skill-distribution-design.md)（PR #147 已合，引入 `<REPLACE_ME>` 占位）+ [`2026-06-25-loop-illustration-and-i18n-fix-design.md`](./2026-06-25-loop-illustration-and-i18n-fix-design.md)（PR #149 已合，把 `<REPLACE_ME>` 推到 main_category_id 这一行）
- 受众：实施 plan 评审 + Loop 使用者
- 不动的部分：/goal 主架构 / 配图链路 / 鉴权 / bundle 分发机制
- 动的部分：让 Claude 帮使用者**自助查 + 自助填** writer skill 矩阵段里的 `main_category_id`，把当前"切到 GEO 后台手抄 id"的 friction 消掉

---

## 0. 一句话

新增 MCP 工具 `list_stock_categories(kind="main")` 让 Claude 能查图库栏目候选；
使用者只要对 Claude 说"帮我查下主推栏目"，Claude 调工具拿候选展示（含 id /
名称 / 图数），用户回一个 id，Claude 用 Edit 工具改写本机 SKILL.md 的
`<REPLACE_ME>`。replaces 当前 onboarding step 5 的"自己开 GEO 后台手抄 id"
死步骤。

---

## 1. 问题定位

PR #147 + #149 已经把 `/goal` Loop 端到端打通，**但 onboarding step 5 卡了**：
使用者必须打开 GEO Web 后台 → 图库管理 → 主推栏目，找自己矩阵对应的 id，
回到本机 vim `.claude/skills/geo-article-writer/SKILL.md` 把 `<REPLACE_ME>`
替换成数字。这个步骤：

- **打断 Claude Code 主对话上下文**：用户要切到浏览器再切回来
- **没有 single source of truth**：栏目可能改名 / 新增，文档说明跟不上
- **不可逆错填**：填错 id 后 /goal 跑不出图但没人提示
- **同事第一次接入很怕**：「图库管理在哪？」「我的矩阵叫什么栏目？」

解决思路在前一轮对话里已锁：让 Claude 调工具自查、展示候选、写回文件。

---

## 2. 锁定决策（brainstorming 已答）

| # | 决策 | 选项 |
|---|------|------|
| 1 | 工具粒度 | **单 MCP 工具 `list_stock_categories(kind=None)`** —— LLM-facing 简单、不带"模糊匹配"魔法 |
| 2 | endpoint 落点 | **`mcp_catalog_router` 下 `/api/mcp/stock-categories`** —— 跟 `list_articles` / `list_question_pools` 等 catalog 端点同组 |
| 3 | 返回字段 | **id / name / kind / description / official_url / image_count**——image_count 关键，让用户秒判"这栏目有没有图" |
| 4 | 写回机制 | **Claude 用通用 Edit 工具改 SKILL.md**，不做自动 fill MCP 工具——决策权留用户 |
| 5 | 文案改 | writer skill 矩阵段加 1 句 + README onboarding step 5 重写 |
| 6 | 兼容旧版 | bundle version v2→v3，旧 v2 sha 保留在 KNOWN |

---

## 3. 架构总览

```
┌────────────────────────────────────────────────────┐
│ 用户对 Claude Code 说："帮我查主推栏目，我用餐厅养成记" │
└────────────────┬───────────────────────────────────┘
                 │
                 ▼
       ┌──────────────────────────────────┐
       │ Claude 调 MCP 工具：              │
       │ list_stock_categories(kind="main")│
       └─────────────┬────────────────────┘
                     │ _aget 转发
                     ▼
       ┌──────────────────────────────────────┐
       │ 新增 endpoint                          │
       │ GET /api/mcp/stock-categories         │
       │ (mcp_catalog_router, require_mcp_token)│
       └─────────────┬────────────────────────┘
                     │ 查 StockCategory + COUNT(StockImage) per category
                     ▼
       ┌──────────────────────────────────────┐
       │ 返回 list[{id, name, kind,            │
       │            description, official_url, │
       │            image_count}]              │
       └─────────────┬────────────────────────┘
                     │
                     ▼
       ┌──────────────────────────────────────┐
       │ Claude 展示「餐厅养成记 id=22 / 30 张图」│
       │ 等用户拍板 → Claude 用 Edit 工具改写：   │
       │ .claude/skills/geo-article-writer/SKILL.md│
       │ 把 <REPLACE_ME> 替换成 22              │
       └──────────────────────────────────────┘
```

### 3.1 关键设计点

1. **endpoint 落在 `mcp_catalog_router`**——跟 `list_articles` / `list_question_pools` 等读类目工具同组、同前缀 `/api/mcp/*`，约定一致
2. **不复用 image_library 自己的 user JWT 端点**——避免 dual-auth；独立薄 endpoint MCP token 鉴权
3. **`image_count` 用 SQL outer join COUNT 一次性算**——避免 N+1，空栏目也返（image_count=0）
4. **MCP 工具签名极简**——单可选参数 `kind: str | None = None`，未传返全量；不内置"模糊匹配矩阵名→栏目"魔法
5. **写回靠通用 Edit 工具**——Claude 已有的能力，不需额外 MCP 工具；决策权留用户
6. **skill + README 改文案极轻**——writer skill 矩阵段加 1 句提示、README onboarding step 5 重写整段
7. **必然 bump bundle version v2→v3**（skill 文本变了），旧 v2 sha 保留在 KNOWN

### 3.2 文件改动

```
入库：
  server/app/modules/mcp_catalog/router.py             # +endpoint + Pydantic
  server/mcp/tools/catalog.py                          # +tool
  server/app/modules/mcp_catalog/connect_router.py     # MCP_TOOLS_COUNT 20→21
  server/tests/test_mcp_connect.py                     # 断言 20→21
  server/tests/test_mcp_stock_categories.py            # 新建 5 用例
  server/app/modules/loop_skills/templates/
    skills/geo-article-writer/SKILL.md                 # 矩阵段加 1 句
    README.md                                          # onboarding step 5 重写
  server/app/modules/loop_skills/version.py            # bump v2→v3 + 新 sha
  docs/superpowers/specs/2026-06-25-loop-skill-category-discovery-design.md
  docs/superpowers/plans/2026-06-25-loop-skill-category-discovery.md

不动：
  - image_library/models.py（StockCategory schema）
  - image_library/router.py（user JWT 端点保留不动）
  - 任何前端
  - 旧 `<REPLACE_ME>` 占位字符串本身（writer skill 矩阵段保留）
```

合计 ~260 行，~2.8 小时工时。

---

## 4. Schema + endpoint + 工具签名

### 4.1 返回 schema

新 Pydantic 模型 `StockCategoryRead`，定义在 `mcp_catalog/router.py` 顶部
（保持模式扁平——只这一处用，不必拆 schemas.py）：

```python
class StockCategoryRead(BaseModel):
    id: int
    name: str
    kind: str                # "main" | "companion"
    description: str | None
    official_url: str | None
    image_count: int         # COUNT(StockImage) per category
```

**字段决策**：
- `id / name / kind`：必返，核心选择凭证
- `description`：可选，帮区分同类目（"餐厅养成记" vs "餐厅养成记 v2"）
- `official_url`：可选，戳进去看官方页面再确认
- `image_count`：必返，**关键决策辅助**——`餐厅养成记 id=22 / 30 张图` 比 `id=22` 信息密度高很多
- **不返** `bucket_name`（MinIO 内部细节，无业务价值）
- **不返** `created_at`（选 id 不需要时间信息）

### 4.2 endpoint

`server/app/modules/mcp_catalog/router.py` 末尾追加：

```python
from server.app.modules.image_library.models import StockCategory, StockImage


class StockCategoryRead(BaseModel):
    id: int
    name: str
    kind: str
    description: str | None
    official_url: str | None
    image_count: int


@router.get("/stock-categories", response_model=list[StockCategoryRead])
def mcp_list_stock_categories(
    kind: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[StockCategoryRead]:
    """[MCP] 列图库栏目（service 视角，无 per-user 过滤）.

    给 /goal Loop onboarding 用——使用者填 main_category_id 前先让 Claude
    调本工具看候选栏目. kind 过滤：'main' / 'companion' / None=全量.
    """
    # COUNT(StockImage) per category via subquery —— 一次 SQL 避免 N+1
    count_subq = (
        select(StockImage.category_id, func.count(StockImage.id).label("cnt"))
        .group_by(StockImage.category_id)
        .subquery()
    )
    q = (
        db.query(StockCategory, func.coalesce(count_subq.c.cnt, 0).label("image_count"))
        .outerjoin(count_subq, count_subq.c.category_id == StockCategory.id)
        .order_by(StockCategory.kind.asc(), StockCategory.id.asc())
    )
    if kind:
        q = q.filter(StockCategory.kind == kind)

    rows = q.all()
    return [
        StockCategoryRead(
            id=cat.id,
            name=cat.name,
            kind=cat.kind,
            description=cat.description,
            official_url=cat.official_url,
            image_count=int(image_count),
        )
        for cat, image_count in rows
    ]
```

**实现选择**：
- **outerjoin 而非 join**：空栏目也要返（image_count=0），不能因为没图就藏起来
- **`coalesce(..., 0)`**：outer join 后无 count 行得到 NULL，给 0 兜底
- **`order_by(kind asc, id asc)`**：`main` 字典序在 `companion` 之前，主推栏目优先展示；同 kind 内按 id 升序稳定排序
- **`Pydantic` 验证 kind 取值**：endpoint 接受任何 str（即使 `kind="garbage"` 也只会返空列表），无需 422——容错比严格更友好

### 4.3 MCP 工具签名

`server/mcp/tools/catalog.py` 末尾追加：

```python
@mcp.tool()
async def list_stock_categories(
    kind: str | None = None,
) -> dict[str, Any]:
    """List stock image library categories (image buckets) — for /goal Loop onboarding.

    Use this when the user needs to find a `main_category_id` to fill into
    their writer SKILL.md matrix section. Show the returned list to the user
    so they can pick the one matching their content matrix (e.g. "餐厅养成记").

    Args:
        kind: Filter by category kind. Common values:
            - "main": 主推栏目 (one per content matrix — what writer skill picks)
            - "companion": 陪衬栏目 (AI auto-detects across all of them)
            - None (default): return all categories

    Returns:
        {"ok": True, "data": [
            {
                "id": int,
                "name": str,           # e.g. "餐厅养成记"
                "kind": str,           # "main" | "companion"
                "description": str | None,
                "official_url": str | None,
                "image_count": int,    # total images in this bucket
            },
            ...
        ], "error": None}
    """
    params: dict[str, Any] = {}
    if kind:
        params["kind"] = kind
    return await _aget("/api/mcp/stock-categories", params=params or None)
```

**docstring 设计**：
- 第一段说**为什么有这个工具**（onboarding 场景）—— 不止"列类目"
- "Use this when..." 明确触发条件，避免无关时调用
- 返回示例字段用真实样例值 `"餐厅养成记"`，让 Claude 接受用户题材关键词时能匹配上

### 4.4 用户对话样例（仅参考，不入实现）

```
用户：帮我查下主推栏目，我用的是餐厅养成记
Claude（调 list_stock_categories(kind="main")）：
  找到 3 个主推栏目：
  - 22 餐厅养成记 (30 张图)
  - 23 江南百景图 (12 张图)
  - 24 桃源深处有人家 (8 张图)
用户：用 22
Claude（用 Edit 工具改 .claude/skills/geo-article-writer/SKILL.md）：
  已替换 main_category_id = <REPLACE_ME> 为 22
```

---

## 5. skill + README 文案改

### 5.1 writer skill 矩阵特例段微调

`server/app/modules/loop_skills/templates/skills/geo-article-writer/SKILL.md`
找到当前的（PR #149 后内容）：

```markdown
- 配图主推栏目：`main_category_id = <REPLACE_ME>`  # ← 安装时查 GEO 后台
  「图库管理」→ 主推栏目「餐厅养成记」的 id；写死在这里
```

替换为：

```markdown
- 配图主推栏目：`main_category_id = <REPLACE_ME>`  # ← 安装时填，**不知道 id 就
  对 Claude 说「帮我查下主推栏目，我用<矩阵名>」**，它会调 list_stock_categories
  MCP 工具列候选并用 Edit 工具帮你写到这里。也可以去 GEO 后台「图库管理」→
  主推栏目手抄 id
```

不动「## 加新矩阵」段、不动「配图风格 / 封面 / 陪衬」3 个 bullet。

### 5.2 README onboarding step 5 重写

`server/app/modules/loop_skills/templates/README.md` 当前 step 5：

```
5. 打开本机 .claude/skills/geo-article-writer/SKILL.md，找到「矩阵特例」段
   `main_category_id = <REPLACE_ME>` 行；去 GEO 后台「图库管理」→ 主推栏目
   里找你矩阵对应栏目（比如餐厅养成记），把 id 填进去（数字）。
```

替换为：

```
5. 让 Claude 帮你填 main_category_id：
   在 Claude Code 主对话里直接说：
     "帮我查下主推栏目，我用的是<你的矩阵名>"
   Claude 会调 list_stock_categories MCP 工具列出候选（含 id / 名称 / 图数），
   你选一个 id 告诉它，Claude 会用 Edit 工具帮你写到本机
   .claude/skills/geo-article-writer/SKILL.md 的「矩阵特例」段。

   也可以走老路：自己去 GEO 后台「图库管理」→ 主推栏目查 id，手动 vim 改文件。
```

step 1-4 / 6 都不动。

---

## 6. bundle version bump

`server/app/modules/loop_skills/version.py`：

```python
LOOP_SKILL_BUNDLE_VERSION = "2026-06-25-v3"   # was "2026-06-25-v2"

KNOWN_BUNDLE_SHAS: frozenset[str] = frozenset(
    {
        # v1 (2026-06-24)
        "49f824a36606c285b84c71ade5aec406ea3c545599b7a3ccf59f863b521667dd",
        # v2 (2026-06-25): writer step 5 + 矩阵特例 + orchestrator 日志中文化
        "abd8416c51f0b591c85cee0c3635645a10a313a2cedbeb52b89953a2c41e7fea",
        # v3 (2026-06-25): writer 矩阵段 + README step 5 改为 list_stock_categories 路径
        "<v3 实施时跑 build_bundle 拿到的新 sha>",
    }
)
```

> v1 / v2 sha 保留：本地未升级用户的 KNOWN 校验仍要认。

---

## 7. 失败矩阵 + 不变式

### 7.1 失败矩阵

| 层 | 故障 | endpoint 反应 | MCP 工具反应 | Claude / 用户能看到 |
|---|---|---|---|---|
| MCP token 错 | `require_mcp_token` → 401 | — | `{ok: false, error: "401 ..."}` | Claude 提示"MCP token 失效，请检查 ~/.claude.json" |
| `kind` 传无效值（如 "xxx"） | 过滤后无行 → 返 `[]` | — | `{ok: true, data: []}` | Claude 跟用户说"没找到该 kind 的栏目，要不要查全量？" |
| 数据库连不上 | 全局 500 | mcp_exception_response 包装的字符串 | `{ok: false, error: "..."}` | Claude 提示后端不可用 |
| StockCategory / StockImage 模型 schema 改变 | Pydantic 422 / SQL error | mcp_exception_response | 同上 | 同上 |
| 栏目 0 张图（空栏目） | 正常返 `image_count=0` | — | 同上 | Claude 展示给用户参考 |
| 数据库空（无任何栏目） | 返 `[]` | — | `{ok: true, data: []}` | Claude 提示"GEO 后台还没建任何图库栏目，先去配一下" |

### 7.2 三个不变式

1. **endpoint 不写不抛业务异常**——只是 list 查询，最多走 mcp_exception_response 包外部异常
2. **MCP 工具是薄壳**——不在工具层做过滤 / 排序 / 格式化（endpoint 已经做了）
3. **bundle sha 必须随模板内容变化**——CI test_bundle_sha_is_known 强制守

---

## 8. 测试策略

### 8.1 自动测（CI 跑）

| # | 测试 | 文件 | 验证什么 |
|---|---|---|---|
| 1 | `test_endpoint_requires_mcp_token` | `tests/test_mcp_stock_categories.py`（新建） | 不带 `X-MCP-Token` → 401 |
| 2 | `test_endpoint_returns_all_categories_when_no_kind_filter` | 同上 | seed 2 main + 1 companion，不带 kind → 返 3 条；字段齐 |
| 3 | `test_endpoint_filters_by_kind_main` | 同上 | 同 seed，`?kind=main` → 返 2 条 main，无 companion |
| 4 | `test_endpoint_image_count_correct` | 同上 | 给某栏目 seed 3 个 StockImage → `image_count=3`；空栏目 `image_count=0` |
| 5 | `test_endpoint_order_main_before_companion` | 同上 | seed mixed kind，返回顺序 main 在 companion 前 |
| - | bundle sha v3 in KNOWN | 既有 `test_loop_skill_bundle.py` | 已有，自动验 |
| - | MCP_TOOLS_COUNT 21 断言 | 既有 `test_mcp_connect.py`（断言改 20→21） | 改完就过 |

**不测**：
- MCP 工具 wrapper（薄壳，端到端鉴权 + 行为已被 endpoint 测覆盖）
- skill 模板内容（sanity grep 就够）
- 文案改动的 markdown 渲染（人眼）

### 8.2 手工冒烟

| # | 步骤 | 期望 |
|---|---|---|
| 1 | 装 v3 skill（Web Section ⑤ 下载 zip 或 install_loop_skills 工具） | bundle 版本 `2026-06-25-v3` |
| 2 | 在 Claude Code 主对话说"帮我查下主推栏目" | Claude 调 list_stock_categories(kind="main") → 展示候选含 id + 图数 |
| 3 | 告诉 Claude 选某个 id（如 22） | Claude 用 Edit 改本机 `.claude/skills/geo-article-writer/SKILL.md`，把 `<REPLACE_ME>` 替成 22 |
| 4 | 跑 `/goal 1 篇国风游戏文章作为冒烟` | 走通 + 文章有插图 + 有封面（验证 v2 配图修复 + v3 onboarding 都正常） |

---

## 9. 工作量估算 + 实施顺序

### 9.1 工作量

| 模块 | 行 | 工时 |
|---|---|---|
| `mcp_catalog/router.py`（endpoint + Pydantic） | +50 | 0.5 h |
| `mcp/tools/catalog.py`（工具） | +30 | 0.2 h |
| `mcp_catalog/connect_router.py`（count 20→21） | +1/-1 | 0.05 h |
| `tests/test_mcp_connect.py`（断言 20→21） | +1/-1 | 0.05 h |
| `tests/test_mcp_stock_categories.py`（5 用例） | +160 | 1.2 h |
| `templates/skills/geo-article-writer/SKILL.md` | +3/-1 | 0.1 h |
| `templates/README.md`（onboarding step 5） | +10/-3 | 0.2 h |
| `loop_skills/version.py`（bump v2→v3） | +3/-1 | 0.05 h |
| 全 lint/test + push + PR | — | 0.5 h |
| **合计** | **~260 行** | **~2.8 h（半天）** |

### 9.2 实施顺序

```
1. endpoint + 5 单测（独立，TDD 友好）
2. MCP 工具薄壳（依赖 endpoint）
3. MCP_TOOLS_COUNT 20→21 同步 + test_mcp_connect 断言同步
4. skill / README 模板改
5. bundle version bump v2→v3 + 新 sha 加 KNOWN
6. 全 lint/test + push + PR
```

---

## 10. 与已有 spec / 实现的关系

| 参考 | 关系 |
|---|---|
| [`2026-06-24-loop-skill-distribution-design.md`](./2026-06-24-loop-skill-distribution-design.md) | PR #147 已合，引入 `<REPLACE_ME>` 占位机制；本设计提供自助 fill 流程 |
| [`2026-06-25-loop-illustration-and-i18n-fix-design.md`](./2026-06-25-loop-illustration-and-i18n-fix-design.md) | PR #149 已合，把 `<REPLACE_ME>` 推到 main_category_id 行；本设计是它的 onboarding UX 改进 |
| `image_library/models.py` (StockCategory) | 只读它的字段；不动 |
| `image_library/router.py` | user JWT 端点不动；新加 MCP 端点是独立薄路径 |
| `mcp_catalog/router.py` | 加 endpoint 跟现有 list_articles 等同模式 |

---

## 11. Out of Scope（明确不做的）

- **Web Section ⑤ 加"一键复制 id"按钮**：Claude 直接帮填，Web 不必再加便利按钮
- **`list_stock_images(category_id)` 工具**：用户层面是"选栏目"不是"挑单图"
- **`setup_loop_skills_matrix(matrix_hint)` 一站式工具**：模糊匹配语义不稳，与 Claude "展示候选 + 让用户选"的能力重复
- **兼容旧 `<REPLACE_ME>` 自动检测 + 自动 fill 的 setup 工具**：YAGNI；明确指令更好
- **chunked / 分页**：图库栏目数量在 dozens 量级，无分页必要
- **`image_library/router.py` 改动**：user JWT 端点不在范围内

---

## 12. 上线门禁

- 后端 ruff / format / pytest 全过
- 5 个新用例通过 + test_mcp_connect 工具数断言（21）更新通过
- bundle sha 校验测试通过（v3 sha 进 KNOWN）
- 手工冒烟 4 步通过（重点：步骤 2 看候选展示、步骤 3 看 Edit 写入成功）
