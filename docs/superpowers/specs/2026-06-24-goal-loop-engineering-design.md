# `/goal` Slash Command · 生文 Loop Engineering 升级 · 设计

- 状态：设计稿（v0），待 review 后进入 writing-plans 阶段
- 日期：2026-06-24
- 上游来源：[`2026-06-18-claude-code-loop-with-geo-mcp-design.md`](./2026-06-18-claude-code-loop-with-geo-mcp-design.md)（POC 已落地）+ [`claude-loops/generation-loop.md`](../../../claude-loops/generation-loop.md)（当前生文 Loop 实现）
- 受众：实施 plan 评审 + 团队复用规范对齐
- 不动的部分：MCP server 架构 / 17 atomic tools / 鉴权 / 飞书 webhook 这些 06-18 已经定的内容
- 动的部分：生文 Loop 从「单会话顺序写 N 篇」升级到「`/goal` 编排 + Ralph 风格 fresh-context subagent + 独立 verifier + 净产出验证」

---

## 🟡 分发模型（重要）

**`.claude/` 不入 git——每位同事在自己电脑上各自维护一份。**

仓库里的 `.gitignore` blanket 忽略 `.claude/`。本 PR 只把**系统层**的东西
入库（后端 service / endpoint / MCP 工具 + 本设计稿 + 实施 plan），不入库
slash command / SKILL.md / .claude/README.md。

| 谁负责 | 干什么 | 落地 |
|---|---|---|
| **平台** (本 PR) | 后端 + MCP 工具 + 设计 / plan 文档 | `git push origin docs/goal-loop-engineering` |
| **每位使用者** (本地一次性) | 把本设计稿 §5 的 3 个 SKILL.md + plan 的 slash command + README **抄到本地** `.claude/` | `git pull` 拿不到，要人工 / out-of-band 同步 |

为什么这样：
- skill 是各人偏好（写作风格、评分门槛、矩阵口味）；fork 一份在本地改最自由
- skill 写得好坏不影响后端契约——只要 MCP 工具 + ground-truth 查询稳定，每个人的 skill 各跑各的不打架
- 避免「skill 上游一改下游所有人被推 PR」的耦合，符合 Loop Engineering「写自己的 loop」纪律

本设计稿 §5 的 SKILL.md 完整内容是**参考实现**——你可以照抄，也可以按需调整。
plan 的 Tasks 4-9 同理。

---

---

## 0. 一句话

把生文 Loop 从「主对话连续写 5 篇、context 越来越脏、写作者自评有 bias」改造成「`/goal "..."` 一句话启动 → 主对话只做调度 + 验收 → 每篇文章由独立 writer subagent 在全新 context 里写 → 由独立 verifier subagent（Haiku）评分 → 主对话每轮调一个新 MCP 工具查 GEO 拿净产出 ground truth 决定是否继续」。

---

## 1. 锁定决策（brainstorming 已答）

| # | 决策 | 选项 |
|---|------|------|
| 1 | `/goal` 目标输入形态 | **自然语言**（"今天产出 5 篇国风游戏文章"）—— 主对话解析后驱动 loop |
| 2 | 工作范围 | **只管生文**（端到入未审核库）—— 分发 / 周报继续走独立 loop |
| 3 | 迭代架构 | **Ralph 风格**——每篇=一轮全新 context 的 writer subagent |
| 4 | Verifier 边界 | **独立 verifier subagent**（writer ≠ verifier，结构性隔离） |
| 5 | 停止条件 | **净产出验证**——查 GEO 拿"今天 verifier 通过的 loop 文章数"作为唯一 ground truth |
| 6 | Budget ceiling | `attempts ≤ 3N` + 主对话 token 预算 ~80k 触线退出 |
| 7 | 复用机制 | 项目级 `.claude/`（跟 git 走，同事 `git pull` 即可用） |

---

## 2. 架构总览

### 2.1 文件布局

**入库部分**（本 PR 提交）：

```
server/app/modules/auto_review/
├── service.py                            # +list_recent_decisions(...)
└── router.py                             # +GET /api/articles/today-loop-decisions

server/mcp/tools/catalog.py               # +list_today_loop_articles MCP tool

server/tests/
└── test_auto_review_loop_query.py        # 新增：service 单测 + endpoint 集成测试

docs/superpowers/specs/2026-06-24-goal-loop-engineering-design.md   # 本设计稿
docs/superpowers/plans/2026-06-24-goal-loop-engineering.md          # 实施 plan

claude-loops/generation-loop.md           # 保留不删，作为「不走 /goal、直接 /loop」的旧路径参考
```

**本地不入库部分**（每位使用者各自维护，`.claude/` 已被 `.gitignore` blanket 忽略）：

```
.claude/                                  # 本地存在但 git 不追踪
├── README.md                             # 同事 onboarding 入口（参考 §4）
├── commands/
│   └── goal.md                           # /goal slash command 定义（参考 plan Task 8）
└── skills/
    ├── geo-goal-orchestrator/SKILL.md    # 主对话调度（参考 §5.1）
    ├── geo-article-writer/SKILL.md       # writer subagent（参考 §5.2）
    └── geo-article-verifier/SKILL.md     # verifier subagent（参考 §5.3）
```

### 2.2 组件协作图

