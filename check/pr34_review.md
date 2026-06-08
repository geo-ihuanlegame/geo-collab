# PR #34「自动分发已审核内容」代码审查报告

> 审查对象：`feat/auto-distribute`（已合并为 main 的 #34，merge `0fa3e56`）。
> 审查方式：多角度并行静态审查 + 逐条源码核实。**该 PR 通过了 CI 全部门禁并已合并** —— 下列问题都是绿灯之后仍然存在的，印证「CI 绿 ≠ 代码正确」。
> 与 [bug_fix_report.md](bug_fix_report.md) 第七节 Bug A/B 同源：又一支「未经专门评审、AI 辅助快速生成」的特性分支，再次出现「假成功 / 静默失败」一类问题。

## 一、总览

| 严重度 | 数量 | 处置 |
|---|---|---|
| 🔴 确认的正确性问题 | 3 | 本修复 PR 全部修掉（含自动化测试） |
| 🟠 需产品定夺（机制已确认） | 3 | 输出决策清单，暂不改 |
| 🟡 清理项（不崩但应改） | 6 | 记录，择期 |
| ✅ 复核后排除/确认无碍 | 4 | 见末节 |

涉及文件：`pipelines/nodes/{approved_content_source,distribute_node,article_group_source}.py`、`pipelines/executor.py`、`pipelines/router.py`、`tasks/service.py`、`web/.../PipelineEditor.tsx`、`tests/test_auto_distribute.py`。

---

## 二、🔴 确认的正确性问题（本 PR 修复）

### Bug 1 ——「已审核分组源 → 分发」默认接线下，空分组从「报错」变成「静默完成」

- **现象**：`article_group_source` 节点同时输出 `group_id` + `article_ids`（`article_group_source.py:36`）；main 上 #31 又把「无 inputMapping = 全透传」设为默认。于是 `distribute` 的 `if article_ids is not None`（`distribute_node.py:21`）对这种标准接线**永远命中**，`elif group_id` 路径成了死代码。
- **后果**：空分组时 source 返回 `article_ids=[]` → `distribute` 直接 `return {"skipped": "无可分发内容"}`、run 状态 `done`。而旧的 group 路径在 `service.py:594-595` 会抛 `ValidationError("Article group has no articles")` 让 run **失败**。一个本该报错的空分组现在被静默报告「完成」—— 典型「假成功」。非空时副作用：`task_type` 由 `group_round_robin` 变 `article_round_robin`，`group_id` 关联丢失。
- **为什么 CI 没抓到**：测试要么直接构造 `inputs={"article_ids":[...]}`，要么写死 `inputMapping`（`test_auto_distribute.py:188/203/241`），从未跑真实的「源节点 → distribute 默认透传」路径，也没覆盖「经 distribute 的空分组」。
- **修复**：`distribute` 改为**先判 `group_id`、再判 `article_ids`**。有 `group_id`（`article_group_source`）→ 走 `group_round_robin`，保留分组语义与空分组报错；无 `group_id`（`approved_content_source`）→ 走 `article_ids`，空列表跳过（无新内容时跳过是正确的）。补两个测试：经 distribute 的空分组应报错、非空分组应建 `group_round_robin` 任务且 `group_id` 不丢。

### Bug 2 —— 去重子查询漏过滤 `status` 与 `is_deleted`，与全仓库既有「已分发」定义背离

- **现象**：`approved_content_source.py:28` 的 `Article.id.notin_(select(PublishRecord.article_id).distinct())` 不过滤发布状态、不过滤软删。而仓库既有的「已分发」判定（`articles/router.py:169-170`）明确是 `status == "succeeded" AND is_deleted == False`。
- **后果**：(a) 只**失败/取消**过一次的文章被永久排除，自动分发再不会重试它 —— 内容静默消失；(b) **软删**过的发布记录也永久挡住对应文章。
- **修复**：去重子查询加 `PublishRecord.status == "succeeded"` 和 `PublishRecord.is_deleted == False`，与既有定义对齐（并去掉 `NOT IN` 里冗余的 `.distinct()`）。补测试：失败记录不应排除文章、软删记录不应排除文章、成功记录仍排除。

