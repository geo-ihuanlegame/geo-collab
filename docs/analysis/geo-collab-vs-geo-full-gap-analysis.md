# geo-collab 现状 vs geo-full 愿景 · 差距评估与演进路线

> 编写日期：2026-06-02
> 锚点材料：`geo-full.html`（GEO 产品方案完整版，9 章节）与当前 geo-collab 代码库（HEAD `35fdb98`）
> 目的：把「已经建好的代码」和「geo-full 愿景」逐层对齐，给出可复用 / 需改造 / 全新建的判断，并把那张 15 个月理想路线图重新落到代码现实上，作为团队决策依据。
> 读者：技术 Lead / 运营 Lead / 项目决策人。

---

## 1. 执行摘要

**一句话结论**：当前 geo-collab 不是「半成品」，而是 geo-full 六层愿景里**最成熟、最难啃的两层（内容生产 Layer 3 + 分发 Layer 4）的扎实基石**；真正的岔路口不在「还要建什么功能」，而在**架构取向**——现在的代码是「面向发布任务的模块化单体」，而愿景要求的是「以工作流引擎为内核、一切能力皆为可编排节点」。

**三个关键判断**：

1. **分发层的核心抽象已经对了。** `PlatformDriver` Protocol + 驱动注册表（[drivers/__init__.py](../server/app/modules/tasks/drivers/__init__.py)）本质上**就是 geo-full 说的「平台适配器节点」**，只是目前只实现了一个驱动（头条）。多平台扩展是「写 N 个驱动」，不是「重建分发层」。这是最大的好消息。

2. **从零开始的长杆子是监测层（Layer 1）、归因落地页（Layer 5 的 L2）、决策中枢数据仓库（Layer 6）。** 这三块今天代码量为零，且互相依赖，是真正的关键路径。geo-full 的路线图把「头条闭环」当 S1 里程碑，但代码现实是：分发那一半已基本做完，**缺的是监测那一半 + 仪表盘**。

3. **不建议为了愿景而大爆炸重写。** 代码库里已经有**两个原型编排器**——发布调度引擎（[tasks/executor.py](../server/app/modules/tasks/executor.py)）和 LangGraph 生文管道（[ai_generation/pipeline.py](../server/app/modules/ai_generation/pipeline.py)）。聪明的做法是让它们**收敛到一个统一的节点抽象**，而不是丢掉两个去换 n8n。

**推荐路径**：混合渐进（详见 §4）。先修少量「扛不住愿景负载」的技术债，定下数据栈方向，再用一个薄编排层把现有两个原型引擎统一，最后优先攻监测层 MVP（最长杆子且能复用浏览器自动化基建）。

---

## 2. 现状能力地图（geo-collab 投影到六层）

| Layer | 愿景子系统 | geo-collab 现状 | 成熟度 |
|---|---|---|---|
| **L1 输入/监测** | Sensor Layer：每日扫描 12 引擎 × 关键词宇宙 → 提及率时序 | **空白**（但浏览器自动化基建可复用） | ⬜ 0% |
| **L2 内核** | 工作流引擎：可视化拖拽节点编排 | **空白**（但有两个专用原型编排器） | ⬜ 0%（编排能力）/ 🟨 内核雏形已存在 |
| **L3 生产** | 内容生产：选题→大纲→正文→GEO优化→配图→审核→快照 | **部分建成，最强区** | 🟩 ~45% |
| **L4 分发** | 多平台账号池 + 发布通道 + 风控 | **部分建成，第二强区** | 🟩 ~40% |
| **L5 痛点1** | 归因层：L1露出/L2中转/L3时序/L4实验/L5自报 | **空白**（仅存了 publish_url 原料） | ⬜ ~5% |
| **L6 决策** | 数据回收 + ROI 仪表盘 + 内容效能模型 + 策略反推 | **空白**（有运营数据 + 审计日志，无分析层） | ⬜ ~5% |
| 视频（L3-P2） | 视频自动化流水线 | **空白**（愿景本身就排在二期） | ⬜ 0% |