```
 用户 ──/goal "今天产出 5 篇国风游戏文章"──▶ 主对话（orchestrator）
                                                │
                                                │ 1. 装载 geo-goal-orchestrator skill
                                                │ 2. sanity check（MCP 通不通）
                                                │ 3. 解析自然语言 → {N, pool_id, topic_hint, matrix, model_label}
                                                │ 4. 抓 candidates + templates
                                                │
                                                ├── 循环开始 ──▶ list_today_loop_articles → netto.count
                                                │                │
                                                │           (netto.count ≥ N? → SUCCESS 退出)
                                                │           (attempts ≥ 3N? → ABORT 退出)
                                                │           (token 预算 > 80k? → ABORT 退出)
                                                │           (consecutive_mcp_fail ≥ 3? → ABORT 退出)
                                                │                │
                                                │  pick next qid │（避重：used_qids set）
                                                │                ▼
                                                │  Agent tool spawn ─▶ Writer subagent (Opus, fresh ctx)
                                                │   (load geo-article-writer)  │
                                                │                              │ list_question_items
                                                │                              │ list_prompt_templates
                                                │                              │ save_article
                                                │                              │ illustrate_article (best-effort)
                                                │                              ▼
                                                │   ◀─ 返回 {"article_id": int, "title": str} ──
                                                │                              
                                                │  Agent tool spawn ─▶ Verifier subagent (Haiku, fresh ctx)
                                                │   (load geo-article-verifier)│
                                                │                              │ get_article
                                                │                              │ list_question_items（反查 qid）
                                                │                              │ list_prompt_templates（反查 tpl_id）
                                                │                              │ submit_review_decision
                                                │                              ▼
                                                │   ◀─ 返回 {"decision": str, "score_total": int} ──
                                                │
                                                └── 循环结束 ──▶ notify_feishu 播报最终结果
```

### 2.3 关键设计点

1. **Skill 装载位置**：`.claude/skills/` 项目级目录。**`.claude/` 被 `.gitignore` blanket 忽略**，每位使用者在本地各自维护一份；新同事按 §4.2 onboarding 流程从本 spec / plan 抄一份到本地。
2. **Subagent 类型**：用 `Agent` 工具的 `general-purpose` 类型；每个调用的 prompt 第一行是 `Read .claude/skills/<name>/SKILL.md and follow it strictly`，把 skill 当作 subagent 的「playbook」装载。
3. **Verifier 模型独立**：orchestrator 在 Opus 主对话里跑（最强模型做规划 + 验收），writer subagent 默认继承（Opus），verifier subagent 显式 `model: haiku`——Loop Engineering「writer ≠ verifier」的结构性隔离落到模型层。
4. **净产出是唯一权威**：主对话不信 writer/verifier 的自报，每轮开头调 `list_today_loop_articles` 查 GEO 数据库拿 ground truth。
5. **可中断 + 可接力**：Ctrl-C 之后已 commit 的 article 完好；第二次 `/goal` 因为查的是 "today since_hours=24"，自动续上。**不需要额外 resume 机制**——这是 netto 验证设计的副产品。

---

## 3. `/goal` Slash Command 行为

### 3.1 输入

```
/goal <自由文本>
```

例：
- `/goal 今天产出 5 篇国风游戏文章`
- `/goal 用 wenti01 池产出 8 篇`
- `/goal matrix=jiangnan 5 篇`（指定矩阵）

### 3.2 主循环伪码

```pseudo
# 0. Sanity check
try:
    pools = list_question_pools()
except McpError as e:
    退出 + 提示「请按 docs/mcp-setup-notes.md 配 ~/.claude.json 的 mcpServers.geo」

# 1. 解析自然语言目标
target = parse_goal(user_text)
# target = {
#   N: int                  # 数字，默认 5
#   pool_id: int            # 池名匹配 list_question_pools 里的 name；缺省取 pools[0]
#   topic_hint: str | None  # 题材关键词；用于过滤 candidates 的 question_text
#   matrix_code: str        # 默认 "" = 用 geo-article-writer；带 "matrix=X" 用 geo-article-writer-X
#   model_label: str        # 固定 "claude-goal-opus-4-7"，写入 metrics.writer_model 用于 netto 查询
# }

# 2. 抓 candidates + templates
candidates = list_question_items(pool_id=target.pool_id, limit=min(3*N, 50)).data
if target.topic_hint:
    candidates = [c for c in candidates if topic_hint_match(c, target.topic_hint)]
templates = list_prompt_templates(scope="generation").data
used_qids = set()
attempts = 0
consecutive_mcp_fail = 0

# 3. 主循环
while True:
    # 3a. 退出闸门（优先级从高到低）
    netto = list_today_loop_articles(
        decided_by="claude-goal-verifier",
        decision="approved",
        since_hours=24,
        model_label=target.model_label,
    ).data
    if netto.count >= target.N:
        notify_feishu("生文 Loop 完成", f"净产出 {netto.count}/{target.N}", "done")
        return SUCCESS
    if attempts >= 3 * target.N:
        notify_feishu("生文 Loop 中止", f"attempts ceiling, 净产出 {netto.count}/{target.N}", "warning")
        return ABORT
    if len(used_qids) >= len(candidates):
        notify_feishu("生文 Loop 中止", "候选问题用尽", "warning")
        return ABORT
    if estimated_main_tokens > 80_000:
        notify_feishu("生文 Loop 中止", f"token 预算触线, 净产出 {netto.count}/{target.N}", "warning")
        return ABORT
    if consecutive_mcp_fail >= 3:
        notify_feishu("生文 Loop 中止", "MCP 连续失败 3 次", "error")
        return ABORT

    # 3b. 选 next qid（避重）
    qid = pick_next_qid(candidates, used_qids)
    used_qids.add(qid)
    tpl_id = (target.tpl_id or templates[attempts % len(templates)].id)
    attempts += 1

    # 3c. Writer subagent（fresh context）
    writer_result = Agent(
        subagent_type="general-purpose",
        description=f"写一篇文章 qid={qid}",
        prompt=f"""Read .claude/skills/geo-article-writer{matrix_suffix(target.matrix_code)}/SKILL.md
            and follow it strictly.
            Input: qid={qid}, tpl_id={tpl_id}, model_label={target.model_label}
            Output: only {{"article_id": int, "title": str}} as final message.""",
    )
    if writer_result.error:
        if is_mcp_error(writer_result.error):
            consecutive_mcp_fail += 1
        echo("[round k/3N] writer 失败, 跳过")
        continue
    consecutive_mcp_fail = 0

    # 3d. Verifier subagent（fresh context, Haiku）
    verifier_result = Agent(
        subagent_type="general-purpose",
        model="haiku",
        description=f"评分 article_id={writer_result.article_id}",
        prompt=f"""Read .claude/skills/geo-article-verifier/SKILL.md and follow it strictly.
            Input: article_id={writer_result.article_id}, qid={qid}, tpl_id={tpl_id}
            Output: only {{"decision": str, "score_total": int}} as final message.""",
    )
    if verifier_result.error:
        echo(f"[round k/3N] verifier 失败, 文章 {writer_result.article_id} 留 pending 由人审")
        continue
    # 不论 verifier 给什么 decision，循环继续；netto 查询会反映真实通过数
```

