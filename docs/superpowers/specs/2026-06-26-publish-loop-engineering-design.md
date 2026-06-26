## `/publish` Slash Command · 发文 Loop Engineering 设计

- 状态：设计稿（v0），待 review 后进入 writing-plans 阶段
- 日期：2026-06-26
- 上游参考：
  - [`2026-06-24-goal-loop-engineering-design.md`](./2026-06-24-goal-loop-engineering-design.md) —— 生文 Loop 工程化基础，8 段框架直接复用
  - [`2026-06-24-loop-skill-distribution-design.md`](./2026-06-24-loop-skill-distribution-design.md) —— 服务端模板正本 + `install_loop_skills` + 前端「MCP 接入」tab 分发模型
  - [`2026-06-25-headless-publish-lever-a-design.md`](./2026-06-25-headless-publish-lever-a-design.md) —— 必备依赖：`stop_before_publish=False` 默认 + record 失败不阻塞别的 record + 账号 expired 联动
  - [`2026-06-25-loop-deep-i18n-design.md`](./2026-06-25-loop-deep-i18n-design.md) —— 中文叙述强约束对照表
  - [`claude-loops/distribute-loop.md`](../../../claude-loops/distribute-loop.md) —— 被升级的 POC 原型，保留不删
- 受众：实施 plan 评审 + 团队复用规范对齐
- 不动的部分：MCP server 架构 / 鉴权 / 飞书 webhook / 分发机制（templates → install_loop_skills → 前端 tab）
- 动的部分：发文 Loop 从「POC 顺序拉号建任务，永远 stop_before_publish=True」升级到「`/publish <自然语言>` 一句话启动 → 主对话调度 + 启发式选号 → 单 article_round_robin task → poll 终态 → 飞书播报」

---

## 0. 一句话

把发文 Loop 从「`/loop claude-loops/distribute-loop.md` 拉一组 toutiao 账号建一个 stop_before_publish=True 任务等人确认」改造成「`/publish "今天发 5 篇头条"` → 主对话装载 orchestrator skill → 解析目标 → 从已审未分发库选 N 篇 + 启发式按账号 7 天 metrics 选号 → 建一个 article_round_robin 真发任务 → 每 30s 调 `get_publish_task_status` poll 终态（最多 30 min）→ 飞书播报 succeeded/N 与失效账号清单」。

与 `/goal` 工程化路径 1:1 对齐：自然语言入口、SKILL.md 自描述、分发模型走服务端 templates + 前端「MCP 接入」tab、中文叙述强约束、飞书播报、单点失败不杀 loop。

---

## 1. 锁定决策（brainstorming 已答）

| # | 决策 | 选项 |
|---|------|------|
| 1 | 命令入口 | **`/publish <自然语言>`** —— 对齐 `/goal` 一句话启动 |
| 2 | Ground truth | **`PublishRecord.succeeded` 计数** —— 单 task 自包含；不像 `/goal` 跨 run 累加 |
| 3 | 选号策略 | **orchestrator 主对话启发式** + `get_account_performance` 7 天 metrics 排序；**不**起 selector subagent |
| 4 | 多平台 | **MVP 只跑 toutiao**；参数留余地，wechat_mp / 跨平台后续单独立项 |
| 5 | `stop_before_publish` | **默认 `False`** —— lever-a headless 上线后允许直发；人审闸门已在 `review_status=approved` 那层做 |
| 6 | 任务粒度 | **单个 `article_round_robin` 大 task** —— 多账号轮转；一轮 `/publish` = 一个 task |
| 7 | 退出时机 | **Poll task 终态再退** —— 间隔 30s，超时 30 min 兜底 |

---

## 2. 架构总览

### 2.1 文件布局

**入库部分**（本 PR 提交）：

```
server/app/modules/articles/service.py                            # list_articles 加 exclude_distributed
server/app/modules/mcp_catalog/router.py                          # mcp_list_articles 透传新参数
server/app/modules/tasks/router.py                                # +GET /api/tasks/{id}/status-mcp（tasks_mcp_router）
server/mcp/tools/catalog.py                                       # +get_publish_task_status、扩 list_articles 签名
server/app/modules/loop_skills/
├── templates/
│   ├── commands/publish.md                                       # 新增
│   └── skills/geo-publish-orchestrator/SKILL.md                  # 新增
└── version.py                                                    # bump LOOP_SKILL_BUNDLE_VERSION + 新 sha

server/tests/
├── test_mcp_catalog_articles.py                                  # 新增（list_articles.exclude_distributed）
└── test_tasks_status_mcp.py                                      # 新增（get_publish_task_status）

docs/superpowers/specs/2026-06-26-publish-loop-engineering-design.md   # 本设计稿
docs/superpowers/plans/2026-06-26-publish-loop-engineering.md          # 实施 plan

claude-loops/distribute-loop.md                                   # 保留不删，作旧路径参考
```

**本地副本**（同事电脑，**不入仓库**）：

```
~/.claude/                                # .gitignore 屏蔽
├── commands/publish.md                   # 由 install_loop_skills 或前端「MCP 接入」tab 下发
└── skills/
    └── geo-publish-orchestrator/SKILL.md # 同上
```

**澄清**：服务端模板正本（`server/app/modules/loop_skills/templates/`）一直入库。同事电脑上的 `.claude/` 副本不入库 —— 改 skill 的方式是改 `templates/` + bump version + CI 校验 sha，同事在前端「MCP 接入」tab 看到新版本提示后点重装。这是已有的分发机制（`2026-06-24-loop-skill-distribution-design.md` 已就位），本设计直接复用。

### 2.2 与 `/goal` 的对齐 / 故意差异

**对齐**：

