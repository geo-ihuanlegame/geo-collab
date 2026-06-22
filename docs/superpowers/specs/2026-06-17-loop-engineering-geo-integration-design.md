# Loop Engineering 与 GEO 的结合：讨论稿

> 状态：讨论稿（v0）｜面向：老板 + 技术 leader 双重受众，先业务后技术｜日期：2026-06-17
>
> 目的：拍板下一步方向、团队据此立项的「Loop Engineering × GEO」讨论稿，不是定稿方案。

---

## 0. 一页摘要

- **Loop Engineering 是什么**：让 AI Agent 不再"一次性听指令、出一篇就停"，而是放进一个**自迭代闭环**里——它自己设目标、自己干、自己评估、不达标自己再来一次，达标了再交给下一个 Agent。Anthropic、Google、Meta 内部都已经把它当作 2026 年新的工程范式（"从 Prompt Engineering 升级到 Loop Engineering"）。
- **跟 GEO 的关系**：GEO 现有的「工作流编排（pipeline）+ 问题池 + 方案池 + 驱动注册表 + worker 状态机」其实已经有了 Loop 的骨架，**缺的是评估器、反馈回流、回路结构**这三块"让它转起来"的零件。
- **本稿提出的双轨方案**：
  - **方案 A（1-2 个月跑通，立刻能讲数字）**：在三段链路上各挂一段 Loop——**选题 Loop**（解决目前热门词靠人工录飞书的瓶颈）+ **评审 Loop**（解决过审率）+ **回流 Loop**（解决效果验证）。改动量小、收益看得见。
  - **方案 C（3-6 个月愿景）**：把 GEO 升级成 "Agent Town"——运营只设目标和预算，一组常驻 Agent 自己分工跑完"选题→生文→审核→分发→复盘"全链路。这是行业最前沿的故事。
- **要拍板的事**：要不要先用 **2 周 + 1 个工程师**做一个"选题 Loop"POC，把热门词从手工录飞书改成 Agent 自动跑 + 自评 + 写回？这是把 Loop Engineering 引入 GEO 的最小可信切入点。

---

## 1. Loop Engineering 是什么（3 分钟读懂）

### 1.1 它解决的真实问题

过去一年，大家给 AI Agent 写 prompt 都遇到同一个尴尬：
- **prompt 写得好不好，全靠人**——一个改五版、一个不改也能用，没标准。
- **Agent 干一次就停**——错了要人来发现、来纠正、再让它重来。
- **上下文越聊越脏**——同一个对话越走越偏，最后只能开新窗。

Loop Engineering 的回答是：**别再坐在 Agent 旁边给它每一步打指令了，给它一个外循环替你驾驶**。

### 1.2 一个 Loop 的五件套

行业共识（Addy Osmani / Boris Cherny / Peter Steinberger 在 2026/6 公开演讲里反复强调的）是：一个"真"Loop 必须凑齐五件套，缺一个就退化成"加了 cron 的 prompt"：

| 要素 | 通俗讲法 | 对应到 GEO |
|------|----------|------------|
| **明确的目标** | "今天要 50 篇过审" | pipeline 的 schedule + 任务规格 |
| **上下文管理** | 每轮重置上下文、用磁盘/数据库保留状态 | GEO 已经是 DB 持久化，天然友好 |
| **可调用工具** | LLM 能调的 API、MCP、写文件 | LiteLLM + 飞书 API + hot_lists 反代 + MCP |
| **对产出的评估** | 能"说不"的机制（测试 / 评分 / 人审） | **目前 GEO 几乎没有** ← 主要缺口 |
| **停止条件** | 达标了、超预算了、超时了 | pipeline 现在只有线性结束，无"达标"概念 |

### 1.3 它跟 Prompt Engineering、传统 Agent 的关系

- **不是替代 Prompt**——一个 Loop 里有多个 Prompt，prompt 烂照样产烂活、只是产得更快。
- **不是 cron 任务**——cron 是"按时跑一次"，Loop 是"按目标跑到达标为止"。
- **不是单 Agent**——Loop 可以是一个 Agent 自己反复跑（Ralph Loop），也可以是多个 Agent 协作跑（Agent Town / Gas Town / 明略 Octo 都在做这个）。

### 1.4 行业在做什么（"竞品视角"）