### 3.2.1 Helper 函数定义（消除伪码歧义）

伪码里出现的 helper 含义如下，避免实施时各种解读：

| Helper | 定义 |
|---|---|
| `matrix_suffix(code)` | `code == ""` → 返回 `""`（指向默认 skill）；否则返回 `"-" + code`（指向 `geo-article-writer-<code>`） |
| `topic_hint_match(item, hint)` | 不区分大小写的子串匹配；命中条件：`hint in item.question_text` **或** `hint in item.category`（任一即可） |
| `pick_next_qid(candidates, used_qids)` | 按 `candidates` 列表顺序返回第一个**不在** `used_qids` 里的 `qid`；若全部用过返回 `None`（触发 candidates 用尽分支） |
| `is_mcp_error(error)` | error 是 `mcp__geo__*` 工具返回的 `{ok: false, error: str}` 或抛出的 401/502/5xx/超时 → `True`；其它（如 subagent 自己 crash / JSON parse 失败）→ `False` |
| `estimated_main_tokens` | 粗略估算：`attempts * 8000`（每轮 writer + verifier 消息往返约 8k 主对话 token）；准确测量等 Claude Code 暴露 token API 后再换 |

### 3.3 进度日志格式（强制）

每轮在主对话 echo 这些短行，让同事看见进度而不是黑盒：

```
[orchestrator] sanity ✓ pool=问题池 N=5 matrix=default model_label=claude-goal-opus-4-7
[round 1/15] qid=123 → writer …
[round 1/15] writer 交稿 article_id=824, verifier …
[round 1/15] verifier decision=approved score=82 breakdown=f85/r79/s80/p90
[netto] today approved by goal-verifier: 1/5
[round 2/15] qid=124 → writer …
...
[done] 净产出 5/5, 共耗时 12m, 飞书已播报
```

### 3.4 退出原因 → 飞书 level 对应

| 退出原因 | 飞书 level | 消息体 |
|---|---|---|
| `netto.count ≥ N` | `done` | `净产出 {count}/{N}, 共耗时 {m}m` |
| `attempts ≥ 3N` | `warning` | `attempts ceiling, 净产出 {count}/{N}, 请检查 prompt/选题` |
| candidates 用尽 | `warning` | `候选问题用尽, 净产出 {count}/{N}` |
| token 预算触线 | `warning` | `token 预算触线, 净产出 {count}/{N}` |
| MCP 连续失败 3 次 | `error` | `MCP 连续失败, 净产出 {count}/{N}, 请检查 GEO 后端 / token` |
| 用户 Ctrl-C | （不发飞书） | 主对话 echo `[interrupted] 已落库 X 篇, 净产出 Y/N, 下次跑 /goal 会接力` |

---

## 4. 同事使用 + 复用

### 4.1 三类使用者

| 角色 | 想做什么 | 接触面 | 不用关心 |
|---|---|---|---|
| **运营** (90%) | 跑 `/goal` 出 5 篇文章 | `/goal` 一句话 | skill 内部 / MCP 签名 / subagent 编排 |
| **写作风格调优** | 改矩阵风格 / 加新矩阵 | `geo-article-writer/SKILL.md` 的 `## 矩阵特例` 段 | orchestrator 调度 / verifier 评分 |
| **平台扩展** | 加新 stop 条件 / 评分维度 / MCP 工具 | orchestrator skill + 后端 `auto_review/service.py` + `mcp/tools/catalog.py` | writer 内部 |

### 4.2 冷启动 onboarding（写进 `.claude/README.md`）

