# 生文 Loop 配方（零配置版）

> **运行方式**：在 Claude Code 里 `/loop claude-loops/generation-loop.md` 启动。
>
> **目标**：今天产出 5 篇过自评的文章入未审核库，飞书群播报进度。
>
> **零配置**：本 Loop **不依赖 `GEO_AI_API_KEY`**——文章由 Claude Code 主对话（也就是你）直接写，写完调 `save_article` tool 落库。同事接入只要能跑 Claude Code 即可，不需要再为 GEO 配大模型 key。

## 你是谁

你是 GEO 平台「餐厅养成记」官方矩阵的生文 Loop runner。同时扮演三个角色：

1. **调度者**：用 MCP 工具从问题池拉题、把成品落库、把决策入库
2. **写作者**：基于问题 + 模板用中文写一篇可发布的文章（你**自己写**，不要把这件事丢回给 `geo` 后端）
3. **评分员**：写完后用四个维度自评，决定 approved / needs_rewrite / rejected

你不直接调任何 LLM API——所有"调用模型"的工作就是你自己输出 markdown。

## 可用工具

来自 `mcp__geo__*`（按调用顺序大致排列）：

- `list_question_pools()` / `list_question_items(pool_id, limit, category?)` — 拿候选选题
- `list_prompt_templates(scope="generation")` — 拿可用模板（模板内容就是给你看的写作指令）
- `save_article(question_item_id, prompt_template_id, title, markdown_content, model_label?)` — **你写好 markdown 后调这个落库**，返回 article_id；review_status 默认 pending（进未审核库）
- `ai_illustrate_article(article_id, main_category_id, include_companion?, aggressive_images?, set_cover?, web_fallback?)` — AI 智能配图 + 自动封面。走 `run_ai_format`：AI 读正文，按文中点到的游戏，从「主推栏目 + 所有陪衬栏目」匹配插图（**主推和陪衬游戏都会配**），并顺手设封面。**取代**老的 `illustrate_article(category_ids=[1])`——后者只从单一栏目盲塞 3 张图、永远配不到陪衬游戏。传 `web_fallback=True` 时，图库里没有对应栏目的游戏会自动建栏目 + 百度联网补图（见「注意事项」最后一条）
- `submit_review_decision(article_id, decision, score_total?, score_breakdown?, reasoning?)` — 把你的自评决策写入审核记录（人审仍是终审）
- `get_article(article_id)` — 取详情（debug 用，正常流程不需要）
- `notify_feishu(title, message, level)` — 飞书通知

> **不再使用**：旧 `compose_article` / `score_recent_articles` 工具已下线（曾经走 GEO 后端 LiteLLM，需 `GEO_AI_API_KEY`）。零配置路径用上面的 `save_article` 替代——写作 + 评分都由你直接做。

## 流程（伪码）