- 自然语言入口、命令 wrapper、SKILL.md 自描述、飞书播报、retry/budget ceiling、中文叙述强约束、可中断、分发模型
- `templates/` 入库正本 + `install_loop_skills` + 前端 tab 分发

**故意差异**：

| 维度 | `/goal` | `/publish` |
|---|---|---|
| Subagent | writer + verifier 两个 fresh-context subagent | **无 subagent**（选号 + 建任务 + poll 是规则性的，不需要 LLM 子上下文） |
| N 的语义 | 今日 verifier approved 的 loop 文章数（跨 run 累加） | 本次 task 里 succeeded 的 record 数（单 task 自包含） |
| Ground truth 查询 | `list_today_loop_articles`（查 AutoReviewDecision 24h 窗口） | `get_publish_task_status(task_id)`（查指定 task 的 records） |
| 退出门 | 净产出达到 N | task 进入终态（任务终态本身就是停止条件） |
| N 不可解析 | 默认 5 | **反问「请明确写几篇」**（发文有真实成本，不猜） |
| 接力 | 跨 run 自动累加 | 不接力（重新跑就是新 task） |

### 2.3 组件协作图

```
用户 ──/publish "今天发 5 篇头条"──▶ 主对话（publish orchestrator）
                                          │
                                          │ 1. 装载 geo-publish-orchestrator skill
                                          │ 2. sanity check（MCP 通不通）
                                          │ 3. 解析 → {N, platform_code, dry_run}
                                          │
                                          ├── 候选准备阶段 ──▶ list_articles(approved + exclude_distributed)
                                          │                ──▶ list_accounts(platform, distribution_enabled=true)
                                          │                ──▶ get_account_performance(每个候选账号, 7d)
                                          │                ──▶ 启发式：按 avg_views 排序选 ≤ N 个账号
                                          │                ──▶ 按 updated_at 取 N 篇 article
                                          │
                                          ├── 候选不足分支 ──▶ notify_feishu warning + 退出
                                          │
                                          ├── 创建任务 ──▶ create_distribute_task(article_ids, account_ids,
                                          │                                       stop_before_publish=False)
                                          │                ──▶ 返回 task_id
                                          │
                                          ├── Poll 阶段（每 30s）
                                          │   └── get_publish_task_status(task_id)
                                          │       ├ 非终态 → 主对话 echo 一行进度
                                          │       ├ 超时（30 分钟）→ 飞书 warning「任务未结束」+ 退出
                                          │       └ 终态 → 进入播报
                                          │
                                          └── 终态播报 ──▶ notify_feishu({succeeded}/{N} 篇成功，
                                                                        失败明细前 5 条 + expired 账号清单)
```

### 2.4 关键设计点

1. **单 task per `/publish`**：每次 `/publish` 只建 1 个 `article_round_robin` task。重新跑就是新建一个 task，**不接力** —— 已审未分发的剩余文章下一轮自然候选。
2. **Polling 间隔 30s + 超时 30 min**：与 lever-a headless 单篇发文 1-2 min 节奏相符；主对话 echo 每轮一行不刷屏。超时 ≠ 失败，飞书 warning + 退出，task 还在后台跑。
3. **失败账号 = 用人话报**：终态播报列出 `account.status` 变 `expired` 的账号（lever-a 已实现该联动），提示运营去登录页扫码重登。
4. **配置走「MCP 接入」tab 分发**：与 `/goal` 同 —— 改 skill 改 `server/app/modules/loop_skills/templates/`，bump version，同事看到新版本提示后点重装。

---

## 3. `/publish` Slash Command 行为

### 3.1 输入

```
/publish <自由文本>
```

例：
- `/publish 今天发 5 篇头条`
- `/publish 发 3 篇`（不带平台 → 默认 toutiao）
- `/publish 演练一下发 2 篇头条`（"演练" 关键词触发 dry_run）

### 3.2 主循环伪码

