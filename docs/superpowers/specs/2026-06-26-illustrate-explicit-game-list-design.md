# 配图：显式游戏清单驱动落图（解耦"识别"与"落图"）

- 日期：2026-06-26
- 状态：设计待评审（已过一轮中立 code-reviewer 审查并据此修订，见文末「评审修订记录」）
- 范围：`server/app/modules/articles/ai_format.py`、`ai_illustrate_svc.py`、`router.py`（MCP 端点）、`pipelines/nodes/ai_illustrate.py`
- 相关记忆：`project_illustrate_undercount_rootcause`、`project_qianfan_qps_limit`、`project_ai_illustrate_web_fallback`

## 1. 背景与问题（已验证）

当前 AI 配图链路在**配图时**用弱模型（`GEO_AI_FORMAT_MODEL`，deepseek-flash）现扫文章、自产 `image_positions` 决定"配哪些游戏、配在哪"。实测三个问题：

1. **根因 A — 弱模型 under-recognition**：`image_positions` 完全由该模型自行识别（`ai_format.py:247/709/1281`）。#1023 有 10 个清晰 h2 heading（`游戏一、《餐厅养成记》`…`游戏十、《剑与远征》`），却只配了 1 张；漏掉的 9 个是原神/明日方舟等**顶流**，非小众——是识别不稳，非图库缺图。
2. **根因 B — `partial_images` 统计盲区**：`requested` 来自 AI 自报位置数（`ai_format.py:767` `len(requested_labels)`，且只在"能定位到栏目"时计入，:718），`missed = requested - inserted`（:769）。AI 只点 1 个时 requested=inserted=1、missed=0 → **不报警**。后端从不数文章真实游戏数。
3. **附带 bug — 回写有损渲染**：`_derive_html_and_text`（`ai_format.py:444`）只渲染 `heading`/`paragraph`，丢掉 `bulletList/orderedList/listItem/image`；`_node_html`（:425）只遍历直接 `text` 子节点、只保留 `bold` 一种 mark、不递归。每次配图回写（`:1093-1096`、`:1429-1432`）无条件用它覆写 `content_html`/`plain_text`。

   **发布影响（已核实，措辞修订）**：正常发布走 content_json —— `parser.py:parse_body_segments` 用 `_append_segments` **递归**抽取片段，正确处理 list/image（`wechat_mp.py:118` 从 content_json 重渲染、`toutiao_inpage.py:301` 从 body_segments 构造）。**但存在一条降级路径**：`parser.py:249`，当 content_json 抽不出任何片段时，回退 `article.plain_text or strip_tags(content_html)` 当单段正文发布。所以"发布不受影响"**不绝对**——降级路径会吃到残缺的 plain_text/content_html。综合：此 bug 主要影响 content_html/plain_text 的下游消费者（plain_text→FTS 检索、预览/导出）+ 这条窄降级发布路径；Fix-3 修复后这些路径亦完整。

## 2. 目标 / 非目标

**目标（本设计的边界）**
- G1：**给定上游传下来的显式游戏清单，确定性、准确地把图配上**（名字 → 文章内 heading → 落图）。
- G2：**准确计数**，修掉 `partial_images` 盲区（`expected` 来自清单，不再信 AI 自报）。
- G3：修 `_derive_html_and_text`/`_node_html` 有损渲染（Fix-3）。

**非目标（明确划出去）**
- 游戏识别（"文章里有哪些游戏"）：由**上游强模型分支**负责，可配置、后续可加联网验真节点。本设计**不碰识别**，只做"拿到清单后落图"。
- 标题提升（段落→heading）：显式清单路径**不做**（见 §5.2 决策）。

## 3. 接缝契约（与上游识别分支对接）

复用并扩展**现有内部结构** `image_positions`，不发明新协议：