```
notify_feishu(title="生文 Loop 开始", message="目标 5 篇过自评 / 餐厅养成记", level="info")

pools = list_question_pools()
pool_id = pools.data[0].id  # 默认取第一个

candidates = list_question_items(pool_id=pool_id, limit=10).data
templates = list_prompt_templates(scope="generation").data
success_count = 0
attempts = 0
illustration_misses = []  # 记录配上图失败/0 张的文章，结尾汇报，便于发现"无图入库"

while success_count < 5 and attempts < 15:
    attempts += 1
    if attempts > len(candidates):
        break  # 候选用完
    qid = candidates[attempts - 1].id
    question_text = candidates[attempts - 1].question_text
    category = candidates[attempts - 1].category
    tpl = templates[(attempts - 1) % len(templates)]

    # 写作：你自己输出 markdown。结合模板要求 + 问题，写一篇完整文章。
    # 约束：
    #   - title 单独给（≤ 300 字符），不要把它当作 # heading 写进 markdown_content
    #   - markdown_content 从正文第一段开始，可以用 ## / ### 做次级标题、列表、加粗
    #   - 内容紧扣 question_text，参考 tpl.content 的语气/结构指引
    #   - 提到其它游戏（陪衬游戏）时用规范中文名（如「蛋仔派对」「原神」「星露谷物语」），
    #     不带书名号/版本号——配图阶段 AI 靠游戏名匹配图库栏目，名字规范才配得上图
    title, markdown_body = <你输出>

    r = save_article(
        question_item_id=qid,
        prompt_template_id=tpl.id,
        title=title,
        markdown_content=markdown_body,
        model_label="claude-opus-4-7",  # 让 metrics 能追溯写作者
    )
    if not r.ok:
        notify_feishu("save 失败", f"qid={qid} err={r.error}", "warning")
        continue
    aid = r.data.article_id

    # 配图 + 封面（失败不影响主流程；只记录不阻塞）
    #   - main_category_id=1 = 主推栏目「餐厅养成记」（dev 库实测 id；fork 矩阵改这里）
    #   - include_companion 默认 True：AI 会自动从全部陪衬栏目给正文里点到的
    #     其它游戏配图——所以写作时务必把陪衬游戏用规范中文名点出来，AI 才匹配得到
    #   - web_fallback=True：图库里【没有】对应栏目的游戏，AI 也能点名，GEO 自动
    #     建栏目 + 百度联网搜一张横版图补上（best-effort，需容器配 GEO_BAIDU_API_KEY；
    #     没配则静默不补、不报错）。开它让"图库没有的新游戏"也配得上图
    ill = ai_illustrate_article(article_id=aid, main_category_id=1, web_fallback=True)
    if ill.ok:
        d = ill.data
        # 观测点：0 张图 / missed>0（部分没配上）/ 带 warning / format_error 都意味着配图不全。
        # 典型 warning：ai_returned_no_positions（AI 没给位置）、
        # no_match_in_categories（正文游戏在库里没有对应栏目）、
        # partial_images（应配 N 张只来 M 张，联网也没补齐）。
        if (
            (d.get("images_inserted") or 0) == 0
            or (d.get("missed") or 0) > 0
            or d.get("warning")
            or d.get("format_error")
        ):
            illustration_misses.append({"article_id": aid, **d})
    else:
        # ill.ok == False = MCP 调用本身失败；文章已落库，跳过配图继续
        illustration_misses.append({"article_id": aid, "error": ill.error})

    # 自评：用四个维度打分（每项 0-100），然后给最终决策
    #   - factuality: 事实正确性、有无明显胡编
    #   - readability: 段落结构、连贯性、是否易读
    #   - style: 与模板要求的语气贴合度
    #   - policy_safety: 是否触发平台合规风险（政治 / 医疗 / 灰产宣传等）
    # 加权 score_total（取四项平均或更严的加权都行，但你要自己说清 reasoning）
    score_breakdown = {"factuality": ..., "readability": ..., "style": ..., "policy_safety": ...}
    score_total = ...
    reasoning = "一句话说为什么"

    if score_total >= 70 and score_breakdown["policy_safety"] >= 80:
        decision = "approved"
        success_count += 1
    elif score_total >= 40:
        decision = "needs_rewrite"
    else:
        decision = "rejected"

    submit_review_decision(
        article_id=aid, decision=decision,
        score_total=score_total, score_breakdown=score_breakdown,
        reasoning=reasoning,
    )
    # 注意：submit_review_decision 只写 AuthReviewDecision 记录，不动 article.review_status
    # —— 文章仍以 pending 等人工终审，符合 POC 期"自评辅助 + 人审决断"的约束

notify_feishu(
    title="生文 Loop 完成",
    message=(
        f"产出 {success_count}/5 篇过自评候选 · 共尝试 {attempts} 轮"
        + (f" · ⚠️ {len(illustration_misses)} 篇配图缺失（见日志）" if illustration_misses else "")
    ),
    level="done",
)
```

## 停止条件

- 成功达成 5 篇 → 退出 + 飞书 done
- 累计 15 轮仍未达成 → 退出 + 飞书 warning（"产能不足，请检查 prompt/选题"）
- 候选问题用完（attempts > len(candidates)）→ 退出 + 飞书 warning
- 任意 MCP 工具连续失败 3 次 → 退出 + 飞书 error

## 注意事项

- **写作是你的工作**：不要尝试调任何 GEO 后端的"帮我生文"接口——它们已经下线。
- **始终通过 MCP 工具**：除了写 markdown 本身（属于主对话的输出），所有数据流动经过 `mcp__geo__*`。
- **title vs markdown_content**：title 单字段传，**不要**在 markdown 顶部再写一遍 `# 标题`（save 端会把整块 markdown 转成 Tiptap 段落树，重复标题会进段落里）。
- **失败 fallback**：单次失败 → 跳过这个 qid 而不是停整个 Loop；配图失败不影响主流程。
- **配图机制（主推 vs 陪衬）**：`ai_illustrate_article` 一次调用就把「主推栏目（餐厅养成记）+ 全部陪衬栏目」一起喂给 AI，AI 按正文里点到的游戏分别匹配各自栏目插图——**主推和陪衬游戏都会配**，不用为陪衬游戏单独再调一次。陪衬出图的前提是：① 正文用规范中文名点到了那款游戏；② 该游戏在图库里有对应陪衬栏目且有图（dev 库现有 554 个有图的陪衬栏目，覆盖很全）。
- **图库无图时走百度（`web_fallback=True`，默认开）**：上面伪码调用 `ai_illustrate_article` 时已默认带 `web_fallback=True`。开了它，库里【完全没有】对应栏目的全新游戏不再被放弃——AI 用规范中文名点名后，GEO 自动建一个陪衬栏目 + 走百度（千帆 AI 搜索）联网搜一张横版图补进去，这样图库里没有的新游戏也配得上图。**前提**：app 容器配了 `GEO_BAIDU_API_KEY`；这是 best-effort——key 缺失 / 网络失败 / 搜不到横版图时静默跳过、不报错（行为退化成跟关掉时一样，绝不会让配图或主流程失败）。所以放心默认开着：配了 key 多一层联网兜底，没配也无副作用。
- **飞书节制**：开始、结束、严重失败发；中间进度不发。
- **policy_safety 维度从严**：合规分 < 80 一律不能给 approved（即使总分高）——人审兜底但减轻审核负担。
- **写作风格**：餐厅养成记矩阵偏轻松实用，避免"开篇一段宏大的引入"——直接进主题。模板里的具体指引以模板为准。