**图例**：🟩 已有可观基础 · 🟨 雏形/可演进 · ⬜ 基本从零

---

## 3. 逐层差距分析

每层给出：**愿景要求 → 现状 → 缺口 → 三色判断**。
三色判断：🟢 **复用**（现成可接）· 🟡 **改造**（有基础需重构/扩展）· 🔴 **新建**（基本从零）。

### Layer 3 · 内容生产（代码级深挖）

这是 geo-collab 投入最多、最值得逐节点拆的一层。把愿景里的 7 节点流水线（3.1）逐一对照：

| 愿景节点 | 现状落点 | 判断 | 说明 |
|---|---|---|---|
| 监测信号触发 | 无（依赖 L1） | 🔴 | 当前生文是人工/问题库触发，没有「关键词 hit rate 低 → 自动触发选题」 |
| **选题节点** | `ai_generation/question_bank.py`（飞书多维表同步、auto/manual 抽题） | 🟡 | 已有问题池 + 板块轮转 + 飞书同步（`feishu_bitable.py`），但选题逻辑是「问题驱动」，不是「监测信号驱动」 |
| **大纲节点** | 无独立节点 | 🔴 | 当前 [pipeline.py](../server/app/modules/ai_generation/pipeline.py) 是**单次直出**：planner 节点已废弃（L40-42 注释明说），没有「先出大纲再写正文」的分步 |
| **正文节点** | `pipeline.parallel_write_node`（LiteLLM + `ThreadPoolExecutor(max_workers=4)`） | 🟢 | 并发写作已做，幂等用 `client_request_id` |
| **GEO 优化节点（CORE）** | **无** | 🔴 | **这是和普通 AIGC 工具最大的差异点（愿景 3.2），却完全没建**：没有结构化引用、实体锚点、FAQ 块、时效标签的转换。当前只有 `articles/ai_format.py` 做标题识别 / 自动插图，不是 GEO 结构化 |
| **配图节点** | `image_library/`（MinIO 图库、`selector.py`/`inserter.py`/`hook.py`）+ `ai_format` 自动插图 | 🟡 | 有「从图库选图并插入正文」的完整链路，但只是**检索式配图**，没有生成式（即梦/MJ/SDXL） |
| **审核节点** | 仅 `nh3` HTML 消毒 | 🔴 | 没有敏感词/风险词、RAG 事实校验、竞品合规、人工抽检流。法务红线（3.5 L3）目前无任何代码兜底 |
| **资产版本快照** | `Article.version` + `client_request_id` + `generation_sessions`（`article_ids` JSON） | 🟡 | 有版本号和批次元数据，但**没有按愿景 3.6 记录 `prompt_version` / `model` / `geo_features` 的内容级快照**——而这正是事后归因「什么内容 hit rate 最高」的依据 |

**额外两项支撑能力**：

- **Prompt 模板库**（愿景 3.3）：🟢 已有 `prompt_templates/` 模块 + `skills/`（SKILL.md + references + skeletons）。模板带 metadata 的雏形在 skill 结构里已存在，是不错的地基。但愿景要的「适用关键词层级 / 目标引擎 / 历史 hit rate」这类可被工作流自动选模板的 metadata 还没有。
- **多模型组合**（愿景 3.4，大纲/主笔/润色/配图分职责）：🟡 当前只有**两套**模型配置（`GEO_AI_MODEL` 主写作 + `GEO_AI_FORMAT_MODEL` 格式），都走 LiteLLM。要做到按职责路由（大纲用 Opus、主笔用 Sonnet、润色用 Flash）只需扩展配置 + 路由表，**架构上零障碍**（LiteLLM 已是统一网关）。
- **RAG 知识库**（愿景 3.4）：🔴 完全没有，需要新建向量库 + 检索注入。