```jsonc
// 上游识别分支产出、传到配图这一步
[
  {"game": "原神"},                       // game 名 = 权威锚点（必填）
  {"game": "梦幻家园", "category_id": 12}, // category_id 可选：已知栏目直接用
  {"game": "阴阳师", "index": 14}          // index 可选：仅作提示/校验
]
```

- **`game`（必填）= 权威锚点**：本组件用它在**配图当时的 content_json** 里匹配 heading、**现算**绝对 index 再落图。
- **`index`（可选）从必填降级为提示**。理由（已验证 `ai_format.py:129/188`）：现有 index 是 `content_json["content"]` 的**绝对下标**，有空洞（含 bulletList/image 占位）、绑死内容快照、无语义；让上游隔分支产出它极易错位。把"数 index"这件易错事收进本组件、只在落图当刻对着那份 content_json 做一次。
- **`category_id`（可选）**：上游已知栏目时直接给，省一次解析。
- **计数语义（明确）**：**清单一项 game = 至多配一张图**。同一游戏即便在文中有多个 heading，也只配首个（见 §5.1）；expected 按清单条目数算（见 §5.3）。

## 4. 架构：解耦"决定位置"与"落图"

```
上游(强模型, 别的分支)            本设计(配图组件)
  识别游戏 → game_list  ──────▶  清单→heading→落图 + 计数 + 回写
```

- **清单存在** → 走**确定性落图路径，不调 ai_format LLM**。副作用红利：这条路没有慢 LLM 调用，**顺带消除 30s MCP 超时 vs 120s ai_format 的错配**（`project_illustrate_undercount_rootcause` 根因 C）。
- **清单缺省**（`None`）→ **回退现有模型流程**（`run_ai_format` 原样），完全向后兼容，不影响别的分支 / 非清单文章。

## 5. 组件设计

### 5.1 清单 → 合成 `image_positions` resolver（新增）

> ⚠️ 实现要点（评审修订）：现有落图后端（`_web_fallback_decide` / `_maybe_insert_images`，`ai_format.py:1281`/`:709`）**以 `parsed: dict` 为中心**，内部调 `_parse_image_positions(parsed.get("image_positions", []))`。该 `parsed` 原本是 LLM 输出。无 LLM 的新路径**必须手工合成一个等价 `parsed`**（`{"image_positions": [...]}`）再喂进去——这是一个**转换/胶水层**，不是零改动复用。

对每个清单项 `{game, category_id?, index?}`：
1. **定位 heading**：在 content_json 顶层 `heading` 节点里找文本含 `game` 的。归一化：去 `《》`、首尾空白、`游戏N、`/`游戏\d+、` 前缀后做 contains（regex 在实现时定稿并测各变体：`游戏一、`/`游戏1、`/带空格）。
   - 唯一命中 → 取该 heading 的绝对 index。
   - **多 heading 命中同一 game** → 取**首个**；若给了 `index` 提示则用提示消歧。
   - 未命中 → 若给了 `index` 提示则退用之；否则记 **missed(reason=heading_not_found)**，不产位置。
2. **冲突去重（评审修订 #5）**：多个清单项解析到**同一绝对 index**（如一个 heading 文本含多个游戏名）→ **按 index 去重，保留先到的一项**（一节点一图），其余记 missed(reason=index_conflict)，避免 `insert_images_at_positions` 在同位重复插图。
3. 产出合成 `parsed = {"image_positions": [{"index", "game", "category_id"?}...]}`，喂给现有 decide/落图管道。

### 5.2 落图（复用现有后端 + 锁，评审修订 #2/#3/#6）