```
# 在 geo-collab 仓库里使用 /goal（5 步）

1. 把本设计稿 §5 的 3 个 SKILL.md + plan Task 8/9 的 slash command + README
   **抄到本地** .claude/（仓库不追踪此目录；你也可以从已配好的同事那拿一份 zip）
2. 一次性配置（每台机器一次）
   - 打开 ~/.claude.json，加 mcpServers.geo 段（参考 docs/mcp-setup-notes.md）
   - 把后端管理员发的 GEO_MCP_TOKEN 填到 headers.X-MCP-Token
3. 重启 Claude Code
4. 在 Claude Code 里输入 /mcp，确认 geo server 显示 "connected"
5. 在 Claude Code 里输入：
   /goal 帮我今天产出 5 篇关于国风游戏的文章

之后会自动跑（约 10-20 分钟）；完成后飞书群会有播报。
```

### 4.3 同事看见什么 / 不看见什么

**会出现在主对话**：见 §3.3 日志格式 —— 干净的状态条 + 关键数字。

**不会出现在主对话**：
- writer subagent 内部写文章的草稿（在子 context 里，不污染主对话）
- verifier 的评分推理
- MCP 工具的原始 JSON 回包（orchestrator 自己解析后只保留关键字段）

这是 Loop Engineering「主对话 context 干净 → 可以连续跑很久不衰减」的具体体现。

### 4.4 复用 / 定制路径

| 想改的事 | 怎么改 |
|---|---|
| 默认 N | 直接在 `/goal` 后说话：`/goal 今天 8 篇` |
| 默认问题池 | 直接说：`/goal 用 wenti01 池产出 5 篇` |
| 加新内容矩阵（餐厅养成记之外） | 复制 `.claude/skills/geo-article-writer/` 为 `geo-article-writer-<code>/`，**只改 `## 矩阵特例` 段**；调用时 `/goal matrix=<code> ...` |
| 单独写一篇（不走 loop） | 主对话直接 `Skill geo-article-writer` 进入写作模式手动跟它配合写——**不调用 verifier、不进 netto 计数**，给写作风格调优用 |
| 改评分门槛 | 改 `geo-article-verifier/SKILL.md` 的「决策门槛」段 |
| 加新评分维度 | 改 verifier skill 的「评分维度」段；如要进 score_breakdown，新键即可，后端 JSON 列不限 schema |

### 4.5 常见排障

| 症状 | 原因 | 处理 |
|---|---|---|
| `/goal` 启动后立刻退出，提示 "MCP 不可用" | `~/.claude.json` 没配 / token 错 | 走 §4.2 第 2 步 |
| 跑到一半 attempts 用完但 netto=0 | verifier 一直不给 approved | 单独 `Skill geo-article-writer` 试一题看写作质量；写作没问题就是 verifier 门槛太严 |
| writer 报 `save_article 415` | markdown 里塞了不支持的元素 | 给后端工程师看错误 detail |
| 配图全部失败 | stock_category 没配 / category_id 不对 | 不致命，文章已落库；联系平台扩展同事配 |
| 飞书没收到播报 | webhook 没配 / 配错环境 | 检查后端 `GEO_FEISHU_WEBHOOK_URL` |

### 4.6 反向约束：skill / command 必须满足的

1. **Self-contained**：每个 SKILL.md 顶部 description 一句话能让陌生人看懂；body 不依赖本对话上下文（不写"按之前说的…"）。
2. **零猜测**：所有外部依赖（MCP 工具、环境变量、文件路径）显式列出，缺哪个就**显式报错**不悄悄继续。
3. **可观测**：每个关键步骤都在主对话 echo 一行短日志，同事看到进度不是黑盒。
4. **可中断**：Ctrl-C 后已落库的 article 不丢；下次 `/goal` 接力。

---

> **⚠️ Source-of-truth 已迁移**
>
> 本节的 SKILL.md 内容已经搬到 `server/app/modules/loop_skills/templates/` 入
> git，作为服务端分发正本。本节内容仅作历史快照保留——以服务端 templates/
> 为准。
>
> 分发方式见 [`2026-06-24-loop-skill-distribution-design.md`](./2026-06-24-loop-skill-distribution-design.md)。
>

## 5. 三个 SKILL.md 骨架

写法遵循 superpowers `writing-skills` 规范：YAML frontmatter `name + description`，description 用「Use when…」让 Claude Code 能自动触发。body 自描述，不假设读者读过本设计。

### 5.1 `geo-goal-orchestrator/SKILL.md`