- **Anthropic 自己**：Claude Code 的 `/loop` `/goal` `/batch` 内置命令、ralph-wiggum 插件已经官方包装；过去一年他们的内部 Agent 从"连续运行 20 分钟容易出错"做到了"连续运行几天几乎不出错"。
- **Geoffrey Huntley**（提出 Ralph Loop 的人）：一个人靠 Loop 在三个月里用 297 美元 API 费用造了一门叫 CURSED 的编程语言。
- **Steve Yegge**（提出 Gas Town）：把"Kubernetes for agents"作为下一个抽象层——多 Agent 并发跑、按"分子粒度"切任务。
- **国内**：明略科技已经声称跑了近 3000 个 Agent 的多 Agent 协作平台 Octo；阿里、字节内部都在推自迭代写代码 Agent。
- **路口判断**：选题/写作/分发这种"高度结构化、有明确成败指标"的运营工作流，是 Loop Engineering 第二波重点落地的场景（第一波是写代码），**现在切入是合适窗口**。

---

## 2. GEO 现状的「Loop 友好度」体检

> 一句话结论：**GEO 的架构离 Loop 化只差三块拼图，不是推倒重来。**

### 2.1 已经具备的（不用动）

| GEO 能力 | 在 Loop 里扮演什么 |
|----------|--------------------|
| `pipeline` 节点注册表（`nodes/base.py` 那套） | 加新 Loop 节点 = 写一个新 handler 注册一下，**1 天工作量** |
| `driver` 注册表（头条 / 微信 / 抽屉…） | Agent 的"嘴"——往哪个平台发就调哪个 driver |
| `question_bank` 飞书多维表同步 + 问题池 | 选题 Loop 的"目标池" |
| `scheme_executor` 的 ThreadPoolExecutor 并发生文 | 已经是 Loop 内核了，差一个评估闸 |
| `hot_lists` 反向代理 DailyHotApi（自带 60+ 热榜源） | 选题 Loop 的"原料抓取"工具，**直接能用** |
| `worker` 状态机 + 乐观锁 + 租约 | Loop 的"调度器底盘"，多 Loop 并发抢工作天然安全 |
| 提示词模板（generation + ai_format 两个 scope） | Loop 自迭代时换 prompt 的"基因池" |
| AI engine 切换（多模型可切，下拉切换 LiteLLM） | Loop 自迭代时换模型的"基因池" |
| 审计日志 `AuditLog` | Loop 的"行为黑匣子"，可观测性已具备 |

### 2.2 缺的三块拼图

1. **评估器（"能说不"的机制）**——目前从生成到分发，全链路只有一个人工审核闸；AI 自己说不出"这篇不好、要重写"。
2. **反馈回流**——文章发布后阅读量/互动数没有自动拉回；提示词模板和写作模型谁好谁差全靠运营拍脑袋。
3. **回路结构**——pipeline 现在严格线性（`node_index` 递增执行），没有"评估不达标 → 回上一节点重试"的环。

这三块**正好是 Loop Engineering 的核心增量**，也是本讨论稿要解决的问题。

### 2.3 Loop 友好度小结

```
┌─────────────────────────────────────────────────┐
│ GEO 已经是 70% 的 Loop 基础设施                  │
│                                                  │
│ ✅ 持久化状态机     ✅ 节点注册表                 │
│ ✅ 并发调度         ✅ 多模型切换                 │
│ ✅ 提示词管理       ✅ 热榜/飞书 集成             │
│                                                  │
│ ❌ 评估器           ❌ 反馈回流   ❌ 回路 DAG     │
└─────────────────────────────────────────────────┘
```

补这三块，GEO 就从"自动化平台"升级成"自迭代平台"。

---

## 3. 三段链路的「现状 → Loop 化」对照

### 3.1 选题环节（**最关心的瓶颈**）

| 维度 | 现状 | Loop 化之后 |
|------|------|-------------|
| 谁来发现热词 | 运营每天手工逛、录到飞书文档 | 选题 Agent 每小时从 60+ 热榜源 + 历史命中文章自动抓 |
| 怎么判断好坏 | 凭经验，主观 | 评估器按"热度趋势 + 历史同主题过审率 + 历史同主题阅读量"打分 |
| 词什么时候更新 | 看人有没有时间 | 每小时跑一次、不达标自动跑下一轮 |
| 谁评价准不准 | 没人评价 | 每个被选中的词都关联到后续文章 → 文章效果数据回流给评估器自学习 |
| 输出去哪 | 飞书文档 | 飞书多维表的问题池（GEO 已经在用），后续方案/pipeline 直接接走 |

### 3.2 生文 + 审核环节

