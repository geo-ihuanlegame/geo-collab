# `/goal` Loop 主对话叙述深度中文化 · 设计

- 状态：设计稿（v0），待 review 后进入 writing-plans 阶段
- 日期：2026-06-25
- 上游：[`2026-06-25-loop-illustration-and-i18n-fix-design.md`](./2026-06-25-loop-illustration-and-i18n-fix-design.md)（PR #149 已合，6 行进度日志首次部分中文化）+ [`2026-06-25-loop-skill-category-discovery-design.md`](./2026-06-25-loop-skill-category-discovery-design.md)（PR #152 已合）
- 受众：实施 plan 评审 + Loop 使用者
- 不动的部分：/goal 主架构 / 配图链路 / MCP 工具签名 / 后端代码
- 动的部分：orchestrator skill 的用户可见文本（echo 日志 + 主对话叙述 + subagent description + 飞书消息）深度去英文化 + 去内部术语化

---

## 0. 一句话

PR #149 留下的进度日志仍混着 `pool / qid / N / matrix / netto / goal / orchestrator`
等英文术语；用户报告"启动 orchestrator。N=5 国风。先看 netto"这种叙述完全看
不懂。本 PR 把所有**用户可见**的字眼（echo 6 行 + Claude 主对话自由叙述 +
subagent description + 飞书播报）改为无术语中文，并在 orchestrator skill 加
一段「叙述规范」硬约束 Claude 不准回流英文。不动伪码变量、MCP 工具参数、
Claude Code UI 自动加的标签等"非用户可控/非用户关心"层。

---

## 1. 问题定位

用户截图（2026-06-25 105841.png）实测 v3 跑 `/goal 今天 5 篇国风文章` 输出：

```
启动 orchestrator。N=5 国风。先看 netto + 已知国风候选 (qid=80/81/82/83, 4 条)。
[快检] pool=wenti01 N=5 matrix=默认 通过
[净产出] 今日通过 goal 评审的文章数: 4/5
差 1 篇。国风候选有 qid=80/81/82/83 共 4 条。进第 1 轮：
[第 1/15 轮] 选题 qid=80 (古风游戏推荐) → 改写中 …
Agent(写一篇文章 qid=80)
geo - save_article (MCP)(question_item_id: 80, prompt_template_id: 11, ...)
```

混了 4 类英文/术语，按"用户能不能理解 + 我们能不能改"分：

| 来源 | 例子 | 用户能理解？ | 我们能改？ |
|---|---|---|---|
| **A. echo 日志行**（orchestrator skill 进度日志段固定模板） | `[快检] pool=wenti01 N=5 matrix=默认` | ❌ | ✅ 完全可控 |
| **B. Claude 自由叙述**（读 skill body 后用自己的话说） | "启动 orchestrator。N=5 国风。先看 netto" | ❌ | ✅ 改 skill body 语气 + 加叙述规范段 |
| **C. subagent description**（Agent tool 的 description 参数） | `Agent(写一篇文章 qid=80)` | ❌ | ✅ orchestrator skill 模板字符串可控 |
| **D. Claude Code UI 自动加** | `Skill(geo-goal-orchestrator)` / `Agent(...)` / `Called geo` | ❌ | ❌ Claude Code 自己加 |
| **E. MCP 工具签名展示** | `save_article (MCP)(question_item_id: 80, prompt_template_id: 11)` | ❌ | ❌ 改了破后端契约 |

本 PR 解决 **A / B / C**；D / E 是 Out of Scope。

---

## 2. 锁定决策（brainstorming 已答）

| # | 决策 | 选项 |
|---|------|------|
| 1 | 翻译力度 | **只改 user 可见的 echo 日志行 + 主对话叙述**（推荐）—— 伪码变量 / MCP 工具调用 / Claude Code UI 不动 |
| 2 | 新分支 base | **从 origin/main** —— 独立 PR，跟 PR #152 / illustration_warnings 并行不干扰 |