```markdown
---
name: geo-goal-orchestrator
description: Use when /goal command is invoked in geo-collab repo. Drives the
  netto-verified article generation loop with Ralph-style fresh-context writer
  + Haiku verifier subagents. Owns natural-language goal parsing, candidate
  question selection, retry/budget ceiling, and Feishu reporting.
---

# Role

你是 /goal 命令的 orchestrator。在**主对话**里执行；写作 + 评分通过 Agent
工具下发到 fresh-context subagent。你**不写文章、不评分**。

# Required Checklist (per /goal invocation)

1. Sanity check — 调 list_question_pools()；失败立即退出 + 指 docs/mcp-setup-notes.md
2. 解析目标 — 从用户自由文本抽取 {N, pool_id, topic_hint, matrix_code, model_label}
3. 抓 candidates + templates — list_question_items + list_prompt_templates
4. 进入主循环（伪码见 docs/superpowers/specs/2026-06-24-goal-loop-engineering-design.md §3.2）
5. 退出前飞书播报 (notify_feishu)

# Goal Parsing 规则

- N: 文中数字（"5 篇" / "8 篇"），默认 5
- pool_id: 用户提到池名 → 匹配 list_question_pools 里的 name；否则取第一个 pending_count>0 的池
- topic_hint: 题材关键词（"国风"、"治愈"…），用于过滤 candidates 的 question_text
- matrix_code: 用户带 `matrix=<code>` 才设；默认空 → 用 geo-article-writer
- model_label: 固定 "claude-goal-opus-4-7"（写入 metrics 用于 netto 查询）

# 主循环（每轮）

[详细伪码见 docs/superpowers/specs/2026-06-24-goal-loop-engineering-design.md §3.2]

# 进度日志（必须 echo 这些行到主对话）

[orchestrator] sanity ✓ pool=<name> N=<N> matrix=<code|default>
[round k/3N] qid=<id> → writer …
[round k/3N] writer 交稿 article_id=<id>, verifier …
[round k/3N] verifier decision=<d> score=<f/r/s/p>
[netto] today approved by goal-verifier: <count>/<N>
[done|abort] 净产出 <count>/<N>, 共耗时 <m>m, 原因=<...>

# 调用 subagent 的格式

writer:
  subagent_type: general-purpose
  description: "写一篇文章 qid=<id>"
  prompt: |
    Read .claude/skills/geo-article-writer<matrix_suffix>/SKILL.md
    and follow it strictly.
    Input: qid=<>, tpl_id=<>, model_label=<>
    Output: only {"article_id": int, "title": str} as final message.

verifier:
  subagent_type: general-purpose
  model: haiku
  description: "评分 article_id=<id>"
  prompt: |
    Read .claude/skills/geo-article-verifier/SKILL.md and follow it strictly.
    Input: article_id=<>, qid=<>, tpl_id=<>
    Output: only {"decision": str, "score_total": int} as final message.

# Stop / Budget Rules

- netto.count >= N → SUCCESS (level=done)
- attempts >= 3N → ABORT (level=warning)
- candidates 用尽 → ABORT (level=warning)
- 估算主对话 token > 80k → ABORT (level=warning)
- 连续 MCP 错误 >= 3 → ABORT (level=error)
```

### 5.2 `geo-article-writer/SKILL.md`

```markdown
---
name: geo-article-writer
description: Use when spawned as a writer subagent by /goal, or when manually
  composing one GEO article. Reads a question + template from MCP, writes
  markdown, calls save_article + (best-effort) illustrate_article, returns
  article_id.
---

# Role

你**只写一篇**文章并入库。不要循环、不要评分、不要碰其他 article。

# Required Checklist

1. get question — list_question_items(pool_id=<from input>) 拿到 qid 对应条目
2. get template — list_prompt_templates(scope="generation") 找到 tpl_id 的 content
3. 写 markdown body（约束见下）
4. save_article(question_item_id, prompt_template_id, title, markdown_content, model_label)
5. illustrate_article(article_id) — best-effort，失败吞掉
6. 返回 {"article_id": int, "title": str}

# title vs markdown_content 约束（重要）

- title 单字段，<= 300 字符，**不要**在 markdown_content 顶部再写 `# 标题`
- markdown_content 从正文第一段开始；用 ## / ### 做次级标题；列表 / 加粗按需

# 通用写作约束

- 内容紧扣 question_text
- 参考 template content 的语气 / 结构指引（template 是给你看的指令，不是给读者看的）
- 不胡编事实；不可验证的数字 / 引述删除或改写
- 不触发平台合规风险（政治 / 医疗 / 灰产宣传等）

## 矩阵特例：餐厅养成记官方矩阵（默认）

- 风格：轻松实用，避免「开篇一段宏大引入」，直接进主题
- 偏好题材：游戏推荐 / 攻略 / 玩法解析 / 国风游戏综述
- 配图类别：温馨治愈、国风山水（具体 stock_category_id 让 illustrate_article 自动按文章 tag 选）

## 加新矩阵的方法

复制本目录为 `geo-article-writer-<matrix-code>/`，**只改本节内容**，
其他段落不动。然后 `/goal matrix=<matrix-code> ...` 调用。

# 失败处理

- save_article 失败 → 输出 {"error": "<message>"} 退出；orchestrator 会跳过这条 qid
- list_question_items / list_prompt_templates 失败 → 同上

# 返回格式（最后一条消息只输出 JSON）

{"article_id": 824, "title": "..."}
```

### 5.3 `geo-article-verifier/SKILL.md`

```markdown
---
name: geo-article-verifier
description: Use when spawned as a verifier subagent by /goal to score a
  freshly written article. Reads article + original question + template,
  scores 4 dimensions independently, writes decision via
  submit_review_decision (does NOT change article.review_status).
---

# Role

你是**独立的**评分员。不是写文章那个 agent。你的工作就是按 4 个维度打分 +
出 decision + 调 submit_review_decision。

# Required Checklist

1. get_article(article_id) — 拿完整内容 + qid + tpl_id
2. list_question_items(pool_id) 反查 qid 对应 question_text
3. list_prompt_templates(scope="generation") 反查 tpl_id 对应 template
4. 按 4 维度评分（0-100）
5. 计算 score_total = (factuality + readability + style + policy_safety) / 4
6. 决策（门槛见下）
7. submit_review_decision(article_id, decision, score_total, score_breakdown,
   reasoning, decided_by="claude-goal-verifier")
8. 返回 {"decision": str, "score_total": int}

# 评分维度