**Layer 3 小结**：正文生成、配图、模板、问题库这些「生产基础设施」已经不错；**真正缺的是把普通 AIGC 升级成 GEO 内容的那一道关键转换（GEO 优化节点）和审核节点**——这两个恰恰是产品差异化和合规底线所在。

### Layer 4 · 分发（代码级深挖）

| 愿景要素 | 现状落点 | 判断 | 说明 |
|---|---|---|---|
| **平台适配器节点（统一接口，4.4）** | `PlatformDriver` Protocol + `register()` 注册表 | 🟢 | **架构完全对路**。愿景画的那个 `{platform, account, content, schedule_at, fallback}` 接口，代码里就是 `PublishPayload` + `driver.publish()`。这是整个项目最有前瞻性的设计 |
| 平台覆盖 | 仅 `toutiao.py`（1028 行） | 🔴 | 愿景要 6 国内 + 2 海外；现有 1 个。但扩展成本是**线性的、有界的**（每平台 = 一个驱动 + DOM 校验/API 对接），不是架构性的 |
| 发布通道 | 仅浏览器自动化（Playwright） | 🟡 | 愿景里头条/百家号/知乎**优先走官方 API**，当前全是 Web 自动化。API 通道更稳更快，但需新增非浏览器的发布路径（驱动协议可容纳，但 `runner.py` 现在硬绑 Playwright 会话） |
| **账号池矩阵（4.2）** | `accounts/` 模块（CRUD、登录会话状态机、浏览器 profile、profile 锁） | 🟡 | 多账号管理已建，但没有愿景要的「人设标签 / 风险分级（绿黄红）/ 养号健康度周扫」 |
| **风控规避（4.3，关键）** | 仅 per-account 串行锁 | 🔴 | **重大缺口**：没有频率控制（每账号每日上限/错峰）、没有内容差异化（同主题多改写版）、没有 IP/设备隔离（独立代理 + 指纹）、没有账号健康度分级预警。当前规模（单平台）无感，但**这是多平台上量时的封号风险源** |
| 远程人工接管（noVNC） | `accounts/browser.py` + `UserInputRequired` + Xvfb/x11vnc/websockify/noVNC | 🟢 | 做得相当完善，处理验证码/登录失效的人工接管。对风控场景是加分项 |
| 链式分发调度 | `scheduled_at` + `group_round_robin` 任务类型 | 🟡 | 有定时和分组轮转，但没有愿景的跨平台 DAG 链（「先头条→3h后知乎→次日小红书」）。这正好是 Layer 2 工作流引擎该提供的能力 |

**Layer 4 小结**：**适配器抽象和人工接管是亮点**，多平台扩展是「填驱动」的有界工作；**最该补的是风控子系统**（频率/差异化/IP隔离/健康度）——它不补，多平台上量就是在裸奔。

### Layer 1 · 监测（战略级）

- **愿景要求**：每日扫描国内 7 + 海外 5 共 12 个 AI 引擎 × ~1500 关键词宇宙（4 层），输出 `{engine, query, mentions, brand_position, citation_is_ours, sentiment, raw_screenshot}` 时序测点（2.3），多频率错峰扫描（2.4）。
- **现状**：🔴 零。
- **可复用的间接资产**：geo-collab 的**浏览器自动化基建可直接服务监测**——豆包/通义/文心/元宝这些「Web 自动化」引擎，正好能复用 `runner.py` 的 Playwright 持久化上下文、账号池、profile 锁、noVNC 人工接管（扫码登录 AI 引擎时）。飞书同步（`feishu_bitable.py`）的模式也能用于关键词宇宙的外部维护。
- **判断**：🔴 主体新建，但**浏览器抓取层 🟢 可复用现有自动化栈**，不必从零造抓取器。
- **数据栈警示**：监测层是**时序密集型**（每日多次扫描留存），愿景建议 Postgres + TimescaleDB（D6.1），而 geo-collab 是 **MySQL only**。这是一个必须在 L1 设计期拍板的数据栈分叉（见 §6）。