```pseudo
# 0. Sanity check
try:
    list_question_pools()           # 借用 /goal 的探活手段，不引入新工具
except McpError:
    退出 + 提示「请按 docs/mcp-setup-notes.md 配 ~/.claude.json 的 mcpServers.geo」

# 1. 解析自然语言目标
target = parse_goal(user_text)
# target = {
#   N: int                       # 必须明确，不可解析时反问；不像 /goal 默认 5
#   platform_code: str           # 默认 "toutiao"；识别"头条"/"微信公众号"
#   dry_run: bool                # 见到"演练"/"dry run"/"只选不发"等关键词置 True
# }

notify_feishu(title="发文 Loop 开始", message=f"目标 {target.N} 篇 / 平台 {target.platform_code}", level="info")

# 2. 候选准备
articles = list_articles(
    review_status="approved", status="ready",
    exclude_distributed=True, limit=target.N + 10,
).data.items

if len(articles) < target.N:
    notify_feishu("发文 Loop 中止",
        f"已审未分发候选不足：{len(articles)}/{target.N}，请先跑 /goal 补库", "warning")
    return ABORT

accounts = list_accounts(platform_code=target.platform_code, distribution_enabled=True).data
accounts = [a for a in accounts if a.status == "valid"]
if not accounts:
    notify_feishu("发文 Loop 中止",
        f"无可用 {target.platform_code} 账号（distribution_enabled + status=valid）", "error")
    return ABORT

# 3. 启发式选号（按 7 天 metrics 排序）
metrics = {a.id: (get_account_performance(a.id, 7).data or {}) for a in accounts}
accounts.sort(key=lambda a: metrics[a.id].get("avg_views") or 0, reverse=True)
selected_accounts = accounts[:min(target.N, len(accounts))]
selected_articles = articles[:target.N]

# 4. 建任务
r = create_distribute_task(
    name=f"发文 Loop · {today} · {target.N} 篇",
    article_ids=[a.id for a in selected_articles],
    account_ids=[a.id for a in selected_accounts],
    platform_code=target.platform_code,
    stop_before_publish=target.dry_run,        # dry_run=True 时停预览
)
if not r.ok:
    notify_feishu("发文 Loop 中止", f"建任务失败：{r.error}", "error")
    return ABORT
task_id = r.data.task_id
notify_feishu("发文任务已派",
    f"task #{task_id} · {target.N} 篇 → {len(selected_accounts)} 账号", "info")
echo(f"[任务已建] task #{task_id}，{target.N} 篇 → {len(selected_accounts)} 账号")

# 5. Poll
poll_started_at = now()
consecutive_mcp_fail = 0
while True:
    s = get_publish_task_status(task_id)
    if not s.ok:
        consecutive_mcp_fail += 1
        if consecutive_mcp_fail >= 3:
            notify_feishu("发文 Loop 中止", "MCP 连续失败 3 次", "error")
            return ABORT
        sleep(30); continue
    consecutive_mcp_fail = 0

    echo(f"[进度] task #{task_id} {s.data.status}"
         f" 成功 {s.data.totals.succeeded}/{target.N}"
         f" 在跑 {s.data.totals.running} 失败 {s.data.totals.failed}")

    if s.data.is_terminal:
        break
    if now() - poll_started_at > 30 * 60:
        notify_feishu("发文 Loop 部分完成",
            f"task #{task_id} 30 min 未达终态，当前 succeeded {s.data.totals.succeeded}/{target.N}，"
            f"请去分发引擎 tab 查后续", "warning")
        return PARTIAL
    sleep(30)

# 6. 终态播报
accounts_refresh = list_accounts(platform_code=target.platform_code, distribution_enabled=True).data
expired_now = {a.id: a for a in accounts_refresh if a.status == "expired"}
expired_in_run = [a for a in selected_accounts if a.id in expired_now]

succeeded = s.data.totals.succeeded
failed = s.data.totals.failed
level = "done" if (s.data.status == "succeeded" and succeeded == target.N) else "warning"
if succeeded == 0:
    level = "error"

msg = (
    f"task #{task_id} 终态：{s.data.status}\n"
    f"成功 {succeeded}/{target.N}、失败 {failed}\n"
)
if expired_in_run:
    msg += f"失效账号需重登录：{', '.join(a.username for a in expired_in_run)}\n"
if failed > 0:
    msg += "失败明细：\n" + "\n".join(
        f"  · article #{r.article_id} → account #{r.account_id}: {r.error_message[:80]}"
        for r in s.data.failed_records[:5]
    )
notify_feishu("发文 Loop 完成" if level == "done" else "发文 Loop 完成（部分失败）" if level == "warning" else "发文 Loop 失败",
              msg, level=level)
```

### 3.2.1 Helper 函数定义（消除伪码歧义）

| Helper | 定义 |
|---|---|
| `parse_goal(text)` | 主对话 LLM 解析；`N` 取文中数字（不可解析 → 反问退出，不默认值）；`platform_code` 见到"头条"→toutiao、"微信公众号"→wechat_mp，缺省 `"toutiao"`；`dry_run` 见到"演练"/"dry run"/"只选不发"等关键词置 True |
| `is_terminal` | 后端返回，等价于 `status in service.TERMINAL_TASK_STATUSES`（`succeeded`/`partial_failed`/`failed`/`cancelled`） |
| `now()` | 主对话本地时钟，用于 poll 超时计时（不依赖 MCP） |
| `sleep(30)` | 主对话里用 Bash tool 跑 `sleep 30`（PowerShell host 上等价 `Start-Sleep -Seconds 30`）。Claude Code 主对话本身没有原生 sleep，靠 Bash tool 阻塞 30s 即可，主对话流程在 sleep 命令返回后自然往下走 |

### 3.3 进度日志（强制中文短行，复用 `/goal` 叙述规范）

```
[启动检查] 平台：toutiao　目标：5 篇　✓
[候选] 已审未分发文章 12 篇，账号 6 个（valid 5、expired 1）
[选号] 按 7 天 avg_views 取前 5：account #3, #1, #7, #2, #5
[任务已建] task #142，5 篇 → 5 账号
[进度] task #142 running 成功 1/5 在跑 2 失败 0
[进度] task #142 running 成功 3/5 在跑 1 失败 0
[完成] task #142 succeeded 5/5，耗时 8 分钟，飞书已播报
```

### 3.4 主对话叙述规范（强制）

复用 `geo-goal-orchestrator/SKILL.md` 那份「英文术语 → 中文」对照表 + 反例 / 正例。新增本 loop 特有词条：

| ❌ 不要说 | ✅ 改成 |
|---|---|
| record / publish_record | 发布记录 |
| dry_run | 演练 / 只选不发 |
| platform_code | 平台 |
| article_round_robin | 多账号轮转分发 |
| stop_before_publish | 停在预览（仅演练时） |
| poll / polling | 轮询 / 等任务跑完 |
| expired | 失效（账号需重登录） |
| terminal | 终态 |

---

## 4. 同事使用 + 复用

### 4.1 三类使用者

| 角色 | 想做什么 | 接触面 | 不用关心 |
|---|---|---|---|
| **运营** (90%) | 跑 `/publish` 发 N 篇 | `/publish` 一句话 + 飞书看播报 | skill 内部 / 选号启发式 |
| **运营策略调优** | 改选号启发式（如换成 "上次发文时间倒序"） | `geo-publish-orchestrator/SKILL.md` 的「启发式选号」段 | MCP 签名 / 后端实现 |
| **平台扩展** | 加新平台支持 / 加新 stop 条件 / 加 MCP 工具 | orchestrator skill + 后端 `tasks/router.py` + `mcp/tools/catalog.py` | 启发式细节 |