---

## 3. 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│ 唯一战场：orchestrator skill 模板                              │
│ server/app/modules/loop_skills/templates/skills/             │
│   geo-goal-orchestrator/SKILL.md                             │
│                                                              │
│ 3 块改造：                                                    │
│ ① 进度日志格式段 6 行：术语 → 无术语中文                       │
│ ② 新增「主对话叙述规范」段：黑名单 + 对照表 + 反例 / 正例       │
│ ③ subagent description 字符串模板 + 飞书消息：术语 → 中文      │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
        ┌─────────────────────────────────┐
        │ bundle version v3→v4            │
        │ KNOWN_BUNDLE_SHAS 加 v4 LF/CRLF │
        │（v1/v2/v3 sha 全保留兼容旧版）   │
        └─────────────────────────────────┘
                     │
                     ▼
        ┌──────────────────────────────────────┐
        │ 自动测加 2 个 lint 用例：              │
        │ - 进度日志段不含黑名单术语              │
        │ - 叙述规范段存在 + 含黑名单关键词       │
        └──────────────────────────────────────┘
```

### 3.1 关键设计点

1. **只改 orchestrator skill** —— writer / verifier 不直接向用户叙述，不动；命令入口 / README 不动；后端 / MCP 工具不动
2. **加「叙述规范」段是关键** —— echo 6 行硬模板只覆盖固定输出；Claude 自由叙述（"启动 orchestrator..."）靠这段约束
3. **黑名单 + 对照表 + 反例 / 正例** 三件套 —— 比单纯"用中文"指令更可靠，Claude 看到具体反例不容易跑偏
4. **伪码变量名保留** —— `netto / used_qids / qid / tpl_id` 是给 Claude 看的代码逻辑，跟用户无关；强译反而易让 Claude 在代码思维和叙述思维之间切错
5. **MCP 工具签名不动** —— `save_article(question_item_id=...)` 是后端契约，改了破其它调用方
6. **必然 bump bundle v3→v4** —— skill 文本变了；KNOWN_BUNDLE_SHAS 同时认 LF + CRLF 两种 sha（吸取 PR #152 经验）

### 3.2 文件改动

```
入库：
  server/app/modules/loop_skills/templates/skills/geo-goal-orchestrator/SKILL.md  # 主战场
  server/app/modules/loop_skills/version.py                                        # v3→v4 + 2 v4 sha
  server/tests/test_loop_skill_bundle.py                                           # +2 lint tests
  docs/superpowers/specs/2026-06-25-loop-deep-i18n-design.md
  docs/superpowers/plans/2026-06-25-loop-deep-i18n.md

不动：
  writer / verifier SKILL.md（不直接叙述用户）
  README.md / commands/goal.md
  任何后端 / MCP 工具代码
  writer 矩阵段 <REPLACE_ME>（保持兼容 PR #152）
```

合计 ~80 行 skill 模板改 + ~5 行 version + ~35 行测试 = ~120 行，~2 小时工时。

---

## 4. 术语对照表 + 6 行日志映射

### 4.1 术语对照表

orchestrator skill 里所有出现在用户面前的字眼，统一替换：

| 当前 | 改为 | 备注 |
|---|---|---|
| `orchestrator` | **编排员** | 主对话叙述时自称的角色名 |
| `netto` | **累计通过数** | 用户最关心的指标，越直白越好 |
| `goal` / `goal-verifier` | **目标** / **评审员** | "goal" 在中文里不必保留品牌字 |
| `pool` / `pool_id` | **问题池** | 已经常说"问题池"了，统一 |
| `qid` | **问题 #N** 或 **问题编号** | 编号前缀 `#` 简短 + 自带语义 |
| `tpl` / `tpl_id` | **模板编号** / **模板 #N** | 同上 |
| `article_id` | **文章 #N** | 同上 |
| `matrix` / `matrix_code` | **矩阵** / **矩阵编号** | "矩阵"本身是中文，但 `matrix=默认` 直接说"矩阵：默认"自然些 |
| `N` (目标数量) | **目标 X 篇** | 数字带量词 |
| `verifier` / `decision` / `score` | **评审员** / **决定** / **分数** | 评分阶段术语 |
| `writer` | **改写员** | （沿用 PR #149 已有翻译） |
| `attempts` (尝试轮数) | **第 X / Y 轮**（X 当前轮，Y = 3N 上限） | 已有，保持 |
| `subagent` | **子助手** | 描述 Agent 工具调用时用 |