| 维度 | 现状 | Loop 化之后 |
|------|------|-------------|
| 一篇文章迭代几次 | 1 次（生成完直接送审） | N 次（评估 < 阈值就自动 refiner 重写一遍，最多 3 轮） |
| 人审做什么 | 看每一篇 | 抽审（AI 评估高分的样本抽 10%，低分的全审） |
| 失败的提示词模板会怎样 | 还在那里被反复选用 | 自动 down-rank，连续 N 次低分会从可选列表移除 |

### 3.3 分发 + 复盘环节

| 维度 | 现状 | Loop 化之后 |
|------|------|-------------|
| 分发完之后 | 流程结束 | 启动"回流 Loop"：24h / 7d 拉回阅读+互动数据 |
| 哪个平台数据好 | 凭感觉 | 每个 driver 自动产出"账号 × 模板 × 模型 × 选题"四维归因表 |
| 谁优化谁淘汰 | 运营开会决定 | Loop 自动调整：差的下线、好的加权、新平台自动小流量探索 |

---

## 4. 方案 A：「三段挂载 Loop」（推荐起步）

> **一句话**：不动现有 pipeline 架构，在三段链路外侧分别挂一段独立 Loop，每段都是独立项目可独立上线。

### 4.1 选题 Loop（POC 首选，1-2 周）

```
   ┌──────────────────────────────────────────────┐
   │   每小时一次（cron-style，复用现有 scheduler）  │
   └──────────────────────────────────────────────┘
                       │
   ┌───────────────────▼────────────────────┐
   │  Step 1: 抓 — hot_lists 60+ 源 + 历史   │
   │           表现好的同主题文章            │
   └───────────────────┬────────────────────┘
                       │
   ┌───────────────────▼────────────────────┐
   │  Step 2: 评 — 评估 Agent 按三维打分     │
   │   (热度趋势 + 同主题过审率 + 阅读量)    │
   └───────────────────┬────────────────────┘
                       │
   ┌───────────────────▼────────────────────┐
   │  Step 3: 写回飞书问题池                  │
   │   (达标的进池，不达标的进"观察池")        │
   └───────────────────┬────────────────────┘
                       │
            ┌──────────▼──────────┐
            │  评估器自学习参数    │
            │  (基于后续文章效果)  │
            └──────────────────────┘
```

**目标**：
- 上线 2 周后：飞书选题池里 80% 的词来自 Agent 自动抓取，运营只做"否决"不做"录入"
- 上线 1 个月后：选题命中率（被采用的词所产出的文章过审率）有第一份基线数据
- 上线 2 个月后：基线 + 10%，因为评估器开始自学习

**改动量**：1 张表（`topic_loop_candidates`）、1 个新的 pipeline 节点类型（`topic_evaluator`）、1 个调度入口、复用现有 `hot_lists` + 飞书 + 提示词模板。**1 个工程师 2 周可出 POC**。

### 4.2 评审 Loop（A 跑通后 3-4 周）

- 在 `to_review` 之前插入 `evaluator` + `refiner` 两个新节点
- 评估器对每篇生成结果按维度打分（事实性 / 可读性 / 风格匹配 / 政策风险）
- 不达标自动让 `refiner` 重写一次，最多 3 轮
- 节点已经是 GEO 一等公民概念（注册表那套），是顺势演进而非另起炉灶——这其实就是**方案 B 的一部分提前内化进 A**

**目标**：由人工审核，改为 Loop 自动审核；失败模板自动降权。

### 4.3 回流 Loop（5-8 周）

- 在 `publish_records` 完成后 24h / 7d 触发"回流 Agent"
- 通过平台分析 API（头条统计 / 微信公众号 stat / 抖音开放平台）或半自动飞书表录入拉回阅读+互动
- 回流数据写回到 `Article.metrics`、`Prompt_template.score`、`AccountConfig.score`
- 闭环回到选题 Loop 的评估器（这一步形成大循环）

**目标**：第一份"账号 × 模板 × 模型 × 选题"四维归因表；每周一封自动邮件。

### 4.4 方案 A 总账

| 项 | 投入 | 收益时间窗 |
|----|------|------------|
| 选题 Loop POC | 1 人 × 2 周 | 2 周后看到第一批自动选题 |
| 评审 Loop | 1 人 × 3-4 周 | 1 个月后看到过审率数字 |
| 回流 Loop | 1 人 × 4-6 周（依赖各平台 API 接入） | 2 个月后看到归因表 |
| **小计** | **约 2-3 人月** | **2 个月内三段都有数字** |

**风险**：低。三段都是"外挂"，跑不动随时关掉不影响现有发布。

---

## 5. 方案 C：「Agent Town」愿景版（3-6 个月）

> **一句话**：把 pipeline 从"执行步骤"升级成"任务规格"——运营只声明目标，一组常驻 Agent 自己分工跑。