- 复用现有 `_web_fallback_decide` → 下载 → `_web_fallback_collect_and_write_back` 全链：`category_id` 给了直接用；否则 `get_or_create_companion_category(game)` → `pick_image_for_category`；空 → `web_fallback` 联网搜（沿用现有限速/负缓存，`project_qianfan_qps_limit`）。
- **锁与并发安全**：新路径**复用现有 `ai_checking` 锁 + 第二道 `lock_started_at` 指纹校验**——即仍走 `_ai_format_write_back` / `_web_fallback_collect_and_write_back`（二者签名已带 `lock_started_at` 并在回写前校验，见 `ai_format.py:1054/1349`）。**不另写无锁回写**，否则并发配图会互相覆盖（`project_publish_perf_optimization` 同类教训）。
- **`heading_indices` 处理（决策已定）**：显式清单路径**传 `heading_indices=set()`，不做标题提升**。理由：该路径确定性、无 LLM；假定上游/写作产出的 heading 已规整。写回函数照常调用，只是 `heading_indices` 为空集，段落不升级。

### 5.3 计数（修根因 B 盲区）
- `expected = len(清单)`（**权威，非 AI 自报**）。
- `inserted = 实际落图数`。
- `missed = expected - inserted`，附 `missed_games` + 每项 `reason`（`heading_not_found` / `index_conflict` / `no_image` / `web_fallback_empty`）。
- **`missed > 0` 必报 `partial_images` warning（即便 `inserted > 0`）** → 盲区根除，因为 expected 来自显式清单而非模型自报。
- 已知精度边界（非缺陷，由 §3 语义决定）：一游戏多 heading 时只配首个、missed 不计另几个 heading——契约即"一游戏一图"。

## 6. Fix-3：有损 html / plain_text 渲染（评审修订 #4，扩范围）

`_node_html` / `_derive_html_and_text` 当前丢失：所有列表/image 块节点、`hardBreak`、除 `bold` 外的全部 marks。本次**一并修全**：

- `_node_html`：**递归**渲染子节点；marks 支持 `bold/italic/code/underline/strike/link`（link 渲染 `<a href>`）；`hardBreak` → `<br>`。
- `_derive_html_and_text`：处理 `bulletList`/`orderedList`/`listItem`（含内嵌 `paragraph`）/`image`/`blockquote`/`codeBlock`，不再只认 `heading`/`paragraph`。未知节点类型保底"尽量取文本、不静默吞整块"。
- 措辞修订：不再宣称"恢复完整"，而是**枚举支持的节点/marks**；超出枚举的新节点类型按保底处理并在测试里固化当前覆盖面。

## 7. 集成点（清单从哪进）

- **service**：`IllustrateOptions` 加 `game_list: list[dict] | None = None`（`None` = 回退现有模型路径）。`illustrate_one` 据此分叉：有清单走 §5 确定性路径、缺省走 `run_ai_format` 现状。
- **MCP tool + 端点**：`ai_illustrate_article` / `POST /api/articles/{id}/ai-illustrate` 加可选参数（暂名 `game_positions`），透传到 `IllustrateOptions.game_list`。
- **pipeline 节点**：`ai_illustrate.py` 节点 input mapping 可接上游 `game_list`，字段名与识别分支对接时敲定。
- 注：上游识别分支未定型，**本设计先把消费侧 + 契约做扎实**；缺省路径保证现状不破。

### 7.1 web 生产者落地（2026-06-29，本节为后续补记）

MCP writer（SKILL v7）之外，web 端三条生文路（方案运行 / ai_compose / ai_generate）的「产清单」生产者已接上，与 MCP 路根上对齐：

- **生产者（共享）**：`ai_generation/article_writer.py:generate_article_from_prompt` 让强写作模型在正文后追加 ` ```json {"games":[...]}``` ` 哨兵块；新增 `_split_games_block` 剥块取名（取最后一个、需 `games` 键、绝不误吃正文代码块），把 `game_list` 盖进 `Article.metrics["game_positions"]`（盘点 / 推荐文非空才盖；散文 / 无块 → 不盖）。**返回签名保持 `int`**，不影响既有 mock。
- **消费者① 方案运行**：`scheme_executor.py:_auto_format_article` 读 stamp → 非空走 `run_ai_format_from_game_list`（aggressive / max_images=12），否则现状 `run_ai_format`。
- **消费者② pipeline**：`ai_illustrate_svc.py:illustrate_one` 在 `options.game_list is None` 时回退读 `metrics["game_positions"]`（`effective_game_list`），显式参数优先；ai_compose/ai_generate → 下游 `ai_illustrate` 节点据此吃上确定性配图。
- **零回归护栏**：模型不吐 / 吐错 json 块 / `games` 空 → 不盖 → 两消费侧回退现有 `run_ai_format`，行为 == 改动前。
- 不碰 `loop_skills` 模板、不 bump `version.py`、无 DB 迁移（复用 `Article.metrics` JSON 列）。