### 4.2 冷启动 onboarding（在「MCP 接入」tab 已有引导基础上追加）

```
# 在 geo-collab 仓库里使用 /publish（4 步，前提是已配过 /goal）

1. 打开 GEO 前端「MCP 接入」tab，看到新 bundle 版本提示（含 publish.md + geo-publish-orchestrator）
2. 点「重新安装 skills」按钮（或在 Claude Code 里说"重装 geo loop skills"，让它调 install_loop_skills 拉新版本）
3. 重启 Claude Code
4. 在 Claude Code 里输入：
   /publish 今天发 5 篇头条

之后会自动跑（约 10-20 分钟）；完成后飞书群会有播报。
```

### 4.3 常见排障

| 症状 | 原因 | 处理 |
|---|---|---|
| `/publish` 启动后立刻退出，提示 "MCP 不可用" | `~/.claude.json` 没配 / token 错 | 参考 `docs/mcp-setup-notes.md` |
| 候选不足报警 | 已审未分发库为空 | 先跑 `/goal` 补库；或检查前端「已审核」库 |
| 候选有但选号阶段全 expired | 账号全失效 | 去前端账号列表逐个重登录（headed + 远程扫码） |
| poll 30 min 超时 | 单个 record 卡死 | 去前端「分发引擎」tab 看 task #X 哪个 record 卡了，必要时手动 cancel |
| 飞书没收到播报 | webhook 没配 / 配错环境 | 检查后端 `GEO_FEISHU_WEBHOOK_URL` |
| Ctrl-C 后 task 还在跑 | 设计如此 —— 不变式 #2 | 去前端「分发引擎」tab cancel；或让它自己跑完看飞书播报 |

### 4.4 反向约束：skill / command 必须满足的

复用 `2026-06-24-goal-loop-engineering-design.md` §4.6 那 4 条（self-contained / 零猜测 / 可观测 / 可中断），不重复展开。

---

## 5. 三个组件的内容大纲

### 5.1 `templates/commands/publish.md`

形式与 `goal.md` 1:1 对齐，~35 行：

```markdown
---
description: Geo 协作平台发文 Loop 入口。自然语言目标 → 自动选号建任务 → Poll 任务终态 → 飞书播报。
---

# /publish — Geo 发文 Loop

你刚被 `/publish $ARGUMENTS` 调用。把这条命令当作 `geo-publish-orchestrator`
skill 的入口包装：

1. **立刻** invoke the `geo-publish-orchestrator` skill（用 Skill tool）来装载完整 playbook。
2. 装载后，按 skill 里的 Required Checklist 执行，把 `$ARGUMENTS` 当作用户的自由文本目标传给「Goal Parsing 规则」段。
3. **不要**在装载 skill 之前先自己解析目标或调 MCP；skill 内部第一步就是 sanity check。

## 这条命令做什么 / 不做什么

**做**：
- 自然语言目标解析（"今天发 5 篇头条"）
- 从已审核未分发库选 N 篇文章
- 启发式按账号近 7 天表现选号
- 创建 article_round_robin 分发任务
- Poll 任务终态后飞书播报

**不做**：
- 不写文章 / 不评分（那是 /goal 的事）
- 不改 article.review_status（人审审过的才进候选）
- 不重试已失败的 record（lever-a 后失败=expired 账号，重登录路径独立）
```

### 5.2 `templates/skills/geo-publish-orchestrator/SKILL.md`

照搬 `geo-goal-orchestrator/SKILL.md` 的 8 段框架，内容换成发文语义。~180 行：

| 段 | 内容要点 |
|---|---|
| **YAML frontmatter** | `name: geo-publish-orchestrator` / `description: Use when /publish command is invoked. Drives the heuristic-selected publish task with poll-to-terminal stop condition.` |
| **Role** | 你是 `/publish` 的 orchestrator。在主对话里执行；不起 subagent。你只做：sanity → 解析目标 → 选号选文 → 建 task → poll → 飞书 |
| **Required Checklist** | 1. sanity（`list_question_pools()`）<br>2. 解析目标 → `{N, platform_code, dry_run}`<br>3. 拉候选 + 账号 + metrics<br>4. 启发式选号选文<br>5. `create_distribute_task`<br>6. Poll 到终态（间隔 30s、超时 30 min）<br>7. 终态飞书播报 |
| **Goal Parsing 规则** | `N` 抽数字（**不可解析时反问，不默认 5**）；`platform_code`（默认 `toutiao`）；`dry_run`（关键词触发） |
| **启发式选号** | 候选账号过滤：`status=valid` + `distribution_enabled=true`；按 `get_account_performance(7d).avg_views` 倒序；取前 `min(N, len(accounts))` 个；账号不够就少派号、不等账号 |
| **候选选文** | `list_articles(review_status="approved", status="ready", exclude_distributed=True, limit=N+10)`；`updated_at desc`；MVP 不按"题材匹配账号" |
| **主循环（poll 阶段）** | 伪码引用本设计稿 §3.2 |
| **进度日志** | §3.3 的 6 行 echo 模板（强制） |
| **主对话叙述规范** | 引用 §3.4 的对照表 + 反例 / 正例 |
| **Stop / Budget Rules** | 见 §4.2 三个不变式 |

### 5.3 `version.py` 改动

