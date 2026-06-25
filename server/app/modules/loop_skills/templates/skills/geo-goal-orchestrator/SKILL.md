---
name: geo-goal-orchestrator
description: Use when /goal command is invoked in geo-collab repo. Drives the
  netto-verified article generation loop with Ralph-style fresh-context writer
  + Haiku verifier subagents. Owns natural-language goal parsing, candidate
  question selection, retry/budget ceiling, and Feishu reporting.
---

# Role

你是 `/goal` 命令的 orchestrator。在**主对话**里执行；写作 + 评分通过
`Agent` 工具下发到 fresh-context subagent。你**不写文章、不评分**——你只
做：sanity check → 解析目标 → 调度子 agent → 查 GEO 拿净产出 → 决定继续/退出
→ 飞书播报。

# Required Checklist (per /goal invocation)

1. **Sanity check** — 调 `list_question_pools()`；失败立即退出 + 提示
   "请按 docs/mcp-setup-notes.md 配 ~/.claude.json 的 mcpServers.geo"
2. **解析目标** — 从用户自由文本抽取 `{N, pool_id, topic_hint, matrix_code, model_label}`
3. **抓 candidates + templates** — `list_question_items` + `list_prompt_templates`
4. **进入主循环**（见下）
5. **退出前飞书播报** —— `notify_feishu(title, message, level)`，level ∈
   `{"done", "warning", "error"}`

# Goal Parsing 规则

| 字段 | 抽取规则 | 缺省 |
|---|---|---|
| `N` | 文中数字 + 量词（"5 篇" / "8 个" / "10 件" 都接受） | `5` |
| `pool_id` | 用户提到池名（"wenti01" / "问题池" 等） → 匹配 `list_question_pools` 里的 `name` | 第一个 `pending_count > 0` 的池 |
| `topic_hint` | 题材关键词（"国风" / "治愈" / "解谜" 等） | `None` |
| `matrix_code` | 用户写 `matrix=<code>` 才设 | `""`（用默认 geo-article-writer） |
| `model_label` | 固定 | `"claude-goal-opus-4-7"` |

# 主循环（每轮）

```pseudo
while True:
    # === 退出闸门（优先级从高到低）===
    netto = list_today_loop_articles(
        decided_by="claude-goal-verifier",
        decision="approved",
        since_hours=24,
        model_label=target.model_label,
    ).data
    echo(f"[净产出] 今日通过 goal 评审的文章数: {netto.count}/{target.N}")

    if netto.count >= target.N:
        notify_feishu("生文 Loop 完成", f"净产出 {netto.count}/{target.N}, 共耗时 {minutes}m", "done")
        return SUCCESS

    if attempts >= 3 * target.N:
        notify_feishu("生文 Loop 中止", f"attempts ceiling, 净产出 {netto.count}/{target.N}", "warning")
        return ABORT
    if len(used_qids) >= len(candidates):
        notify_feishu("生文 Loop 中止", f"候选问题用尽, 净产出 {netto.count}/{target.N}", "warning")
        return ABORT
    if estimated_main_tokens > 80_000:
        notify_feishu("生文 Loop 中止", f"token 预算触线, 净产出 {netto.count}/{target.N}", "warning")
        return ABORT
    if consecutive_mcp_fail >= 3:
        notify_feishu("生文 Loop 中止", "MCP 连续失败 3 次, 请检查后端/token", "error")
        return ABORT

    # === 选 next qid（避重）===
    qid = pick_next_qid(candidates, used_qids)
    used_qids.add(qid)
    tpl_id = templates[attempts % len(templates)].id
    attempts += 1

    # === Writer subagent（fresh context, Opus）===
    matrix_suffix = "" if target.matrix_code == "" else "-" + target.matrix_code
    writer_result = Agent(
        subagent_type="general-purpose",
        description=f"写一篇文章 qid={qid}",
        prompt=f"""Read .claude/skills/geo-article-writer{matrix_suffix}/SKILL.md and follow it strictly.

Input: qid={qid}, tpl_id={tpl_id}, model_label={target.model_label}

Output: ONLY a single-line JSON object as the final message, like:
  {{"article_id": 824, "title": "..."}}
or on failure:
  {{"error": "..."}}
No other text.""",
    )
    parsed = parse_last_json_line(writer_result.stdout)
    if "error" in parsed:
        echo(f"[第 {attempts}/{3*target.N} 轮] 改写失败: {parsed.error}")
        if is_mcp_error(parsed.error):
            consecutive_mcp_fail += 1
        continue
    consecutive_mcp_fail = 0
    article_id = parsed["article_id"]
    echo(f"[第 {attempts}/{3*target.N} 轮] 改写完成 article_id={article_id}, 评审中 …")

    # === Verifier subagent（fresh context, Haiku）===
    verifier_result = Agent(
        subagent_type="general-purpose",
        model="haiku",
        description=f"评分 article_id={article_id}",
        prompt=f"""Read .claude/skills/geo-article-verifier/SKILL.md and follow it strictly.

Input: article_id={article_id}, qid={qid}, tpl_id={tpl_id}

Output: ONLY a single-line JSON object as the final message, like:
  {{"decision": "approved", "score_total": 82}}
No other text.""",
    )
    parsed_v = parse_last_json_line(verifier_result.stdout)
    if "error" in parsed_v:
        echo(f"[第 {attempts}/{3*target.N} 轮] 评审失败, article {article_id} 留 pending 由人审")
        continue
    echo(f"[第 {attempts}/{3*target.N} 轮] 评审 决策={parsed_v.decision} 总分={parsed_v.score_total}")
    # 不管 decision 是什么循环都继续——netto 查询会反映真实通过数
```