### Bug 3 —— 前端勾选框默认态与后端默认值相反，存在把去重悄悄关掉的风险

- **现象**：`PipelineEditor.tsx:281` 的 `checked={!!sel.config[f.key]}`，在配置缺 `exclude_distributed` 键时渲染为**未勾选**；而后端默认 **True**（`approved_content_source.py:15`）。界面显示与实际行为相反。
- **后果**：用户按界面所见去操作并保存为 `false` 时，去重被关掉 → 同一批内容每天重复分发。
- **修复**：`config_schema` 字段支持 `default`，给 `exclude_distributed` 标 `default: true`；前端在配置缺键时按 `default` 渲染，使显示与生效行为一致（前端无测试框架，本项靠 typecheck + 人工核对，已在 PR 说明中标注）。

---

## 三、🟠 需产品定夺（机制已确认，是否算缺陷取决于设计意图）

| # | 位置 | 机制 | 待定问题 |
|---|---|---|---|
| 4 | `approved_content_source.py:25-26` + `service.py:119` | owner 为 admin 时 source 不加 `user_id` 过滤、`create_task(role="admin")` 又把 owner 校验置空 | admin 的定时分发会把**所有用户**的已审内容轮播到 admin 的账号 —— 特权还是越权？ |
| 5 | `distribute_node.py:50` | `create_task` 支持 `client_request_id` 幂等（`service.py:109-117`）但这里没传；去重靠「记录是否存在」，而记录在建任务之后才有 | 调度重试 / 手动+定时撞车会重复建任务，需不需要幂等键？ |
| 6 | `approved_content_source.py:27-28` | 去重粒度是「文章级全局」，但 round-robin 每篇只发一个账号 | 一篇发过账号 A 就永远轮不到 B；多账号扇出是否本意？ |

---

## 四、🟡 清理项（不崩，择期）

1. `approved_content_source.py:21-30` 重造了 articles 服务已有的「已审列表 + 已分发判定」；复用现成查询可顺带根治 Bug 2 的根因。
2. `service.py:559-573` `article_round_robin` 第三次重写 owner/存在性校验（single、group 各一份），应抽 `_validate_articles_owned()`。
3. `approved_content_source.py:28-29` `NOT IN (SELECT DISTINCT…)` 反连接 + `updated_at` 无索引 filesort，每次定时全表排序；改 `NOT EXISTS`/`LEFT JOIN` 并加索引（设计文档已提）。
4. `distribute_node.py:56` 只输出 `{task_id}`，下游节点拿不到 `article_ids`/数量。
5. `router.py` node `config_schema` 三处手动同步（router 声明 / 节点 `cfg.get` / 前端 switch），键名漂移无人拦。
6. `approved_content_source.py:28` `NOT IN` 里的 `.distinct()` 冗余（本 PR 顺手去掉）。

---

## 五、✅ 复核后排除 / 确认无碍

- 迁移 `0042` 与模型一致（`ck_publish_tasks_task_type` 含 `article_round_robin`），无模型/迁移漂移。
- 两个新节点都已注册、executor 能解析，无「注册了没处理」的坑。
- 两个新节点各自建/关自己的 session，符合「session 非线程安全」规则。
- `limit` 清空 → `Number("")=0` → 后端 `0 or 20` 兜住，不崩。

---

## 六、流程结论

同一类「假成功 / 静默失败」问题在 #28、#34 两支不同的「大体量、AI 辅助、未经专门评审」分支上反复出现，且都能通过 CI。这印证 [bug_fix_report.md](bug_fix_report.md) 第九节的建议：把这类提交纳入**先评审 + 补测试再合并**的流程，并对人读不过来的大 diff 用自动化语义审查（`/code-review`，与 CI 状态无关）补盲区。