```python
LOOP_SKILL_BUNDLE_VERSION = "2026-06-26-v1"

KNOWN_BUNDLE_SHAS: frozenset[str] = frozenset({
    # ... 现有 sha 保留 ...
    # v5 (2026-06-26, this PR): +publish.md + geo-publish-orchestrator/SKILL.md
    "<新 CRLF sha 占位，CI 跑 build_bundle().bundle_sha256 取>",  # CRLF
    "<新 LF sha 占位，CI 跑 build_bundle().bundle_sha256 取>",   # LF (CI canonical)
})
```

实施时按 `version.py` 现有约定：先把模板内容敲定，跑 `python -c "from server.app.modules.loop_skills.service import build_bundle; print(build_bundle().bundle_sha256)"` 拿 sha 填进去（CRLF 在 Windows host、LF 在 CI / Linux 各拿一次）。

---

## 6. 新增 MCP 工具 `get_publish_task_status`

### 6.1 定位

- **唯一职责**：让 orchestrator 跟 GEO 数据库要 ground truth，回答"这个 task 跑到哪一步了，成功几篇 / 失败几篇 / 是否终态"。
- **不扩范围**：不返回 record 全字段、不返回截图、不返回 logs。回包小、高频调（每 30s 一次）。
- **分组归属**：catalog 组（只读、低 side-effect）。

### 6.2 MCP 工具签名

`server/mcp/tools/catalog.py` 末尾追加：

```python
@mcp.tool()
async def get_publish_task_status(task_id: int) -> dict[str, Any]:
    """Get a publish task's current status + per-record breakdown.

    Used by /publish orchestrator to poll until terminal state.

    Args:
        task_id: PublishTask id, returned by create_distribute_task.

    Returns:
        {"ok": True, "data": {
            "task_id": int,
            "status": str,                  # pending|running|succeeded|partial_failed|failed|cancelled
            "is_terminal": bool,            # status in {succeeded, partial_failed, failed, cancelled}
            "totals": {
                "pending": int,
                "running": int,
                "succeeded": int,
                "failed": int,
                "cancelled": int,
                "waiting_manual_publish": int,
                "waiting_user_input": int,
            },
            "succeeded_article_ids": list[int],
            "failed_records": [             # 截断到前 20 条，供飞书播报列明细
                {"record_id": int, "account_id": int, "article_id": int, "error_message": str},
                ...
            ],
            "started_at": str | None,        # ISO 8601 UTC
            "finished_at": str | None,
        }, "error": None}
    """
    return await _aget(f"/api/tasks/{task_id}/status-mcp")
```

### 6.3 后端实现

**Endpoint** —— `server/app/modules/tasks/router.py` 的 `tasks_mcp_router`：

```python
@tasks_mcp_router.get(
    "/{task_id}/status-mcp",
    dependencies=[Depends(require_mcp_token)],
)
def get_task_status_mcp(task_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """[MCP] Get a publish task's status snapshot for /publish orchestrator polling."""
    task = get_task(db, task_id)
    if task is None or task.is_deleted:
        raise HTTPException(status_code=404, detail=f"Task #{task_id} not found")

    records = list(
        db.execute(
            select(PublishRecord).where(
                PublishRecord.task_id == task_id,
                PublishRecord.is_deleted == False,  # noqa: E712
            )
        ).scalars().all()
    )
    totals = {
        "pending": 0, "running": 0, "succeeded": 0, "failed": 0,
        "cancelled": 0, "waiting_manual_publish": 0, "waiting_user_input": 0,
    }
    succeeded_article_ids: list[int] = []
    failed_records: list[dict] = []
    for r in records:
        totals[r.status] = totals.get(r.status, 0) + 1
        if r.status == "succeeded":
            succeeded_article_ids.append(r.article_id)
        elif r.status == "failed" and len(failed_records) < 20:
            failed_records.append({
                "record_id": r.id,
                "account_id": r.account_id,
                "article_id": r.article_id,
                "error_message": (r.error_message or "")[:500],
            })

    return {
        "ok": True,
        "data": {
            "task_id": task.id,
            "status": task.status,
            "is_terminal": task.status in TERMINAL_TASK_STATUSES,
            "totals": totals,
            "succeeded_article_ids": succeeded_article_ids,
            "failed_records": failed_records,
            "started_at": task.started_at.isoformat() + "Z" if task.started_at else None,
            "finished_at": task.finished_at.isoformat() + "Z" if task.finished_at else None,
        },
        "error": None,
    }
```

### 6.4 命名 / 路径决策

| 维度 | 选择 | 原因 |
|---|---|---|
| MCP tool 文件 | `server/mcp/tools/catalog.py` | 只读、高频；与 `list_today_loop_articles` 同处 |
| 后端路由前缀 | `/api/tasks/{task_id}/status-mcp` | 沿用 `tasks_mcp_router` 的 `/api/tasks/mcp` 同根 |
| 鉴权 | `Depends(require_mcp_token)` | 与现有 MCP 端点一致 |
| 工具命名 | `get_publish_task_status` | "get" 前缀表示单 task 查询；"publish_task" 与后端 model 名对齐 |

---

## 7. 扩 `list_articles` 加 `exclude_distributed` 参数

### 7.1 为什么需要

现 `/api/mcp/articles` 只能 `status` + `review_status` 过滤，没排除「已分发/在途」。客户端用返回的 `published_count == 0` 只能排除「已成功发布」，**无法排除「task 已派号但还在跑」** —— 会被两个 `/publish` 重复派出去。

### 7.2 改动

**service 层** —— `server/app/modules/articles/service.py:list_articles` 加参数：