### 4.2 6 行进度日志映射

**当前**（PR #149 之后）：

```
[快检] pool=<name> N=<N> matrix=<code|默认> 通过
[第 k/3N 轮] 选题 qid=<id> → 改写中 …
[第 k/3N 轮] 改写完成 article_id=<id>, 评审中 …
[第 k/3N 轮] 评审 决策=<d> 总分=<total>
[净产出] 今日通过 goal 评审的文章数: <count>/<N>
[完成|中止] 净产出 <count>/<N>, 共耗时 <m>m, 原因=<...>
```

**改为**：

```
[启动检查] 问题池：<name>　目标：<N> 篇　矩阵：<code|默认>　✓
[第 k/3N 轮] 选题：问题 #<id> → 改写中 …
[第 k/3N 轮] 改写完成（文章 #<id>），评审中 …
[第 k/3N 轮] 评审结果：<d>　分数 <total>
[累计通过] 今日已过审文章数：<count>/<N> 篇
[完成|中止] 累计通过 <count>/<N>，共耗时 <m> 分钟，原因：<...>
```

**保留的格式约定**：
- 方括号 tag 留着方便用户 grep 状态
- `#<id>` 编号前缀（短 + 自带"这是个 id"语义）
- `k/3N` 轮次表达不动（已经直观）
- 全角空格 `　` 用作字段分隔（比 `pool=name N=5` 阅读上松散舒服）

### 4.3 subagent description 字符串映射

orchestrator 调 Agent tool 时用的 description 参数（Claude Code 在主对话显示 `Agent(...)` 时会用它）：

**当前**：

```python
writer_result = Agent(
    description=f"写一篇文章 qid={qid}",
    ...
)
verifier_result = Agent(
    description=f"评分 article_id={article_id}",
    ...
)
```

**改为**：

```python
writer_result = Agent(
    description=f"改写文章（问题 #{qid}）",
    ...
)
verifier_result = Agent(
    description=f"评审文章 #{article_id}",
    ...
)
```

### 4.4 飞书播报消息映射

当前飞书消息夹英文 / 术语：

| 当前 | 改为 |
|---|---|
| `"生文 Loop 完成"` | `"生文流程完成"` |
| `"生文 Loop 中止"` | `"生文流程中止"` |
| `f"净产出 {n}/{N}, 共耗时 {m}m"` | `f"累计通过 {n}/{N}，共耗时 {m} 分钟"` |
| `"attempts ceiling, 净产出 ..."` | `"已达尝试轮数上限，累计通过 ..."` |
| `"候选问题用尽, 净产出 ..."` | `"候选问题用完，累计通过 ..."` |
| `"token 预算触线, 净产出 ..."` | `"主对话内存预算触顶，累计通过 ..."` |
| `"MCP 连续失败 3 次, 请检查后端/token"` | `"接口连续失败 3 次，请检查服务连接 / 凭证"` |

---

## 5. 主对话叙述规范段（新增）

紧邻 orchestrator skill「进度日志」段插入：