### 5.1 角色矩阵

```
  ┌────────────┐     ┌────────────┐     ┌────────────┐
  │ 选题 Agent  │ ──► │ 写作 Agent  │ ──► │ 评审 Agent  │
  │ (Loop)     │     │ (Loop)     │     │ (Loop)     │
  └────────────┘     └────────────┘     └────────────┘
        ▲                                      │
        │                                      ▼
  ┌────────────┐     ┌────────────┐     ┌────────────┐
  │ 复盘 Agent  │ ◄── │ 分发 Agent  │ ◄── │ 配图 Agent  │
  │ (Loop)     │     │ (Loop)     │     │ (Loop)     │
  └────────────┘     └────────────┘     └────────────┘
        │
        ▼ 学习/调参
  ┌────────────────────────────────────────────────┐
  │  共享任务池 + 状态机 + 预算闸 + 人工 checkpoint  │
  └────────────────────────────────────────────────┘
```

**运营的工作变成**：
- 每周一在 GEO 后台设目标：「这周产 300 篇过审 + 分发到这 12 个账号，预算 500 元 API 费用」
- 设几个人工 checkpoint（"发布之前必须我点确认"或者全自动）
- 周五看一份系统自动产出的「本周复盘 + 下周建议」

### 5.2 跟现有生态的关系

| 组件 | 在 Agent Town 里的角色 |
|------|------------------------|
| **GEO 的 pipeline UI（智能体管理）** | 升级成"角色 + 目标 + 预算"的声明式 UI，pipeline 仍然存在但被降级为"Agent 的脚本之一" |
| **Skill 层**（目前休眠） | **激活**——作为各 Agent 的能力包（写作 skill、评估 skill、归因 skill） |
| **MCP** | 作为对外接口：MCP server 暴露飞书、热榜、各平台数据；MCP client 让 GEO Agent 复用外部能力 |
| **OpenClaw**（外部生态） | OpenClaw 是 "保活 Agent、移动世界" 的另一种范式；Agent Town 是 "保活世界、移动 Agent"。两者不冲突——可以让某个 Agent 跑在 OpenClaw 内做长寿命任务（如"持续跟踪一个账号一周"），主调度仍在 Agent Town |
| **Claude Code 的 /loop /goal /batch** | 内部研发提效用——团队用 Claude Code 帮 GEO 改代码本身也是 Loop Engineering 的实践 |

### 5.3 必须配的"刹车"

- **预算闸**：每个 Agent 每天 API 花费有上限，超了自动停
- **强制人工 checkpoint**：发布、删账号、调主流量分发策略 = 必须人工二次确认
- **行为黑匣子**：所有 Agent 决策走 `AuditLog`，运营随时回溯
- **沙盒账号优先**：所有 Agent 探索新平台都先用"沙盒账号"小流量跑，达标才动主账号

### 5.4 业务讲法

- 内部叙事："运营成本从'每人盯 3 个账号'降到'每人定 30 个账号的策略'"
- 对外（融资 / 客户）叙事："国内第一个面向内容运营的多 Agent 协作平台"
- 风险叙事：必须坦诚——"行业还没有真正跑通的 Agent Town 案例（明略 Octo 也只是声称），我们是探索者，所以分阶段、有刹车、有预算闸"

---

## 6. 方案 B 在哪：「A → B → C」的演进自然态

方案 B（把 evaluator / refiner / stop_condition / metrics_collector 做成 pipeline 一等节点）并不需要单独立项——它就是 **方案 A 的评审 Loop 和回流 Loop 落地时的自然形态**。重点不是"做不做 B"，而是确保 A 的实现方式留好"回路 DAG"的扩展空间（pipeline 编辑器要能画环、`flow_meta` 要支持 `loop_back_to: node_id`、最大轮次/超时要内置）。

---

## 7. 跟外部生态的关系（给技术 leader 看）

| 外部范式 | 跟 GEO 的关系 | 我们的判断 |
|----------|---------------|------------|
| **Ralph Loop**（Huntley） | 极简单 Agent 自迭代；适合"目标明确 + 自动验证"的场景 | 方案 A 的选题 Loop **本质就是一个 Ralph**——目标=填满问题池，验证=评估器打分 |
| **Gas Town / Octo**（多 Agent 协作） | 多 Agent + 任务池 + 分子粒度任务 | 方案 C 的内核形态，**但不要 100% 抄**——内容运营场景没必要分到"分子粒度" |
| **OpenClaw**（长寿命 Agent） | "保活 Agent、移动世界" | 方案 C 里**可选项**，用于"持续盯一个账号一周"这种长任务 |
| **MCP** | 协议层 | 方案 C 的对外接口；**方案 A 不需要 MCP**，先把内部 Loop 跑通 |
| **Claude Code /loop** | 工具层 | 团队研发提效用；不直接进 GEO 架构 |
| **LangGraph**（GEO 已有） | Graph 化 Agent 编排 | **方案 A 评审 Loop 直接复用**——已有的依赖，不引入新栈 |