| 维度 | 0-100 分什么 |
|---|---|
| factuality | 事实正确性、有无明显胡编、数字 / 时间 / 引述是否站得住 |
| readability | 段落结构、连贯性、易读程度、标题层级合理性 |
| style | 与 template 指引的语气贴合度（矩阵风格） |
| policy_safety | 合规风险（政治 / 医疗 / 灰产 / 违禁）—— **从严** |

# 决策门槛

- score_total >= 70 **且** policy_safety >= 80 → "approved"
- score_total >= 40 → "needs_rewrite"
- 否则 → "rejected"

policy_safety < 80 一律不能 approved，即使总分高（人审兜底但减负）。

# 反例（什么不该 approve）

- 开篇 "在这个 XX 的时代…" 这种空洞引入 → readability 扣分
- 出现"据某权威机构 99% 用户…" 但没有源 → factuality 扣分
- 涉及医疗效果断言 / 投资收益承诺 → policy_safety 直接拉到 < 60

# 重要约束

- **绝不调** set_review_status —— 不直接动 article.review_status（保留人审兜底）
- submit_review_decision 的 decided_by 字段必须 = "claude-goal-verifier"
  （净产出验证依赖这个串筛）

# 返回格式（最后一条消息只输出 JSON）

{"decision": "approved", "score_total": 82}
```

### 5.4 三个 skill 的共同设计原则

- 每个 skill 第一段「Role」就告诉新人：你只做这一件事，别的事不要碰。
- Required Checklist 在顶部，让 subagent 看完前 30 行就知道要干什么。
- 矩阵 / 模型 / 评分维度等**可能要 fork** 的内容放专门的小节，方便复用同事按节复制改造。
- 返回格式硬约束到最后一条消息只能是 JSON——主对话靠 stdout 解析，不能让 subagent 啰嗦。

---

## 6. 新增 MCP 工具 `list_today_loop_articles`

### 6.1 工具定位

- **唯一职责**：让 orchestrator 跟 GEO 数据库要 ground truth，回答"今天 verifier 通过的 loop 文章有几篇"。
- **不要扩范围**：不返回正文、不返回 metrics 全字段。回包小、可频繁查（orchestrator 每轮调 1 次，一天可能调 15+ 次）。
- **分组归属**：catalog 组（只读、低 side-effect），从 7 个工具变成 8 个。

### 6.2 MCP 工具签名

`server/mcp/tools/catalog.py` 末尾追加：

```python
@mcp.tool()
async def list_today_loop_articles(
    *,
    decided_by: str = "claude-goal-verifier",
    decision: str = "approved",
    since_hours: int = 24,
    model_label: str | None = None,
    limit: int = 50,
) -> dict:
    """Count + list articles that the /goal loop wrote and verifier decided on,
    within a rolling time window.

    Used by the /goal orchestrator as the source-of-truth stop condition,
    independent of the writer subagent's self-report.

    Args:
        decided_by: AutoReviewDecision.decided_by filter. Default
            "claude-goal-verifier" matches the verifier skill convention.
        decision: AutoReviewDecision.decision filter. Default "approved".
        since_hours: Window length, default 24h. Cap=168 (1 week).
        model_label: Optional. If supplied, also filter
            Article.metrics.writer_model == model_label. None = no filter.
        limit: Max items in `items`. Default 50, cap 200.

    Returns:
        {"ok": True,
         "data": {
             "count": int,              # total in window (not capped by limit)
             "items": [{                # capped by limit, newest first
                 "article_id": int,
                 "title": str,
                 "decided_at": str,     # ISO 8601 UTC
                 "score_total": int | None,
             }, ...],
         },
         "error": None}
    """
    return await _fetch_catalog(
        "GET",
        "/api/articles/today-loop-decisions",
        params={
            "decided_by": decided_by,
            "decision": decision,
            "since_hours": min(since_hours, 168),
            "model_label": model_label,
            "limit": min(limit, 200),
        },
    )
```

### 6.3 后端实现

**service 层** — `server/app/modules/auto_review/service.py` 新增：

```python
def list_recent_decisions(
    db: Session,
    *,
    decided_by: str,
    decision: str,
    since_hours: int,
    model_label: str | None = None,
    limit: int = 50,
) -> tuple[int, list[dict]]:
    """Return (total_count, items[:limit]) for AutoReviewDecision rows in window.

    items are dicts {article_id, title, decided_at, score_total}, newest first.
    total_count is the full count (not limited).
    """
    since = datetime.utcnow() - timedelta(hours=since_hours)

    q = (
        db.query(AutoReviewDecision, Article)
        .join(Article, Article.id == AutoReviewDecision.article_id)
        .filter(
            AutoReviewDecision.decided_by == decided_by,
            AutoReviewDecision.decision == decision,
            AutoReviewDecision.created_at >= since,
        )
    )
    if model_label:
        q = q.filter(
            func.json_unquote(func.json_extract(Article.metrics, "$.writer_model"))
            == model_label
        )

    total = q.count()
    rows = q.order_by(AutoReviewDecision.created_at.desc()).limit(limit).all()
    items = [
        {
            "article_id": a.id,
            "title": a.title,
            "decided_at": d.created_at.isoformat() + "Z",
            "score_total": d.score_total,
        }
        for d, a in rows
    ]
    return total, items