```python
def list_articles(
    db: Session,
    query: str | None = None,
    skip: int = 0,
    limit: int = 50,
    user_id: int | None = None,
    review_status: str | None = None,
    exclude_distributed: bool = False,        # 新增
) -> list[Article]:
    # ... 现有逻辑 ...
    if exclude_distributed:
        # 与 pipeline approved_content_source 节点同口径（CLAUDE.md 明文）：
        #   「已分发或在途」= 存在 PublishRecord.status NOT IN [failed, cancelled] 且未软删
        #   failed/cancelled/软删的记录允许重新分发（不永久埋没）
        distributed = select(PublishRecord.article_id).where(
            PublishRecord.is_deleted == False,  # noqa: E712
            PublishRecord.status.notin_(["failed", "cancelled"]),
        )
        stmt = stmt.where(Article.id.notin_(distributed))
    # ... 现有 offset / limit ...
```

**MCP router 层** —— `server/app/modules/mcp_catalog/router.py:mcp_list_articles` 透传：

```python
@router.get("/articles", response_model=list[ArticleListRead])
def mcp_list_articles(
    status: str | None = Query(default=None),
    review_status: str | None = Query(default=None),
    exclude_distributed: bool = Query(default=False),    # 新增
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[ArticleListRead]:
    articles = svc_list_articles(
        db, ..., exclude_distributed=exclude_distributed,
    )
    # ...
```

**MCP tool 签名** —— `server/mcp/tools/catalog.py:list_articles` 加参数（默认 False，不影响现有调用方）。

### 7.3 与 pipeline 节点的口径对齐

| 来源 | 判定语句 |
|---|---|
| `approved_content_source.run_approved_content_source` | `PublishRecord.status.notin_(["failed", "cancelled"]) & is_deleted == False` |
| `article_group_source` | 同上（CLAUDE.md 明文：「共用此判定」） |
| **本设计** `list_articles(exclude_distributed=True)` | 同上 |

实施时直接 import 或抽公共函数；不再重复维护三处判定。

---

## 8. 失败矩阵 + 不变式

### 8.1 失败矩阵

| 层 | 故障 | orchestrator 反应 | 数据后果 |
|---|---|---|---|
| **MCP** | 401（token 错） | 立刻退出 + 提示 `docs/mcp-setup-notes.md` | 无 |
| **MCP** | 502/5xx/超时（单次） | 本轮跳过；`consecutive_mcp_fail++` | 无 |
| **MCP** | `consecutive_mcp_fail >= 3` | 退出 ABORT + 飞书 error | 无 |
| **候选** | `list_articles(exclude_distributed=True)` 返回 < N | notify_feishu warning「已审未分发候选不足 X/N」+ 退出 | 无 |
| **账号** | `list_accounts` 返回空 / 全部 `status != valid` | notify_feishu error「无可用 toutiao 账号」+ 退出 | 无 |
| **账号** | `get_account_performance` 个别失败 | 该账号 `avg_views=0` 排到末位，**不阻塞 loop** | 无 |
| **建任务** | `create_distribute_task` 422 | notify_feishu error + 退出；不重试（候选已过 `exclude_distributed` 还出 422 = 平台 bug） | 无 |
| **建任务** | 5xx/网络 | 同 MCP 502 路径，consecutive_mcp_fail++ | 无 |
| **Poll** | `get_publish_task_status` 单次失败 | consecutive_mcp_fail++；不 break 循环；本轮 sleep 30s 接着 poll | 无 |
| **Poll** | 30 min 仍未达终态 | notify_feishu warning + 退出 PARTIAL | task 后台继续跑，不杀 |
| **Poll** | task 状态 = `cancelled`（人在前端取消） | 当作终态进播报，level=warning | 无 |
| **任务执行（下层）** | 个别 record `failed`（lever-a 已实现：headless 失败不阻塞别的 record） | 终态播报里列前 5 条 `error_message` | account 可能被自动置 `expired` |
| **任务执行（下层）** | 账号被置 `expired`（lever-a 已实现） | 终态播报里附 expired 账号清单 | 账号 UI 标红；该账号其余 pending record 走校验秒失败（由 worker 处理） |
| **Orchestrator** | 自然语言 N 解析不出 | 反问「请明确写几篇」，**不默认 5** | 无 |
| **Orchestrator** | `notify_feishu` 失败 | 吞掉 + 主对话 echo warning；不杀 loop | 无 |
| **Orchestrator** | 用户 Ctrl-C | 主对话 echo `[已中断] task #X 仍在后台，请去分发引擎 tab 看进度` | task 不受影响 |

### 8.2 三个不变式（硬约束，写进 orchestrator skill）

1. **不建任务前的失败不杀 task**：任何检查/选号阶段失败，loop 退出就好，不留半建的 task 在数据库里。
2. **建任务后的失败不杀 task**：task 一旦建好（`create_distribute_task` 返回 task_id），无论 poll 是否完成、orchestrator 是否退出、用户是否 Ctrl-C，**task 自己在后台跑完**。loop 退出 ≠ task 退出。这是设计核心松耦合。
3. **Ground truth 是 PublishRecord.succeeded**：自然语言里的"5 篇发完了"必须来自 `get_publish_task_status.totals.succeeded`，**绝不**用 task.status 单独推断（succeeded task 也可能含个别 failed record）。

### 8.3 失败账号联动（与 lever-a 协作）

| 场景 | lever-a 已做的 | 本 loop 做的 |
|---|---|---|
| headless 发布撞 `UserInputRequired` | 标 record `failed`（`failure_kind=login_required`） + 置 `account.status='expired'` + 继续 task 其它 record | poll 完后在飞书里点名 expired 账号 |
| account 已是 `expired` 时建任务 | `create_task` 拒（`AccountError`） | orchestrator 选号阶段已过滤 `status=valid`，正常不会撞 |
| 同账号多 record 撞 expired | lever-a 让 worker 自动 fail，loop 不重试 | 终态播报里这些 record 进 `failed_records` |

