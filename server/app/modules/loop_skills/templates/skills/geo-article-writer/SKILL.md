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
4. `save_article(question_item_id, prompt_template_id, title, markdown_content, model_label)`
5. `ai_illustrate_article(article_id, main_category_id=<从矩阵特例段拿>)` —
   AI 智能配图 + 自动封面，**返回值检查 `format_error` / `cover_error` 字段**；
   有错就在最后 JSON 里加 `illustration_warnings` 透传给 orchestrator，不抛错
6. 返回 `{"article_id": int, "title": str}` 作为**最后一条消息**，**只输出 JSON 一行**

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
- 配图主推栏目：`main_category_id = <REPLACE_ME>`  # ← 安装时查 GEO 后台
  「图库管理」→ 主推栏目「餐厅养成记」的 id；写死在这里
- 配图风格：默认 `aggressive_images=True`（积极配图，每个明确出现的游戏都插）
- 封面：默认 `set_cover=True`（从主推栏目随机取一张做封面，已有封面则跳过）
- 陪衬：默认 `include_companion=True`（AI 同时从所有陪衬栏目选）

> 调用约定：
> `ai_illustrate_article(article_id=<>, main_category_id=<上面那个值>)`
> 其余 3 个布尔参数走默认即可。

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

成功：
```
{"article_id": 824, "title": "国风游戏 2026 推荐 10 选"}
```

失败：
```
{"error": "save_article 415: unsupported markdown element"}
```

不要在 JSON 前后加任何解释 / markdown 包裹 / "我写完了" 之类的话。
orchestrator 用正则匹配最后一行 JSON 拿结果。