### Layer 2 · 工作流引擎（战略级，最关键的架构分歧）

- **愿景要求**：所有子系统功能封装为「节点」，运营可视化拖拽组合 GEO 策略；一期确定性规则节点，二期可换 Agent 节点。这是 geo-full 反复强调的**内核**。
- **现状**：🟨 没有「通用可视化编排引擎」，但**有两个专用的原型编排器**：
  1. **发布调度引擎** [tasks/executor.py](../server/app/modules/tasks/executor.py)：四级并发控制（per-task 锁 → 全局信号量 → per-account 锁 → 跨进程 profile 锁）、双层状态机、乐观锁租约、超时/僵死恢复。它**编码了大量来之不易的领域逻辑**。
  2. **LangGraph 生文管道** [pipeline.py](../server/app/modules/ai_generation/pipeline.py)：`planner → parallel_write → finalize` 三节点图。**它本身就是一个图编排器**，只是节点是硬编码的、不面向运营。
- **判断**：🔴 面向运营的可视化编排是新建，但 🟨 **图/节点的内核雏形已存在**——驱动注册表是节点注册表，LangGraph 是图执行器。关键决策是「在这两个雏形上长出统一节点层」还是「引入外部引擎」（详见 §4）。

### Layer 5 · 归因（战略级，痛点 1）

- **愿景要求**：5 层归因模型（L1 露出 0.10 / L2 UTM 中转 0.30 / L3 时序桥接 0.20 / L4 实验对照 0.30 / L5 用户自报 0.10），综合评分公式（5.3），专属落地页系统 + 渠道码 + 实验调度器 + 归因数据仓库（5.4）。
- **现状**：🔴 ~5%。`PublishRecord.publish_url` 存了每条发布的落地 URL，是 L1/L2 的原料起点，但没有任何归因计算。
- **判断**：
  - L1 露出归因 🟢 **复用 Layer 1 监测**（mentions/brand_position/citation_is_ours 直接就是 L1 指标）——所以监测层做了，L1 归因几乎白送。
  - L2 中转归因 🔴 需新建**独立的 Web 落地页子系统**（短链 + UTM + 来源识别 + deep link）。注意 geo-collab 的 FastAPI 已服务 SPA，技术上能扩展，但这是一个新的对外 Web 面，需独立域名（D5.5）。
  - L3 时序桥接 🔴 依赖 Layer 6 数据仓库 + 外部品牌搜索量（巨量/百度指数）。
  - L4 实验对照 / L5 自报 🔴 全新（L5 还需 App 客户端配合，是外部依赖 D5.3）。

### Layer 6 · 决策中枢（战略级）

- **愿景要求**：汇总监测/生产/分发/归因数据 → 数据仓库 → ROI 仪表盘 + 内容效能模型 + 策略反推器 → 回流 Layer 1/2 形成闭环（6.1）。
- **现状**：🔴 ~5%。有分散的运营数据（`publish_records`、`articles`、`generation_sessions`）和**审计日志**（`audit/` 模块，游标分页，admin 专用），但没有数据仓库、没有 BI、没有效能模型。
- **判断**：🔴 主体新建。`audit/` 的游标分页和 admin 边界可作为「内部数据查询面」的起点，但分析/仓库/仪表盘是新栈。仪表盘愿景建议「核心自建 + Metabase」（D6.2）。

---

## 4. 核心架构分歧：模块化单体 vs 工作流引擎内核

这是整份报告的**重心**，也是最该被决策的一点。

**张力**：geo-full 的核心论点是「工作流引擎是内核，不是数据库、不是 LLM；所有子系统都是它的节点，运营拖拽编排」。而 geo-collab 现实是硬编码的模块化单体——`tasks/executor.py` 是一个强大但**专用**的发布调度器，`ai_generation/pipeline.py` 是一个 LangGraph 图但不面向运营。两者都不是「通用可视化节点引擎」。

