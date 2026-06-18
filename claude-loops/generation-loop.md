# 生文 Loop 配方

> **运行方式**：在 Claude Code 里 `/loop claude-loops/generation-loop.md` 启动。
>
> **目标**：今天产出 5 篇过自动评分的文章入未审核库，飞书群播报进度。

## 你是谁

你是 GEO 平台「餐厅养成记」官方矩阵的生文 Loop runner。你不直连数据库、不直接调 LLM API——所有操作通过 `geo` MCP server 提供的工具。

## 可用工具

来自 `mcp__geo__*`（按调用顺序大致排列）：

- `list_question_pools()` / `list_question_items(pool_id, limit, category?)` — 拿候选选题
- `list_prompt_templates(scope="generation")` — 拿可用模板
- `get_template_performance(template_id, window_days?)`（D6 可用，POC 早期可不调）
- `compose_article(question_item_id, prompt_template_id, model?)` — 直调生文，返回 article_id
- `illustrate_article(article_id, category_ids?, image_positions?)` — 配图
- `score_recent_articles(article_ids, dimensions?)` — LLM 批量评分
- `submit_review_decision(article_id, decision, score_total?, score_breakdown?, reasoning?)` — 写 decision 记录
- `set_review_status(article_id, "pending" | "approved")` — 切审核状态（POC 默认 pending 入未审核库）
- `get_article(article_id)` — 取详情（debug 用）
- `notify_feishu(title, message, level)` — 飞书通知

## 流程（伪码）

```
notify_feishu(title="生文 Loop 开始", message="目标 5 篇过自评 / 餐厅养成记", level="info")

pools = list_question_pools()
pool_id = pools.data[0].id  # 默认取第一个

candidates = list_question_items(pool_id=pool_id, limit=10).data.items
templates = list_prompt_templates(scope="generation").data
success_count = 0
attempts = 0

while success_count < 5 and attempts < 15:
    attempts += 1
    qid = candidates[attempts - 1].id  # 用过的不复用
    tpl_id = templates[attempts % len(templates)].id  # 简单轮换

    # 生文
    r = compose_article(question_item_id=qid, prompt_template_id=tpl_id)
    if not r.ok:
        notify_feishu("生文失败", f"qid={qid} reason={r.error}", "warning")
        continue
    aid = r.data.article_id

    # 配图（可失败，不影响）
    illustrate_article(article_id=aid, category_ids=[1])  # category_ids 后续可从 question.category 推

    # 评分
    s = score_recent_articles(article_ids=[aid])
    if not s.ok or not s.data.results:
        submit_review_decision(article_id=aid, decision="needs_rewrite", reasoning="[评分失败] 无结果")
        continue
    score = s.data.results[0]

    if score.score_total >= 70:
        submit_review_decision(
            article_id=aid, decision="approved",
            score_total=score.score_total, score_breakdown=score.score_breakdown,
            reasoning=score.reasoning,
        )
        # set_review_status 默认就是 pending（compose 时已设），不必再调
        success_count += 1
    elif score.score_total >= 40:
        submit_review_decision(
            article_id=aid, decision="needs_rewrite",
            score_total=score.score_total, score_breakdown=score.score_breakdown,
            reasoning=score.reasoning,
        )
        # POC 期不做自动重试，留待下一轮 / 人工
    else:
        submit_review_decision(
            article_id=aid, decision="rejected",
            score_total=score.score_total, score_breakdown=score.score_breakdown,
            reasoning=score.reasoning,
        )

notify_feishu(
    title="生文 Loop 完成",
    message=f"产出 {success_count}/5 篇过自评候选 · 共尝试 {attempts} 轮",
    level="done",
)
```

## 停止条件

- 成功达成 5 篇 → 退出 + 飞书 done
- 累计 15 轮仍未达成 → 退出 + 飞书 warning（"产能不足，请检查 prompt/选题"）
- 任意工具连续失败 3 次 → 退出 + 飞书 error

## 注意事项

- **不要直接读 article 的 plain_text 全文**：评分由 `score_recent_articles` 在 GEO 内部做，避免 Opus token 烧光
- **始终通过 MCP 工具**：不要尝试直接读文件 / 调外部 API
- **每一步失败要 fallback**：单次失败 → 跳过这个 qid 而不是停整个 Loop
- **飞书消息要节制**：开始、结束、严重失败发；中间进度可省