**故意不做**：
- 不自动给 expired 账号触发重登录（headed + 远程扫码独立流程）
- 不重试 failed record（lever-a 后 failed 多半 = 账号失效）
- 不反向触发新一轮 `/publish` 补失败的

### 8.4 飞书消息模板

| 场景 | level | 标题 | body 模板 |
|---|---|---|---|
| 开始 | info | 发文 Loop 开始 | `目标 {N} 篇 / 平台 {platform_code}` |
| 候选不足 | warning | 发文 Loop 中止 | `已审未分发候选不足：{found}/{N}，请先跑 /goal 补库` |
| 无可用账号 | error | 发文 Loop 中止 | `无可用 {platform_code} 账号（distribution_enabled + status=valid）` |
| 建任务成功 | info | 发文任务已派 | `task #{task_id} · {N} 篇 → {M} 账号` |
| Poll 超时 | warning | 发文 Loop 部分完成 | `task #{task_id} 30 min 未达终态，当前 succeeded {a}/{N}，请去分发引擎 tab 查后续` |
| 全成功 | done | 发文 Loop 完成 | `task #{task_id} succeeded {N}/{N}，耗时 {m} 分钟` |
| 部分失败 | warning | 发文 Loop 完成（部分失败） | `task #{task_id}: succeeded {a}/{N}，failed {b}，失效账号：{expired_list}` |
| 全失败 | error | 发文 Loop 失败 | `task #{task_id} failed {N}/{N}，失效账号：{expired_list}` + 列前 5 条 `error_message` |
| MCP 连错 3 次 | error | 发文 Loop 中止 | `MCP 连续失败，请检查 GEO 后端 / token` |

---

## 9. 测试策略

### 9.1 自动测（CI 跑）

| 测试 | 文件 | 用例数 |
|---|---|---|
| `list_articles` MCP endpoint `exclude_distributed=True` 正确排除「已在 task 在跑」+「已 succeeded」 | `server/tests/test_mcp_catalog_articles.py` | 2 |
| `list_articles` `exclude_distributed=True` 保留 failed/cancelled/软删 record 对应的文章（允许重发） | 同上 | 1 |
| `list_articles` 默认 `exclude_distributed=False` 与现行行为字节一致（防回归） | 同上 | 1 |
| `get_publish_task_status` 返回 totals 按 status 分组正确 | `server/tests/test_tasks_status_mcp.py` | 1 |
| `is_terminal` 与 `service.TERMINAL_TASK_STATUSES` 完全一致（成员对等） | 同上 | 1 |
| `failed_records` 截断 ≤ 20 条 + 含 `error_message` | 同上 | 1 |
| `succeeded_article_ids` 与 `totals.succeeded` 长度一致 | 同上 | 1 |
| MCP token 鉴权（无 token → 401） | 同上 | 1 |
| Task 不存在 → 404 | 同上 | 1 |
| `loop_skills` bundle sha 校验（改 templates 必同步 bump） | 已有 `test_loop_skill_bundle.py::test_bundle_sha_is_known` | 自动覆盖 |

合计 **9 个新用例**，复用现有 `build_test_app` fixture 跑 MySQL。

### 9.2 有意不测

| 不测 | 为什么 |
|---|---|
| Orchestrator 自然语言解析 | LLM 推理，fixture 测无意义；§9.3 冒烟覆盖 |
| 启发式选号正确性 | LLM 主对话本地排序，本就允许「次优解」 |
| Poll 间隔 / 超时定时器 | 30s × 60 = 30 min 墙钟逻辑，单测桩成本远大于价值 |
| `create_distribute_task` 内部行为 | 已被 `test_tasks_router.py` / `test_mcp_router.py` 覆盖 |
| Lever-a 失败联动账号 expired | 已被 `test_runner_headless.py` 覆盖 |
| 飞书 webhook 链路 | 已被 `test_feishu_notify.py` 覆盖 |
| Bundle zip 解压完整性 | 已被 `test_loop_skill_bundle.py` 覆盖 |

### 9.3 手工冒烟（每次 bump LOOP_SKILL_BUNDLE_VERSION 前跑）

前置：本地有 ≥ 1 个 `status=valid` 的 toutiao 账号，已审未分发库里有 ≥ 1 篇文章。

| # | 步骤 | 期望 |
|---|---|---|
| 1 | 前端「MCP 接入」tab 看到新 bundle 版本，点重装 | `~/.claude/commands/publish.md` + `~/.claude/skills/geo-publish-orchestrator/SKILL.md` 落盘 |
| 2 | Claude Code 重启 → `/mcp` 看 `get_publish_task_status` 出现 | 工具注册成功 |
| 3 | `/publish 帮我发 1 篇头条作为冒烟` | 主对话 echo `[启动检查] ... ✓` |
| 4 | 主对话 echo `[候选] 已审未分发 X 篇，账号 Y 个` | list_articles + list_accounts 工作正常 |
| 5 | 主对话 echo `[任务已建] task #X，1 篇 → 1 账号` | create_distribute_task 成功 |
| 6 | 每 30s 主对话 echo `[进度] task #X running 成功 0/1 在跑 1` | get_publish_task_status 工作 |
| 7 | 终态 echo `[完成] task #X succeeded 1/1，耗时 X 分钟` | poll 终态识别正确 |
| 8 | 飞书群有「发文 Loop 完成」播报 | 通知链路通 |
| 9 | GEO 前端「分发引擎」tab 该 task 显示 succeeded | 数据库可见 + UI 一致 |
| 10 | 头条该账号实际发出文章 | 端到端真发可见 |
| 11 | （故障演练）把 1 个账号置 `status=expired` 后再跑 `/publish 1 篇` | 选号阶段过滤掉它；如全部账号 expired，飞书 error |
| 12 | （故障演练）`/publish 一下`（N 不可解析） | 反问「请明确写几篇」，**不**默认 5 |