```markdown
# 主对话叙述规范（强制）

你向用户叙述本次 /goal 运行时，**只能用中文 + 上面进度日志的固定格式**。
绝对不要在叙述里出现以下英文 / 内部术语（左侧错例，右侧用法）：

| ❌ 不要说 | ✅ 改成 |
|---|---|
| orchestrator | 编排员 / 我 |
| netto / 净产出 | 累计通过数 |
| goal / goal-verifier | 目标 / 评审员 |
| pool / pool_id | 问题池 |
| qid | 问题 #编号 |
| tpl_id | 模板 #编号 |
| article_id | 文章 #编号 |
| matrix / matrix_code | 矩阵 |
| N | 目标 X 篇 |
| writer / verifier | 改写员 / 评审员 |
| subagent | 子助手 |
| attempts | 已尝试轮数 |
| token 预算 | 主对话内存预算 |

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
```

---

## 6. bundle version bump

`server/app/modules/loop_skills/version.py`：

```python
LOOP_SKILL_BUNDLE_VERSION = "2026-06-25-v4"   # was "2026-06-25-v3"

KNOWN_BUNDLE_SHAS: frozenset[str] = frozenset(
    {
        # v1 (2026-06-24)
        "49f824a36606c285b84c71ade5aec406ea3c545599b7a3ccf59f863b521667dd",
        # v2 (2026-06-25)
        "abd8416c51f0b591c85cee0c3635645a10a313a2cedbeb52b89953a2c41e7fea",
        # v3 (2026-06-25, PR #152): writer 矩阵段 + README step 5 改为 list_stock_categories
        "58448672effda8290f97dc5afdfb6c4146ea9c8b7cc7c432b2d4a76274b65856",  # CRLF
        "515c9202f1e0880a1657e4da768954e83bb8a2a5cdfc220355a369862d2cefc6",  # LF (CI canonical)
        # v3 (2026-06-25, illustration_warnings PR): writer step 5 改 4 类信号 + illustration_warnings
        "ee9659ae08d68a6bdabecfce9f60324fab2093b7ad4535f056f5cdc4ea6a77e8",  # LF
        "c8050b24111efc69b56259194bed5d4b236b537ce7effc777a5e2786b3e44acc",  # CRLF
        # v4 (2026-06-25, this PR): orchestrator skill 进度日志 + 主对话叙述深度中文化
        "<v4 实施时跑 build_bundle 拿到的 LF sha>",
        "<v4 实施时跑 build_bundle 拿到的 CRLF sha>",
    }
)
```

v1 / v2 / v3 全保留 —— 已经装了旧版的同事本机校验仍要认。

---

## 7. 测试策略

### 7.1 自动测（CI 跑）

追加到既有 `server/tests/test_loop_skill_bundle.py`：

| # | 测试 | 验证什么 |
|---|---|---|
| 1 | `test_orchestrator_skill_progress_log_no_english_jargon` | 读 orchestrator skill 文件，断言进度日志段不含黑名单子串：`pool=` / `qid=` / `matrix=<` / `goal 评审` / `netto` / `attempts ceiling` / `candidates` / `token 预算` / `MCP 连续` |
| 2 | `test_orchestrator_skill_has_narration_rules_section` | 断言 skill 含 `# 主对话叙述规范（强制）` 字符串 + 黑名单关键词 `❌ 不要说` / `正例` |
| 3 | `test_bundle_sha_v4_is_known`（既有 sha 校验机制自动覆盖） | v4 sha 加进 KNOWN_BUNDLE_SHAS（不需新测试，复用既有 `test_bundle_sha_is_known`） |

**有意不测**：
- Claude 主对话叙述的实际内容（LLM 输出不稳定，让 §7.2 冒烟覆盖）
- 飞书消息渲染（需要真实 webhook）
- subagent description 实际显示效果（需要真实 Agent 调用）

### 7.2 手工冒烟

