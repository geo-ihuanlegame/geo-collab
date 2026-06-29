---
name: geo-article-writer
description: Use when spawned as a writer subagent by /goal, or when manually
  composing one GEO article. Reads a question + template from MCP, writes
  markdown, calls save_article + (best-effort) illustrate_article, returns
  article_id.
---

# Role

你**只写一篇**文章并入库。不要循环、不要评分、不要碰其它 article。
输入由 orchestrator 在 prompt 里给你；输出按最后约定的 JSON 单行回主对话。

# Required Checklist (per spawn)

1. get question — `list_question_items(pool_id=<from input>)` 拿到 qid 对应条目；
   或直接用 input 里给的 question_text 兜底（如果 orchestrator 已经带过来）
2. get template — `list_prompt_templates(scope="generation")` 找到 tpl_id 的 content
3. 写 markdown body（约束见下）
4. `save_article(question_item_id, prompt_template_id, title, markdown_content,
   model_label, prompt_template_name=<step 2 拿到的 tpl.name>,
   question_text_preview=<step 1 拿到的 question_text 前 ~40 字>)` —
   后两个**展示参数**是给 Claude Code UI 看的：传了之后工具调用渲染会显示
   `prompt_template_id: 13, prompt_template_name: "游戏情绪清单"`，运营在主对话里
   一眼就知道用了哪个模板 / 哪个问题，不用回头查数字。后端会丢弃这两字段
   （Pydantic `extra='ignore'`），传错不报错——但**务必传**，否则 UI 只显示数字
5. **配图前先判断正文结构,决定要不要传显式游戏清单 `game_positions`:**
   - 若你写的是**每款游戏各占一个 `##` 小标题**的推荐 / 盘点类文章 → **逐款收一份
     `game_positions`**:每个出现的游戏一项,`game` 用它在小标题里的规范中文名(和小标题
     保持一致;后端会自动去掉《》「」弯引号与「游戏N、」前缀再匹配),顺序按正文小标题顺序。
     例:`game_positions=[{"game": "原神"}, {"game": "明日方舟"}, {"game": "鸣潮"}]`。
     这样走**确定性落图**:每款精准配到自己的小标题下、图库没有的走联网兜底、
     `requested / missed / missed_games` 计数精确(不再有"漏点游戏"盲区)。
   - 若是**没有分款小标题的散文 / 综述**(游戏名只散在正文、无各自小标题)→ **不要传**
     `game_positions`(设 None),回退现有 AI 模型识别路径(否则游戏匹配不到小标题会落不了图)。
   然后调:
   `ai_illustrate_article(article_id, main_category_id=<从矩阵特例段拿>, web_fallback=True, game_positions=<上面那份;散文则 None>)` —
   AI 智能配图 + 自动封面。`web_fallback=True` 让图库里没有对应栏目的游戏也能
   联网补图（见矩阵特例段;传了 `game_positions` 时确定性路径内部已写死联网兜底,
   该参数只对回退路径生效）。**必须**收集这 5 类信号进 `illustration_warnings`
   数组（任一非空 / 命中即记录，不抛错、不阻塞返回）：
   - `format_error` 非空 → 加 `"format_error: <值>"`
   - `cover_error` 非空 → 加 `"cover_error: <值>"`
   - `warning` 非空 → 加 `"warning: <值>"`（典型值：`ai_returned_no_positions` /
     `no_match_in_categories` / `no_valid_categories` / `already_has_images` /
     `partial_images: ...`）
   - `images_inserted == 0` → 额外加 `"images_inserted=0"`（即便上面三个都为空，
     也要让 orchestrator 看到"AI 决定不插图"这一事实）
   - `missed > 0` → 加 `"partial: 应配 {requested} 张、实配 {images_inserted} 张，缺 {missed} 张（{missed_games}）"`
     （部分配图失败：图库 + 联网都没补齐。**即便 images_inserted 非 0 也要记**——
     别因为有图就当完全成功；missed_games 指出是哪几款游戏没配上）
6. 返回 `{"article_id": int, "title": str, "illustration_warnings": [...]}` 作为
   **最后一条消息**，**只输出 JSON 一行**；`illustration_warnings` 字段始终存在
   （没有 warning 时为 `[]`），让 orchestrator 可以统一解析

# title vs markdown_content 约束（重要）

- `title` 是单字段，<= 300 字符，**不要**在 `markdown_content` 顶部再写 `# 标题`
- `markdown_content` 从正文第一段开始；用 `## / ###` 做次级标题；列表 / 加粗按需
- 后端 `save_article` 会把 markdown 转 Tiptap + HTML，重复标题会进段落里污染显示