---

## 8. 下一步决策项

### 8.1 立刻能做的（这周或下周）

- [ ] 拍板：是否同意 **方案 A 起步 + 方案 C 作为愿景** 的双轨路线
- [ ] 立项：选题 Loop POC，1 个工程师 × 2 周
- [ ] 准备：评估器的"打分维度 + 评分模型 + 提示词初稿"——需要专家参与定义

### 8.2 POC 验收标准

- 选题 Loop 每小时自动跑一次
- 飞书问题池里至少 80% 新增条目来自 Agent
- 每条新增条目带评分（0-100）+ 评分理由
- 运营可以在飞书表里"否决/通过"，反馈写回 Loop 让评估器调整权重
- 上线 1 周后产出第一份"自动选题命中率"报表

### 8.3 立项后 1-2 个月的节奏建议

| 周次 | 里程碑 |
|------|--------|
| W1-W2 | 选题 Loop POC 上线 |
| W3-W6 | 评审 Loop（带 evaluator + refiner 节点） |
| W5-W8 | 回流 Loop（接 1-2 个平台分析 API） |
| W9    | 内部复盘：方案 C 立项 or 继续优化 A |

### 8.4 留给后续讨论的开放问题

1. **预算**：Agent 的 API 费用每月上限是多少？这直接决定 Loop 的最大轮次和并发 Agent 数。
2. **方向认可**：方案 A 到 C 的整体规划方向是否 ok？需要对双轨路线的拍板。
3. **跟同事的协调**：已有同事在做选题这块——是把方案 A 的选题 Loop 并入他的工作，还是双轨独立？需要先内部对齐。
4. **人手**：方案 A 全套 2-3 人月，现有团队能挤出来吗？是否需要新招 1 人专门做 Agent/Loop 方向？

---

## 9. 附录：术语对照表

| 术语 | 在本稿中的意思 |
|------|----------------|
| Loop Engineering | 设计、运营、改进让 Agent 自迭代的循环系统的工程实践 |
| Ralph Loop | 单 Agent 在 bash 里死循环跑、靠磁盘/git 保留状态的极简实现 |
| Agent Town / Gas Town | 多 Agent 协作平台，任务池 + 状态机 + 分子粒度任务 |
| Loop 五件套 | 目标 / 上下文 / 工具 / 评估 / 停止条件 |
| Evaluator / Refiner | Loop 里负责"能说不"和"按反馈重做"的角色 |
| 选题 Loop / 评审 Loop / 回流 Loop | 本稿提出的方案 A 三段挂载 Loop |

---

## 引用来源（链接保留供 leader 后续深读）

- [Loop Engineering: How to Design Coding Agent Loops That Run While You Sleep](https://explainx.ai/blog/loop-engineering-coding-agents-claude-code-guide-2026)（explainx.ai，2026 完整指南）
- [What Is Loop Engineering? The New Meta for AI Coding Agents](https://www.mindstudio.ai/blog/what-is-loop-engineering-ai-coding-agents)
- [Inventing the Ralph Wiggum Loop（Geoffrey Huntley 访谈）](https://devinterrupted.substack.com/p/inventing-the-ralph-wiggum-loop-creator)
- [Ralph vs. OpenClaw — Understanding Process](https://kenhuangus.substack.com/p/ralph-vs-openclaw-understanding-process)
- [The Ralph Loop: How Recursive AI Agents Actually Work](https://thomas-wiegold.com/blog/ralph-loop-how-recursive-ai-agents-work/)
- [Anthropic 官方 ralph-wiggum 插件 README](https://github.com/anthropics/claude-code/blob/main/plugins/ralph-wiggum/README.md)
- [Loop Engineering 循环工程（菜鸟教程中文版）](https://www.runoob.com/ai-agent/loop-engineering.html)
- [行业还在争论 Loop Engineering，这家公司已经跑了近 3000 个 Agent（网易科技，明略 Octo 报道）](https://www.163.com/dy/article/KVFDE8IC0534A4SC.html)
- [Designing Loops — A Practitioner's Short Field Guide](https://interestingengineering.substack.com/p/designing-loops-a-practitioners-short)