| # | 步骤 | 期望 |
|---|---|---|
| 1 | 装 v4 skill（重启 Claude Code） | bundle 版本 `2026-06-25-v4` |
| 2 | 跑 `/goal 今天 5 篇国风游戏文章` | 主对话叙述里**不出现** orchestrator / netto / qid / pool / N / matrix / goal-verifier 任何一个 |
| 3 | 6 行进度日志格式跟 §4.2 完全一致 | 字段名 / 编号前缀 / 中文标点都对得上 |
| 4 | `Agent(...)` 旁边显示的 description 是中文 | `Agent(改写文章（问题 #80）)` / `Agent(评审文章 #824)` |
| 5 | 飞书播报消息全中文 | 无 "Loop" / "netto" / "attempts ceiling" 等 |

---

## 8. 工作量估算 + 实施顺序

### 8.1 工作量

| 模块 | 行数 | 工时 |
|---|---|---|
| `templates/skills/geo-goal-orchestrator/SKILL.md`（6 行日志 + 叙述规范段 + subagent description + 飞书消息） | +60 / -15 | 1 h |
| `version.py`（bump + 2 个 v4 sha） | +3 | 0.1 h |
| 新加 2 个 lint 测试到 `test_loop_skill_bundle.py` | +35 | 0.4 h |
| 跑 lint/test + push + PR | — | 0.5 h |
| **合计** | **~120 行** | **~2 h** |

### 8.2 实施顺序

```
1. 改 orchestrator skill 6 行进度日志（§4.2 直接替换）
2. 改 orchestrator skill subagent description 字符串（§4.3）
3. 改 orchestrator skill 飞书消息（§4.4）
4. 加新增「主对话叙述规范」段（§5 整段插入）
5. 加 2 个 lint 测试到 test_loop_skill_bundle.py（TDD：先写测、跑红、改 skill 让测试过）
6. 跑 build_bundle 拿 v4 sha（CRLF + LF 两个），填进 KNOWN_BUNDLE_SHAS
7. bump version.py v3→v4
8. push + PR
```

---

## 9. 与上游 PR 关系

| 参考 | 关系 |
|---|---|
| PR #149 (`2026-06-25-loop-illustration-and-i18n-fix-design.md`) | 上游：首次部分中文化 6 行日志；本 PR 是它的深度迭代 |
| PR #152 (`2026-06-25-loop-skill-category-discovery-design.md`) | 独立：都改 orchestrator skill 同一文件，但本 PR 主战场是「日志 + 叙述」、#152 主战场是 writer 矩阵段 + README；先合谁后合谁都行；后合的 rebase 一下解决 KNOWN_BUNDLE_SHAS 集合合并即可 |
| `fix/illustration-silent-zero` PR | 独立：改 writer skill 不冲突；本 PR 不依赖 |

**合并策略**：3 个独立 PR；按 review 完成顺序合；后合的 rebase 只动 `version.py` 里的 KNOWN_BUNDLE_SHAS。

---

## 10. Out of Scope（明确不做的）

- **改 writer / verifier skill 文案** —— 他们不直接向用户叙述
- **重命名 MCP 工具参数**（`question_item_id` → `问题编号`）—— 会破后端契约
- **改 Claude Code UI 标签**（`Skill(...)` / `Agent(...)` / `Called geo`）—— 不归我们
- **改伪码内部变量名**（`netto / used_qids / qid` 在伪码里）—— 编程层概念，跟用户无关，强译反让 Claude 切码思维出错
- **build_bundle 行尾归一化**（LF/CRLF sha 一致化）—— 单独 spec / PR 处理；本 PR 走"两个 sha 都认"
- **重写 v1/v2 sha 集合**（v1/v2/v3 老 sha 全保留兼容旧装的同事本机）

---

## 11. 上线门禁

- 后端 ruff / format / pytest 全过
- 2 个新 lint 测试通过 + sha 校验通过
- 手工冒烟 5 步全过（**重点：第 2 步用户看不到任何 orchestrator/netto/qid 等术语**）
- 至少 1 个非作者同事跑 `/goal` 后口头反馈"主对话叙述读得懂、不夹生硬术语"