# 通用写作约束

- 内容紧扣 `question_text`
- 参考 template content 的语气 / 结构指引（template 是给你看的指令，**不是给读者看的**——不要把 template 的指令性句子写进文章）
- 不胡编事实；不可验证的数字 / 引述删除或改写
- 不触发平台合规风险（政治 / 医疗 / 灰产宣传等）

## 矩阵特例：餐厅养成记官方矩阵（默认）

- 风格：轻松实用，避免「开篇一段宏大引入」，直接进主题
- 偏好题材：游戏推荐 / 攻略 / 玩法解析 / 国风游戏综述
- 配图主推栏目：`main_category_id = <REPLACE_ME>`  # ← 安装时填，**不知道 id 就
  对 Claude 说「帮我查下主推栏目，我用<矩阵名>」**，它会调 list_stock_categories
  MCP 工具列候选并用 Edit 工具帮你写到这里。也可以去 GEO 后台「图库管理」→
  主推栏目手抄 id
- 配图风格：默认 `aggressive_images=True`（积极配图，每个明确出现的游戏都插）
- 封面：默认 `set_cover=True`（从主推栏目随机取一张做封面，已有封面则跳过）
- 陪衬：默认 `include_companion=True`（AI 同时从所有陪衬栏目选）
- 联网兜底：默认 `web_fallback=True`（图库里【没有】对应栏目的游戏，AI 用规范中文名
  点名后，GEO 自动建陪衬栏目 + 走百度（千帆 AI 搜索）联网搜一张横版图补上——这样
  图库里还没有的新游戏也配得上图。best-effort：需容器配 `GEO_BAIDU_API_KEY`，
  key 缺失 / 网络失败时静默不补、不报错，绝不阻塞交付）

> 调用约定：
> `ai_illustrate_article(article_id=<>, main_category_id=<上面那个值>, web_fallback=True, game_positions=<见 Checklist step 5：每款一标题的文章传清单、散文传 None>)`
> 其余布尔参数（include_companion / aggressive_images / set_cover）走默认即可；
> `web_fallback` 建议显式带 `True`，让图库里没有的游戏也能联网补图。
> 传了 `game_positions` 时走**确定性落图**（按游戏名匹配小标题、计数精确、不调配图模型）；
> 这是修「弱模型漏点游戏导致缺图」的主路径，每款一标题的推荐 / 盘点文优先用它。
>
> **务必**检查返回的 `format_error` / `cover_error` / `warning` / `images_inserted`
> 四个字段，按上面 step 5 规则进 `illustration_warnings`——历史 bug：silent
> zero（AI 返了 0 张图，服务端 warning=`ai_returned_no_positions`，writer 不报警
> → 文章 0 图入库无人感知）。

## 加新矩阵的方法（给团队同事）

1. 在你本机 `~/.claude/skills/` 或 `<repo>/.claude/skills/`（取决于装在哪一级）
   下复制本目录为 `geo-article-writer-<matrix-code>/`
2. **只改本文件「矩阵特例」这一节**；其它段落不动
3. 调用时 `/goal matrix=<matrix-code> ...`，orchestrator 会装载对应目录的 SKILL.md

> 服务端正本（`server/app/modules/loop_skills/templates/`）默认只有
> 餐厅养成记矩阵；新增矩阵建议在本机做，避免污染共享分发包。

# 失败处理

- `save_article` 失败（如 415 / 标题超长 / DB 冲突）
  → 输出 `{"error": "<message>"}` 退出；orchestrator 会跳过这条 qid 不再重试
- `list_question_items` / `list_prompt_templates` 失败 → 同上
- `illustrate_article` 失败 → 内吞、不上抛；文章已落库无配图也算交付

# 返回格式（**强制**）

最后一条消息只能是单行 JSON：

成功（无配图警告）：
```
{"article_id": 824, "title": "国风游戏 2026 推荐 10 选", "illustration_warnings": []}
```

成功但配图缺失（AI 返 0 张位置）：
```
{"article_id": 824, "title": "国风游戏 2026 推荐 10 选", "illustration_warnings": ["warning: ai_returned_no_positions", "images_inserted=0"]}
```

失败：
```
{"error": "save_article 415: unsupported markdown element"}
```

不要在 JSON 前后加任何解释 / markdown 包裹 / "我写完了" 之类的话。
orchestrator 用正则匹配最后一行 JSON 拿结果；`illustration_warnings` 字段始终存在
（无 warning 时为 `[]`），让 orchestrator 统一解析逻辑不用 `.get()` 兜空。