## 8. 向后兼容与风险

- **缺省 → 现状完全不变**（最大兼容护栏）。
- 显式清单路径**不调 LLM** = 更快 + 无超时；**不做 heading 提升**（决策已定，§5.2）。
- Fix-3 会改变**所有** ai_format 文章的 content_html/plain_text 输出（变完整）——是修复方向；正常发布读 content_json 不受影响，降级路径（`parser.py:249`）与 FTS/预览在修复后更准。
- 落图复用 web_fallback 的限速/负缓存（进程级、单 worker 下有效，见 `project_qianfan_qps_limit`）；新路径并发下共享状态语义与现状一致。

## 9. 测试计划（TDD，先写失败用例）

- **resolver 匹配**：game→heading——带 `《》`、`游戏N、`/`游戏\d+、` 前缀及带空格变体、index 空洞、多 heading 命中同一 game（取首个）、未命中（+index 提示回退）。
- **冲突去重**：一 heading 含多游戏 → 同 index 去重、其余记 `index_conflict`、不在同位插两图。
- **计数**：expected 取自清单；各 `reason` 的 missed；**`partial_images` 在 `inserted>0` 时也触发**（专杀盲区）。
- **锁/并发**：新路径走 `lock_started_at` 指纹校验，模拟并发两次配图不互相覆盖。
- **Fix-3**：list + image + `blockquote`/`codeBlock`/`hardBreak` + 各 marks（italic/link/code/underline/strike）渲染 round-trip（以 #1023 类 bulletList-heavy + image 内容做回归）。
- **集成**：显式清单 → 正确落图 + 准确 warning；缺省 → 回退现有（旧 ai_format 测试仍绿）。

## 10. 待上游分支对齐（接缝细节）

1. `game_list` 通道：MCP 参数 vs pipeline input mapping 字段名。
2. 是否附带 `category_id` / `index` 提示（给了 `index` 与 heading 实算不一致时，**以 heading 实算为准**、index 仅在多义/未命中时兜底——实现时固化此优先级）。

> 注：原"显式清单路径是否保留 heading 提升"已在 §5.2 决策定为**不保留**，从待办移除。

---

## 评审修订记录（2026-06-26，中立 code-reviewer 审查后）

逐条核实并采纳：
- **#1（事实硬伤）**：`parser.py:249` 存在 content_html/plain_text 降级发布路径 → §1.3 措辞改为"发布不绝对不受影响"。
- **#2（阻塞）**：`_web_fallback_decide` 以 `parsed` dict 为中心，无 LLM 时需**合成 parsed** → §5.1 明写转换/胶水层。
- **#3（阻塞）**：写回函数强依赖 `heading_indices` → §5.2 定为传 `set()`、不提升标题。
- **#4（改进）**：`_node_html` 只保留 bold、漏 hardBreak/其他 marks → §6 扩为补全 marks + hardBreak + blockquote/codeBlock + 递归，措辞去掉"完整"。
- **#5（改进）**：同 index 重复插图 → §5.1 加 index 去重 + `index_conflict` reason。
- **#6（改进）**：新路径绕过 run_ai_format 的锁 → §5.2 明写复用 `ai_checking` 锁 + `lock_started_at` 指纹校验。
- 语义澄清：一游戏多 heading=配首个（§3/§5.3），非缺陷。