```

**router 层** — `server/app/modules/auto_review/router.py`（或现有 mcp-facing sub-router）：

```python
@auto_review_router.get(
    "/today-loop-decisions",
    dependencies=[Depends(require_mcp_token)],
)
def get_today_loop_decisions(
    decided_by: str = "claude-goal-verifier",
    decision: str = "approved",
    since_hours: int = Query(24, ge=1, le=168),
    model_label: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    try:
        count, items = service.list_recent_decisions(
            db,
            decided_by=decided_by,
            decision=decision,
            since_hours=since_hours,
            model_label=model_label,
            limit=limit,
        )
        return {"ok": True, "data": {"count": count, "items": items}, "error": None}
    except Exception as exc:
        raise mcp_exception_response(exc, context="list_today_loop_articles")
```

### 6.4 命名 / 路径决策

| 维度 | 选择 | 原因 |
|---|---|---|
| MCP tool 文件 | `server/mcp/tools/catalog.py` | 工具是只读 catalog 风格；高频调用 |
| 后端路由前缀 | `/api/articles/today-loop-decisions` | 沿用 `auto_review` 现有 router 在 `main.py` 的 `/api/articles` 挂载前缀（与 `POST /api/articles/score`、`POST /api/articles/{id}/auto-review` 同根） |
| 鉴权 | `Depends(require_mcp_token)` | 与现有 auto_review MCP 端点一致 |
| 工具命名 | `list_today_loop_articles` | "list" 前缀和 catalog 组一致；"loop" 词标识用途 |

---

## 7. 失败矩阵 + 不变式

### 7.1 失败矩阵

| 层 | 故障 | orchestrator 反应 | 数据后果 | netto 影响 |
|---|---|---|---|---|
| **MCP** | 401（token 错） | 立即退出 + 提示 docs/mcp-setup-notes.md | 无 | — |
| **MCP** | 502（LiteLLM 上游错） | 本轮跳过，`consecutive_mcp_fail++` | 无 | — |
| **MCP** | 5xx / 网络超时 | 同 502 | 无 | — |
| **MCP** | `consecutive_mcp_fail >= 3` | 退出 ABORT + 飞书 error | 无 | — |
| **Writer** | `save_article` 失败（如 415 / 标题超长） | `attempts++`，**qid 加入 used_qids** 不再重试 | 无（事务回滚） | 0 |
| **Writer** | Agent crash / 不返回 JSON | 同上 + 累加 mcp_fail（若是 mcp 类错误） | 无 | 0 |
| **Writer** | `illustrate_article` 失败 | writer 内吞，不上抛 | 文章落库，无配图 | 取决于 verifier |
| **Verifier** | `get_article` 失败 / `submit_review_decision` 失败 | 本轮跳过 verifier 步骤 | **文章保持 pending（人审兜底）** | 0 |
| **Verifier** | Agent crash / 不返回 JSON | 同上 | 文章保持 pending | 0 |
| **Verifier** | 给 needs_rewrite / rejected | **不是失败**，是预期路径 | 文章落库，decision 已写 | 0 |
| **Orchestrator** | 解析 N 失败 | 退出 + 反问「请明确写几篇」 | 无 | — |
| **Orchestrator** | 主对话 token > 80k | 主动 ABORT + 飞书 warning | 无 | — |
| **Orchestrator** | 用户 Ctrl-C | 主对话 catch → echo `[interrupted] 已落库 X 篇, 净产出 Y/N, 下次跑会接力` | 已落库不丢 | 累加到下次 |
| **Orchestrator** | `notify_feishu` 失败 | 吞掉 + 主对话 echo warning | 无 | — |

### 7.2 三个不变式（写进 orchestrator skill 作为硬约束）

1. **单点失败不杀整个 loop** —— 除非 MCP 连续 3 次失败。
2. **落库失败 ≠ 验证失败**：save_article 失败 → 这条 qid 不再重试；verifier 失败 → 文章留 pending 由人审兜底。两者绝不混淆。
3. **netto 是唯一计数事实**：任何"我感觉写好了"都不算数，必须查 GEO 拿确认。

---

## 8. 测试策略

### 8.1 自动测（CI 跑）

| 测试 | 文件 | 用例数 |
|---|---|---|
| `list_recent_decisions` 基础查询 | `server/tests/test_auto_review_loop_query.py` | 1 |
| 时间窗边界（24h / 168h cap） | 同上 | 1 |
| `decided_by` / `decision` 过滤 | 同上 | 1 |
| `model_label` JSON 过滤 | 同上 | 1 |
| `limit` 截断但 `count` 不截 | 同上 | 1 |
| MCP token 鉴权（无 token → 401） | 同上 | 1 |
| 端点回包正常 + since_hours 过滤 | 同上 | 2 |

合计 **8 个用例**。

> **不再做 SKILL.md lint 测试**：skill 文件已改成本地不入库的分发模式
> （见顶部「分发模型」段），`.claude/` 不在 CI checkout 范围内，写 lint
> 测试 CI 必失败。skill 文件良构由本地用户跑 `/goal` 自检（lint 失败时
> Claude Code 自己会报"skill 装载失败"）。

### 8.2 手工冒烟（每次发版前）

| 步骤 | 期望 |
|---|---|
| 1. 在 Claude Code 输入 `/goal 帮我产出 1 篇国风游戏文章作为冒烟` | orchestrator echo sanity ✓ |
| 2. 主对话出现 `[round 1/3] qid=... → writer ...` | writer subagent 启动 |
| 3. 主对话出现 `[round 1/3] writer 交稿 article_id=...` | save_article 成功 |
| 4. 主对话出现 `[round 1/3] verifier decision=... score=...` | verifier subagent 启动并完成 |
| 5. 主对话出现 `[netto] today approved by goal-verifier: 1/1` | MCP 查询正确 |
| 6. 主对话出现 `[done] 净产出 1/1, 共耗时 X m` | 退出路径正确 |
| 7. 飞书群里有播报 | webhook 链路通 |
| 8. GEO web UI 文章列表能看到这条 article | 数据库可见 |
| 9. 该 article 的 review_status="pending"（**没被自动 approved**） | 人审兜底纪律保住 |
| 10. AutoReviewDecision 表里有对应行，`decided_by="claude-goal-verifier"` | 净产出验证依赖项保住 |

### 8.3 有意不测

| 不测的事 | 为什么 |
|---|---|
| writer 的 markdown 质量 | LLM 输出不稳定；让 verifier 当门 + 人审兜底就够了 |
| verifier 的评分准确性 | 同上；硬约束只有「不能调 set_review_status」 |
| orchestrator 自然语言解析正确性 | LLM 推理；用 fixture 测无意义。靠 §8.2 冒烟覆盖 |
| Agent 工具 spawn subagent 的耗时 | 平台行为，不归我们 |

---

## 9. 工作量估算 + 实施顺序

### 9.1 工作量

**入库部分**（本 PR）：

| 模块 | 改动行 | 工时 |
|---|---|---|
| `server/app/modules/auto_review/service.py` | +30 行 | 0.5 h |
| `server/app/modules/auto_review/router.py` | +25 行 | 0.5 h |
| `server/mcp/tools/catalog.py` | +35 行 | 0.5 h |
| `server/tests/test_auto_review_loop_query.py` | +300 行 | 1.5 h |
| 本设计稿 + 实施 plan 撰写 | +2400 行 | 3 h |
| **小计** | **~2800 行** | **~6 h** |

**本地一次性部分**（每位使用者自己抄一份）：

| 模块 | 改动行 | 工时 |
|---|---|---|
| `.claude/README.md` | +60 行 | 0.1 h（复制） |
| `.claude/commands/goal.md` | +30 行 | 0.1 h |
| `.claude/skills/geo-goal-orchestrator/SKILL.md` | +160 行 | 0.1 h |
| `.claude/skills/geo-article-writer/SKILL.md` | +75 行 | 0.1 h |
| `.claude/skills/geo-article-verifier/SKILL.md` | +60 行 | 0.1 h |
| 手工冒烟 + 调通 | — | 1 h |
| **小计** | **~385 行** | **~1.5 h** |

**合计**：~3200 行，~7.5 h（约 1 天）。

### 9.2 实施顺序（依赖关系）

```
1. 后端 + MCP 工具（独立）           ── 可先做、不阻塞 skill 写作
   ├ service.list_recent_decisions
   ├ router GET /today-loop-decisions
   ├ catalog.py list_today_loop_articles
   └ test_auto_review_loop_query.py

2. Skill 文件（依赖：MCP 工具签名稳定）
   ├ geo-article-writer/SKILL.md          ── 可单独测：Skill <name> 手动写 1 篇
   ├ geo-article-verifier/SKILL.md        ── 可单独测：手动喂 article_id 让它评分
   └ geo-goal-orchestrator/SKILL.md       ── 依赖前两者

3. Slash command + 入口（依赖：3 个 skill 就绪）
   ├ .claude/commands/goal.md
   └ .claude/README.md

4. 冒烟（依赖：全部就绪）
   └ §8.2 10 步
```

每步完成后 git commit；冒烟通过后 PR。

---

## 10. 与已有 spec / 实现的关系

| 参考 | 关系 |
|---|---|
| `2026-06-18-claude-code-loop-with-geo-mcp-design.md` | **基础**——本设计沿用其 MCP 架构 / 鉴权 / 17 tools / 飞书 webhook，**不动** |
| `2026-06-22-claude-code-loop-architecture-visual-design.md` | **可视化分发版**——本设计落地后，可在那张图右下角加「`/goal` 模块」展开，但不强制 |
| `claude-loops/generation-loop.md` | **被升级**——保留不删，作为「不走 /goal、直接 /loop」的旧路径；新同事建议走 /goal |
| `claude-loops/distribute-loop.md` | **不相关**——本设计只管生文；分发 Loop 继续走它，未来若要类似升级再单独立项 |
| `claude-loops/weekly-report-loop.md` | **不相关**——同上 |

---

## 11. Smoke Test 与上线门禁

上线（合并到 main）门禁：
- §8.1 全部 9 用例 + lint 通过
- §8.2 10 步手工冒烟全通过
- 至少 1 个非作者同事按 §4.2 的 5 步流程独立跑通 `/goal`（验证 onboarding 文档可用）

满足以上 3 条合并；任一不满足 → 不合并。

---

## 12. Out of Scope（明确不做的）

- **分发 / 周报 Loop 升级**：本设计只动生文。
- **多模板自动 A/B**：当前模板是 round-robin 轮询；A/B 评估留给 `performance/` 模块未来扩展。
- **resume 机制**：netto 验证天然支持接力，不再单独做。
- **跨矩阵并行**：单次 `/goal` 只跑一个矩阵；要并行可分两个终端各起一次（写库无冲突，netto 查询会汇总）。
- **飞书 → /goal 反向触发**：见 06-22 设计的 v3，本设计不涉及。
