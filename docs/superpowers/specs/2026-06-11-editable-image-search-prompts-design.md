# 设计：AI 配图搜索词 + 陪衬插图提示词改为数据库可编辑

> 状态：已确认，进入实现。范围 = A（可编辑搜索词）+ B（可编辑陪衬插图提示词）。C（AI 联网判断游戏）另立项。

## 背景与动机

AI 自动配图链路里，两段关键文案目前**硬编码在 Python 源码**，导致没法在线测试调优：

1. **百度搜图关键词**：[`server/app/shared/baidu.py:22`](../../../server/app/shared/baidu.py) 的 `_LANDSCAPE_QUERY_SUFFIX = "横屏壁纸"`，最终搜索词写死为 `f"{游戏名} 横屏壁纸"`（baidu.py:99）。实测搜出的图"不够游戏强相关"，原因是这段关键词太单薄（缺"官方宣传图""横版"等字眼），且改一次要发版。
2. **陪衬游戏插图提示词**：[`server/app/modules/articles/ai_format.py:488-496`](../../../server/app/modules/articles/ai_format.py) 的 `_WEB_FALLBACK_PROMPT_SUFFIX`，控制 AI 对"库里没有的游戏"是否积极配图。实测陪衬游戏插图"不够积极"，想能手动调这段提示词。

**关键澄清**：用户口中的"AI 搜图提示词"其实**不是喂给 AI 的提示词**——AI（DeepSeek v4-flash）只负责判断"哪段配图、属于哪个游戏"，搜索词是后端拿 AI 返回的游戏名硬拼的。本设计就是把这两段写死文案搬进数据库、在前端可编辑。

**预期结果**：在前端「提示词管理」tab 新增两个类目，用户可像编辑其它提示词一样新建/编辑/启停搜索词与陪衬提示词，反复 A/B 测试调优，无需改代码发版。

## 设计

### 复用现有提示词模板系统，新增两个 scope

`prompt_templates` 表的 `scope` 字段是无约束的 `String(50)`（建表迁移 `0026`，应用层用 `VALID_PROMPT_SCOPES` + `PromptScope` Literal 双白名单），**新增 scope 值不需要写数据库迁移**——只改应用层白名单 + 前端 tab，沿用 `/api/prompt-templates` 全套 CRUD。

新 scope：

- **`image_search`** —— 百度搜图关键词模板。内容支持 `{game}` 占位符：含占位符则 `content.replace("{game}", 游戏名)`；不含则按 `f"{游戏名} {content}"` 拼接（带空格）。空表/无启用时回退默认常量 `DEFAULT_IMAGE_SEARCH_QUERY = "{game} 横版 官方宣传图"`（保留"横版"以维持 `landscape_only()` 横版过滤意图）。
- **`image_companion`** —— 陪衬游戏插图提示词，替换 `_WEB_FALLBACK_PROMPT_SUFFIX`。空表/无启用时回退到现有那段硬编码常量（保持不配置时行为完全不变）。

### "当前生效模板"语义：取启用的那一条（区别于随机抽）

generation / ai_format 走的是"按 id 从允许列表随机抽一条"（`_pick_valid_template`）。但搜索词/陪衬提示词是**全局调优旋钮**，语义应是"当前启用的那一条"。新增纯函数：

```python
def get_active_template_content(db, *, scope, user_id, default) -> str:
    # 过滤 is_deleted==False & is_enabled==True & (user_id==uid OR is_system)
    # 排序：本人模板优先于系统模板（is_system asc），再按 updated_at desc
    # 取第一条的 content（strip 后非空）；无则返回 default
```

用户靠启停切换做 A/B：同一 scope 同时只启用一条即确定。不引入 `preset_id` 指针/新列。

### 接线：顶层一次性解析成字符串再下传（不污染 baidu.py）

`run_ai_format` 顶层同时手握 `db` + `user_id`，在那里把两个模板解析成字符串：

- **搜索词**：解析出 `image_search_query` 字符串 → 透传 `_maybe_insert_images(..., image_search_query=...)` → `_web_fallback_fill_category(db, category, image_search_query)` → `baidu.search_landscape_images(game_name, query_template=image_search_query)`。`baidu.py` 保持纯工具、不碰 DB/user_id。
- **陪衬提示词**：在现有 `if include_images and web_fallback:` 处，把 `system_prompt += _WEB_FALLBACK_PROMPT_SUFFIX` 改为 `system_prompt += get_active_template_content(db, scope="image_companion", user_id=user_id, default=_WEB_FALLBACK_PROMPT_SUFFIX)`。

> `user_id` 三个入口（`articles/router.py`、`scheme_executor.py`、`ai_illustrate.py`）都已传进 `run_ai_format`；本设计只在 `_maybe_insert_images`/`_web_fallback_fill_category` 中间层把"已解析的搜索词字符串"透传下去，不再额外传 user_id 进 baidu 这种共享工具。
> `user_id` 为 None 时（理论上不该发生在 web_fallback 路径），`get_active_template_content` 内部对 `user_id is None` 直接回退 default。

## 要改的文件

**后端**

- `server/app/shared/baidu.py` — `search_landscape_images(game_name, *, query_template=DEFAULT_IMAGE_SEARCH_QUERY, top_k=15)`；`_build_query()` 实现 `{game}` 占位逻辑；`_LANDSCAPE_QUERY_SUFFIX` → `DEFAULT_IMAGE_SEARCH_QUERY = "{game} 横版 官方宣传图"`。
- `server/app/modules/articles/ai_format.py` — `run_ai_format` 顶层解析 `image_search_query`；透传到 `_maybe_insert_images` → `_web_fallback_fill_category`；陪衬提示词改读 `image_companion` 模板（常量保留作默认值）。
- `server/app/modules/prompt_templates/service.py` — `VALID_PROMPT_SCOPES` 增加两值；新增 `get_active_template_content()`。
- `server/app/modules/prompt_templates/schemas.py` — `PromptScope` Literal 增加两值。

**前端**

- `web/src/types.ts` — `PromptScope` 增加两个字面量。
- `web/src/features/prompt-templates/PromptsWorkspace.tsx` — `scopeTabs` 加两项（`搜图关键词`/`陪衬配图提示词`）；modal 里按 scope 显示一行小提示（搜图词说明 `{game}` 占位符 + 空格拼接规则）。复用现有列表/启停/软删/弹窗，无新组件。

**无需**：数据库迁移、新建表、新 API 端点。

## 测试

后端（`server/tests/`，MySQL，需 `GEO_TEST_DATABASE_URL`）：

- `get_active_template_content`：本人优先于系统、过滤软删/停用、空表回退 default、`user_id=None` 回退 default。
- `baidu` query 构造纯函数：含 `{game}` 占位符替换 vs 不含时空格拼接。
- 端到端接线：建启用 `image_search` 模板，跑 `run_ai_format(web_fallback=True)`，monkeypatch `baidu.search_landscape_images` 断言收到的 `query_template`=模板内容；建 `image_companion` 模板，monkeypatch `_call_litellm_completion` 捕获 system prompt 断言包含该内容、无模板时含默认常量。

前端：`pnpm --filter @geo/web typecheck` + `build`。

手动验收（需配 `GEO_BAIDU_API_KEY`）：前端「提示词管理 → 搜图关键词」建 `{game} 横版 官方宣传图` 并启用 → 跑 AI 配图节点（web_fallback 开）→ 看后端日志确认发给百度的 query 用了新词。