---

## 10. 工作量 + 实施顺序

### 10.1 工作量

**入库部分**（本 PR）：

| 模块 | 改动行 | 工时 |
|---|---|---|
| `server/app/modules/articles/service.py:list_articles` 加 `exclude_distributed` | +15 行 | 0.3 h |
| `server/app/modules/mcp_catalog/router.py:mcp_list_articles` 透传新参数 | +5 行 | 0.2 h |
| `server/app/modules/tasks/router.py` 加 `GET /api/tasks/{id}/status-mcp` | +60 行 | 1 h |
| `server/mcp/tools/catalog.py` 加 `get_publish_task_status` + 扩 `list_articles` 签名 | +50 行 | 0.5 h |
| `server/app/modules/loop_skills/templates/commands/publish.md` | +35 行 | 0.3 h |
| `server/app/modules/loop_skills/templates/skills/geo-publish-orchestrator/SKILL.md` | +180 行 | 1.5 h |
| `server/app/modules/loop_skills/version.py` bump（version + 2 个新 sha） | +5 行 | 0.1 h |
| `test_mcp_catalog_articles.py`（4 用例） | +180 行 | 1 h |
| `test_tasks_status_mcp.py`（5 用例） | +200 行 | 1.2 h |
| 设计文档（本稿） | 已计算 | — |
| 实施 plan 撰写 | +1000 行 | 1.5 h |
| **小计** | **~1700 行** | **~7.6 h** |

**本地一次性部分**（同事走「MCP 接入」tab 重装）：~0.6 h（点重装 + Claude Code 重启 + 冒烟 1 篇）。

### 10.2 实施顺序（依赖关系）

```
1. 后端 + MCP 工具（独立可先）
   ├ list_articles + exclude_distributed
   ├ GET /api/tasks/{id}/status-mcp
   ├ catalog.py: get_publish_task_status + 扩 list_articles
   └ 9 个用例（test_mcp_catalog_articles.py + test_tasks_status_mcp.py）

2. 模板正本（依赖：MCP 工具签名稳定）
   ├ templates/commands/publish.md
   └ templates/skills/geo-publish-orchestrator/SKILL.md

3. Bundle 版本（依赖：模板正本最终）
   └ version.py bump LOOP_SKILL_BUNDLE_VERSION + KNOWN_BUNDLE_SHAS（CRLF + LF）

4. CI 验证（依赖：3 完成）
   └ test_loop_skill_bundle.py 自动校验新 sha

5. 冒烟（依赖：CI 通过 + 同事本地重装）
   └ §9.3 12 步
```

每步完成即可 commit；冒烟通过后开 PR。**关键依赖点**：步 3 必须在步 2 模板内容完全敲定后才做（避免 sha bump 完又改内容）。

### 10.3 上线门禁

- §9.1 全部 9 个新用例 + 现有 CI 全绿
- §9.3 12 步手工冒烟全过（含 2 个故障演练）
- 至少 1 个非作者同事通过「MCP 接入」tab 重装并独立跑通 `/publish 1 篇`

任一不满足 → 不合并。

---

## 11. Out of Scope（明确不做的）

| 不做的事 | 理由 |
|---|---|
| 多平台同 run 并发 | MVP 头条；wechat_mp 后续单独立项（仿 pipeline distribute 按平台分组分别建 task） |
| selector subagent | 用户已确认主对话启发式即可；账号上百再重评 |
| 失败 record 自动重试 | lever-a 后 failed 多半 = 账号失效；重试无意义，靠重登录路径独立处理 |
| 失败账号自动触发重登录 | 重登录走 headed + Xvfb + 远程扫码，不是 loop 职责；飞书提示运营 |
| 跨 run 的「今日发文 N 篇」累加 | `/publish` 是单 task 自包含，不像 `/goal` 跨 run 累加 |
| 题材→账号匹配二次过滤 | MVP 按 metrics 排序；A/B 评估留给 `performance/` 模块未来扩展 |
| `distribute-loop.md` 删除 | 保留作为「不走 /publish、直接 /loop」的旧路径参考 |
| dry_run 之外的 stop_before_publish 参数 | dry_run 用关键词触发 `stop_before_publish=True`，不再单独加参数 |
| 取消运行中 task 的 slash 命令 | Ctrl-C 后让 task 后台跑完是设计；要取消去 GEO 前端「分发引擎」tab |

---

## 12. 与已有 spec / 实现的关系

| 参考 | 关系 |
|---|---|
| `2026-06-18-claude-code-loop-with-geo-mcp-design.md` | 基础：MCP 架构 / 鉴权 / 飞书 webhook 不动 |
| `2026-06-24-goal-loop-engineering-design.md` | 工程化模板：`/publish` 完整对齐其 8 段框架 |
| `2026-06-24-loop-skill-distribution-design.md` | 分发模型：`templates/` 入库 + `install_loop_skills` + 前端 tab 流程复用 |
| `2026-06-25-headless-publish-lever-a-design.md` | 必备依赖：`stop_before_publish=False` + record 失败不阻塞别的发布 + 账号 expired 联动 |
| `2026-06-25-loop-deep-i18n-design.md` | 中文叙述强约束：`/publish` 复用 `/goal` 那份对照表 |
| `claude-loops/distribute-loop.md` | 被本设计升级；保留不删（旧路径参考） |
| `claude-loops/generation-loop.md` | 平行关系：旧路径参考；新同事建议走 `/goal` + `/publish` 组合 |