### 三种过渡方案

**方案一 · 维持单体，新能力当模块加**
- 做法：监测、归因、决策中枢都按现有 `modules/` 四件套模式新增，不引入编排内核。
- 优点：短期最快，团队零学习成本，CI/测试模式不变。
- 代价：**永远到不了「运营可编排」的愿景**；跨子系统的链式策略（链式分发、监测触发选题）全靠硬编码胶水，随子系统增多指数级变脆；与 geo-full 的核心主张直接背离。

**方案二 · 引入成熟工作流引擎当内核（n8n / Dify / Temporal）**
- 做法：选一个外部引擎做编排内核，现有模块退化为被它调用的服务/节点。
- 优点：最贴愿景，可视化编排开箱即用（n8n/Dify），或拿到工业级持久化编排（Temporal）。
- 代价：**大前期重平台化**；更关键的是——`tasks/executor.py` 里的领域逻辑（账号串行锁、profile 租约、noVNC 接管、Playwright greenlet 线程亲和）**无法干净地映射到通用引擎的节点模型上**，强塞会丢掉这些来之不易的正确性保证。n8n/Dify 的「节点」也不是为「单实例发布 worker + 浏览器会话保活」这种有状态长任务设计的。

**方案三（推荐）· 混合渐进**
- 做法：
  1. 把现有发布运行时（executor + driver 协议）**保留为「发布运行时」**——它编码的领域逻辑是资产，不是债务。
  2. 在其之上引入一个**薄编排/节点层**：定义统一的「节点接口」，让驱动注册表、LangGraph 节点、未来的监测/归因能力都实现同一个节点契约。
  3. **推迟选重型引擎**：直到 Layer 1/2 真的需要跨子系统 DAG 时，再决定是自研薄编排还是用 Temporal 做持久化后端。可视化编辑器是更后面的事。
- 理由：代码库**已经有两个图/节点雏形**（驱动注册表 = 节点注册表，LangGraph = 图执行器）。收敛它们到一个公共抽象，比「丢掉两个去换 n8n」风险低得多，也比「维持单体」更接近愿景。这条路让每一步都有可工作的产物，不赌一次大爆炸。

> **建议**：采纳方案三。把 geo-full 的「工作流引擎内核」理解为**渐进收敛的目标态**，而不是「S0 就要选型落地的前提」。S0 的真正任务是「统一节点接口 + 收敛两个原型引擎」，而非「从零选 n8n」。

---

## 5. 路线现实校准（把 geo-full S0–S6 落到代码现实）

geo-full 的 6 阶段路线图（Section 8）假设大部分东西从零建。但代码现实是：**内容+分发已领先于那张图，监测+归因+仓库则是真正的零起点长杆子。** 逐阶段校准：

| 愿景阶段 | geo-full 设定 | 代码现实校准 |
|---|---|---|
| **S0 基建**（M1） | 工作流引擎选型（n8n/Dify/Temporal/自研） | **不是从零选型**。现有发布运行时 + 驱动协议 + LangGraph 已是可工作内核。S0 应改为：①定义统一节点接口、收敛两个原型引擎；②拍板数据栈方向（MySQL 扩展 vs 引入时序库）；③修前置技术债（§6）。 |
| **S1 国内单引擎单平台闭环**（M2-3） | 把「手工跑头条」完整自动化 | **分发那一半基本做完**（头条驱动 + 账号池 + 调度 + 人工接管）。S1 真正缺的是**监测（豆包）那一半 + 仪表盘 v0**。闭环的瓶颈在监测，不在分发。 |
| **S2 多引擎多平台**（M4-5） | 监测扩 7 引擎、分发扩 4 平台、账号池系统、归因 L2/L3 | 分发扩平台 = **填驱动**（有界工作），不是架构活。真正的工作量在：监测 7 引擎、**风控子系统**（§3 Layer4 红色项）、归因 L2 落地页（新 Web 面）。 |
| **S3 海外 MVP + L5**（M6-7） | 海外 5 引擎、Medium/Reddit、L5 问卷 | 海外平台同样靠驱动协议；L5 依赖 App 客户端配合（外部，D5.3）。 |
| **S4 上线 + 视频启动**（M8-9） | 一期上线、视频接口预留 | 受益于现有 noVNC/任务引擎的运维成熟度。 |
| **S5-S6 二期**（M10-15） | 视频自动化、归因深化、ML 反推 | 视频是真·从零；ML 反推依赖 Layer 6 数据积累。 |