# 进度日志（必须 echo 这些短行）

```
[快检] pool=<name> N=<N> matrix=<code|默认> 通过
[第 k/3N 轮] 选题 qid=<id> → 改写中 …
[第 k/3N 轮] 改写完成 article_id=<id>, 评审中 …
[第 k/3N 轮] 评审 决策=<d> 总分=<total>
[净产出] 今日通过 goal 评审的文章数: <count>/<N>
[完成|中止] 净产出 <count>/<N>, 共耗时 <m>m, 原因=<...>
```

# Helper 定义（消除歧义）

| Helper | 定义 |
|---|---|
| `matrix_suffix(code)` | `code == ""` → `""`；否则 `"-" + code` |
| `topic_hint_match(item, hint)` | 不区分大小写子串匹配；`hint in item.question_text` OR `hint in item.category` |
| `pick_next_qid(candidates, used_qids)` | 按 `candidates` 顺序返第一个不在 `used_qids` 的；全用过返 None |
| `is_mcp_error(error)` | `mcp__geo__*` 返回 `{ok:false, error}` 或抛 401/502/5xx/超时 → True |
| `estimated_main_tokens` | 粗估 `attempts * 8000`；Claude Code 暴露精确 API 后再换 |
| `parse_last_json_line(text)` | 找最后一行能 `json.loads` 解析的；找不到返 `{"error": "no JSON in subagent output"}` |

# Stop / Budget Rules（再次强调）

- `netto.count >= N` → SUCCESS（飞书 done）
- `attempts >= 3N` → ABORT（飞书 warning）
- candidates 用尽 → ABORT（飞书 warning）
- 估算主对话 token > 80k → ABORT（飞书 warning）
- 连续 MCP 错误 >= 3 → ABORT（飞书 error）
- 用户 Ctrl-C → 主对话 echo `[interrupted] 已落库 X 篇, 净产出 Y/N, 下次 /goal 会接力`（不发飞书）

# 三个不变式（硬约束）

1. **单点失败不杀 loop**——除非 MCP 连续 3 次
2. **落库失败 ≠ 验证失败**：save_article 失败 → qid 加入 used_qids 不重试；verifier 失败 → 文章留 pending 由人审
3. **netto 是唯一计数事实**：subagent 自报"我写好了"都不算数，必须查 MCP
