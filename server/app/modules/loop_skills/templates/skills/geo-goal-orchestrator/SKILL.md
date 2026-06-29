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
| `tpl_id` | 用户写 `生文提示词Id=<数字>` / `生文提示词 #<数字>` / `用生文提示词 <数字>`（也兼容旧写法 `tpl=<数字>` / `模板 #<数字>`）→ 写死该提示词，全部 N 篇都用它；不写则按 attempts 轮转所有提示词 | `None`（按轮转） |
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
    echo(f"[累计通过] 今日已过审文章数：{netto.count}/{target.N} 篇")

    if netto.count >= target.N:
        notify_feishu("生文流程完成", f"累计通过 {netto.count}/{target.N}，共耗时 {minutes} 分钟", "done")
        return SUCCESS

    if attempts >= 3 * target.N:
        notify_feishu("生文流程中止", f"已达尝试轮数上限，累计通过 {netto.count}/{target.N}", "warning")
        return ABORT
    if len(used_qids) >= len(candidates):
        notify_feishu("生文流程中止", f"候选问题用完，累计通过 {netto.count}/{target.N}", "warning")
        return ABORT
    if estimated_main_tokens > 80_000:
        notify_feishu("生文流程中止", f"主对话内存预算触顶，累计通过 {netto.count}/{target.N}", "warning")
        return ABORT
    if consecutive_mcp_fail >= 3:
        notify_feishu("生文流程中止", "接口连续失败 3 次，请检查服务连接 / 凭证", "error")
        return ABORT

    # === 选 next qid（避重）===
    qid = pick_next_qid(candidates, used_qids)
    used_qids.add(qid)
    # target.tpl_id 优先（用户在 /goal 里显式指定了生文提示词）；缺省按 attempts 轮转所有提示词。
    tpl_id = target.tpl_id or templates[attempts % len(templates)].id
    attempts += 1

    # === Writer subagent（fresh context, Opus）===
    matrix_suffix = "" if target.matrix_code == "" else "-" + target.matrix_code
    writer_result = Agent(
        subagent_type="general-purpose",
        description=f"改写文章（问题 #{qid}）",
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
        echo(f"[第 {attempts}/{3*target.N} 轮] 改写失败：{parsed.error}")
        if is_mcp_error(parsed.error):
            consecutive_mcp_fail += 1
        continue
    consecutive_mcp_fail = 0
    article_id = parsed["article_id"]
    echo(f"[第 {attempts}/{3*target.N} 轮] 改写完成（文章 #{article_id}），评审中 …")

    # === Verifier subagent（fresh context, Haiku）===
    verifier_result = Agent(
        subagent_type="general-purpose",
        model="haiku",
        description=f"评审文章 #{article_id}",
        prompt=f"""Read .claude/skills/geo-article-verifier/SKILL.md and follow it strictly.

Input: article_id={article_id}, qid={qid}, tpl_id={tpl_id}

Output: ONLY a single-line JSON object as the final message, like:
  {{"decision": "approved", "score_total": 82}}
No other text.""",
    )
    parsed_v = parse_last_json_line(verifier_result.stdout)
    if "error" in parsed_v:
        echo(f"[第 {attempts}/{3*target.N} 轮] 评审失败，文章 #{article_id} 留待人工审核")
        continue
    echo(f"[第 {attempts}/{3*target.N} 轮] 评审结果：{parsed_v.decision}　分数 {parsed_v.score_total}")
    # 不管 decision 是什么循环都继续——netto 查询会反映真实通过数
```

# 进度日志（必须 echo 这些短行）

```
[启动检查] 问题池：<name>　目标：<N> 篇　矩阵：<code|默认>　生文提示词：<#id 写死|轮转>　✓
[第 k/3N 轮] 选题：问题 #<id> → 改写中 …
[第 k/3N 轮] 改写完成（文章 #<id>），评审中 …
[第 k/3N 轮] 评审结果：<d>　分数 <total>
[累计通过] 今日已过审文章数：<count>/<N> 篇
[完成|中止] 累计通过 <count>/<N>，共耗时 <m> 分钟，原因：<...>
```

# 主对话叙述规范（强制）

你向用户叙述本次 /goal 运行时，**只能用中文 + 上面进度日志的固定格式**。
绝对不要在叙述里出现以下英文 / 内部术语（左侧错例，右侧用法）：

| ❌ 不要说 | ✅ 改成 |
|---|---|
| orchestrator | 编排员 / 我 |
| netto / 净产出 | 累计通过数 |
| goal-verifier | 评审员 |
| pool / pool_id | 问题池 |
| qid | 问题 #编号 |
| tpl_id | 生文提示词 #编号 |
| article_id | 文章 #编号 |
| matrix / matrix_code | 矩阵 |
| N | 目标 X 篇 |
| writer / verifier | 改写员 / 评审员 |
| subagent | 子助手 |
| attempts | 已尝试轮数 |
| 主对话内存预算（裸说 token） | 主对话内存预算 |

**反例**（千万别这样说）：

> 启动 orchestrator。N=5 国风。先看 netto，已知国风候选 qid=80/81/82/83。

**正例**：

> 开始执行 /goal：目标 5 篇国风游戏文章。先看一下累计通过数，
> 当前候选问题：#80 / #81 / #82 / #83（共 4 条）。

**例外**（这些保留原样，因为是 Claude Code 自己加的或后端契约）：
- `Skill(...)` / `Agent(...)` / `Called geo` 前缀 — Claude Code UI 自动加
- MCP 工具调用 `save_article(question_item_id=80, prompt_template_id=11)` — 工具签名
- 文件路径、URL、内部命令行 — 保持原样

> 这一段比技术契约更重要——使用者看不懂"netto"，但他们花 10 分钟跑 /goal 时
> 主对话是他们唯一的进度反馈。不要让英文 / 缩写打断他们的注意力。

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
- 用户 Ctrl-C → 主对话 echo `[已中断] 已落库 X 篇，累计通过 Y/N 篇，下次 /goal 会接力`（不发飞书）

# 三个不变式（硬约束）

1. **单点失败不杀 loop**——除非 MCP 连续 3 次
2. **落库失败 ≠ 验证失败**：save_article 失败 → qid 加入 used_qids 不重试；verifier 失败 → 文章留 pending 由人审
3. **netto 是唯一计数事实**：subagent 自报"我写好了"都不算数，必须查 MCP