**真实关键路径（最长杆子，按依赖排序）**：
1. **数据栈拍板**（gate 住 L1 和 L6）→
2. **监测层 MVP**（L1，从零，但复用浏览器基建；它一做，L1 归因白送）→
3. **归因 L2 落地页**（新 Web 子系统）→
4. **决策中枢仪表盘 v0**（闭环才看得见 ROI）。

> 内容生产和分发**不在关键路径上**——它们已经够用来喂下一阶段。把工程火力压在监测+归因+仓库这三块从零的子系统上，闭环才能真正合上。

---

## 6. 承载愿景的前置技术债

这些是我在代码评审中发现的债务，**按「会不会卡住愿景负载」重新排序**——不是为洁癖，是为承重。

| 优先级 | 债务 | 位置 | 为什么是愿景级阻塞 |
|---|---|---|---|
| 🔴 高 | **发布 worker 单实例** | `worker/executor.py` + executor 进程内锁 | 今天单平台头条无感。但 L1 监测（每日扫 12 引擎）+ 多平台上量会**饱和单 worker**。乐观锁基建已支持多实例，**唯一阻塞是账号登录处理器（`_account_login_loop`）非多进程安全**——必须在上量前解决。 |
| 🔴 高 | **三份内容并行存储手动同步** | `Article.content_json/html/plain_text`，[articles/service.py](../server/app/modules/articles/service.py) | 随着 GEO 优化节点、审核节点、多平台改写版增多，写正文的路径会爆炸式增长，手动三同步会变成**正确性炸弹**。应收敛为单一真源 + 派生。 |
| 🟡 中 | **plain_text 直接存原始 Markdown** | [pipeline.py:107](../server/app/modules/ai_generation/pipeline.py#L107)（注释「简化」） | 发布纯文本里残留 `#`/`**` 等标记，**直接伤发布质量**——而监测/归因恰恰在度量发布质量，脏数据会污染整条归因链。早修。 |
| 🟡 中 | **数据栈 MySQL only vs 时序需求** | `core/config.py`、`alembic` | L1 监测是时序密集型，愿景建议 TimescaleDB（D6.1）。MySQL 能扛但非最优。**在 L1 设计期必须拍板**：扩展 MySQL 还是引入专用时序库。 |
| 🟢 低 | **README 仍是 GitLab 默认模板** | `README.md` | 指向 `hlgit.5518game.com`，但已迁 GitHub Actions。对新成员误导，CLAUDE.md 才是事实源。本周可改。 |
| 🟢 低 | **`openai==2.38.0` 显式钉入依赖** | `requirements.txt` + [pipeline.py:_inject_api_key](../server/app/modules/ai_generation/pipeline.py#L221) | 与「全走 LiteLLM、不 import openai/anthropic SDK」的规则张力；虽是 litellm 传递依赖，但显式 pin + 同时注入两个 env key 易误导。加注释澄清即可。 |
| 🟢 低 | **`require_local_token()` 死代码** | `core/security.py` | 已知死代码，清理。 |

---

## 7. 决策点 × 代码映射

geo-full Section 9 列了 30+ 决策点（D1–D8）。这里只挑**代码现实能给出新信息**的几个，标注是「被代码阻塞/影响」还是「纯业务」：

| 决策点 | 主题 | 代码视角 |
|---|---|---|
| **D2.2** | 抓取方案：自研 Playwright vs 第三方 GEO 工具 | **代码已倾向自研**。geo-collab 已有成熟的 Playwright + 账号池 + noVNC 基建，监测层复用它的边际成本低。沉没成本在这里是资产，不是负担——**倾向自研（至少国内引擎）**。 |
| **D3.1** | 主笔模型选型 | **代码层零成本切换**。已走 LiteLLM 统一网关，换模型/加多模型路由是配置活，不是架构活。可放心按内容质量+成本拍。 |
| **D6.1** | 数据仓库选型（Postgres+TimescaleDB） | **与现状 MySQL only 冲突**，是真正影响代码的决策。需在 L1 启动前决定（见 §6）。 |
| **D4.1–D4.3** | 一期平台数 / 账号池规模 | **驱动协议让平台扩展是线性有界的**，每平台是独立工作量，可灵活增减，架构不锁死。 |
| **D1 / D5.3** | App 归因 SDK / 客户端配合 | **纯外部依赖，无代码杠杆**。是 geo-full 自己标的阻塞项，本项目推不动，需业务侧推进。 |

---

## 8. 建议的下一步

按「低风险先行、关键路径压火力」排序：

1. **本周（卫生债，低风险）**：重写 README、给 openai 依赖加澄清注释、清理 `require_local_token` 死代码。零风险、立竿见影。
2. **数据栈拍板（D6.1）**：决定 MySQL 扩展 vs 引入时序库。这一步 gate 住监测层和决策中枢，**越早定越好**。
3. **修两个高危债**：plain_text 改存干净纯文本；规划 worker 多实例化（先解账号登录处理器的多进程安全）。
4. **节点接口最小提案（spike）**：写一份「统一节点契约」草案，论证驱动注册表 + LangGraph 如何收敛到同一抽象——这是方案三的第一块砖，低风险纯设计。
5. **监测层 MVP（最长杆子）**：选单引擎（豆包，Web 自动化）打通「扫描 → 测点入库 → 提及率曲线」，复用现有浏览器基建。它一通，L1 露出归因几乎白送，闭环就看见雏形。

> 第 1、4、5 步可并行启动；第 2 步是其余几步的前置 gate，建议立即推动。

---

## 附录 · 本报告的代码依据（关键文件）

- 应用装配 / 模块化单体：[server/app/main.py](../server/app/main.py)
- 发布调度引擎（四级并发 / 状态机 / 乐观锁）：[server/app/modules/tasks/executor.py](../server/app/modules/tasks/executor.py)
- 发布运行器 / Playwright 会话：[server/app/modules/tasks/runner.py](../server/app/modules/tasks/runner.py)
- 平台适配器抽象：[server/app/modules/tasks/drivers/base.py](../server/app/modules/tasks/drivers/base.py)、[drivers/toutiao.py](../server/app/modules/tasks/drivers/toutiao.py)
- 生产 worker（单实例 / 账号登录子线程）：[server/worker/executor.py](../server/worker/executor.py)
- AI 生文管道（LangGraph 三节点）：[server/app/modules/ai_generation/pipeline.py](../server/app/modules/ai_generation/pipeline.py)
- 文章三份存储 / 同步：[server/app/modules/articles/models.py](../server/app/modules/articles/models.py)、[articles/service.py](../server/app/modules/articles/service.py)
- 图片库 / 问题库 / 飞书同步：[image_library/](../server/app/modules/image_library/)、[ai_generation/question_bank.py](../server/app/modules/ai_generation/question_bank.py)、[shared/feishu_bitable.py](../server/app/shared/feishu_bitable.py)
- 配置面：[server/app/core/config.py](../server/app/core/config.py)
- 部署编排：[docker-compose.yml](../docker-compose.yml)
