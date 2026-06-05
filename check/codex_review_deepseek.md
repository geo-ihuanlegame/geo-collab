# Codex 对 DeepSeek 审核报告的复核

复核对象：`check/DeepSeek_review.md`

复核日期：2026-06-05

## 总评

DeepSeek 报告有价值，但优先级失真明显，把不少风格/技术债标成了 P0。真正应该优先处理的是它没抓全的生产边界：审核绕过、定时漏跑、run 卡死、资源耗尽、迁移兼容。

验证已跑，均为非写入操作：

- `ruff check ...`：通过
- `pnpm --filter @geo/web typecheck`：通过
- `python -m pytest server/tests/test_agent_management.py -q -m "not mysql"`：9 passed

未改代码。

## 逐条裁决

| # | DeepSeek 原优先级 | Codex 结论 | 建议优先级 | 裁决 |
|---|---:|---|---:|---|
| 1 `type` 遮蔽内建 | P0 | 部分成立 | P3 | 可读性问题；ruff 当前不报，不能算 P0。 |
| 2 run `done` 后降级 | P0 | 成立 | P2 | UI 会状态回滚，但不是数据破坏。 |
| 3 rollback 后复用 Session | P0 | 部分成立 | P2 | 可改进，但不是首要事故源。 |
| 4 `scheme_router` factory None 卡 pending | P0 | 成立 | P1 | 生产正常 create_app 会注入，但测试/异常装配会 stuck。 |
| 5 `publish_draft` 长锁 | P1 | 成立 | P2 | 锁竞争真实存在，大节点数才明显。 |
| 6 scheduler timezone | P1 | 证据不足 | P3 | DB session 已设 UTC；最多补注释。 |
| 7 executor 额外 session | P1 | 成立 | P3 | 性能洁癖，非高优先级。 |
| 8 scheduler 全局变量无锁 | P1 | 部分成立 | P3 | 进程内启动一次，理论竞态。 |
| 9 `account_ids or []` | P1 | 不成立 | P3 | 当前逻辑正确捕获空列表，未来风险不能算 bug。 |
| 10 `article_group_source` 不用 inputs | P1 | 部分成立 | P3 | source 节点设计上可 config-only；如要动态 source 再改。 |
| 11 `onRun` 错误路径 running | P1 | 不成立 | P3 | `setRunStatus("running")` 在 `await startRun` 后，startRun 失败不会置 running；最多残留旧状态。 |
| 12 recovery 重复 | P2 | 成立 | P3 | 技术债。 |
| 13 scheduler 管理重复 | P2 | 成立 | P3 | 技术债。 |
| 14 预留 endpoint 死代码 | P2 | 部分成立 | P3 | 不建议删；补测试/文档即可。 |
| 15 临时封面 bucket | P2 | 成立 | P2 | 硬编码生产临时代码，且 `ORDER BY RAND()`。 |
| 16 `_next_version_no` 全量扫描 | P2 | 成立 | P2 | 应改 DB `max()`。 |
| 17 `max_workers=4` 硬编码 | P2 | 成立 | P2 | 单项不是事故，但和无全局闸叠加危险。 |
| 18 factory fallback 不一致 | P2 | 成立 | P1 | 与 #4 合并处理。 |
| 19 测试 helper 重复 | P2 | 成立 | P3 | 测试维护债。 |
| 20 scheme task 三次 session | P2 | 部分成立 | P3 | 有性能成本，但避免 LLM 调用持 DB session，是合理取舍。 |
| 21 依赖风险 | P2 | 部分成立 | P2 | `langgraph/markdown/python-frontmatter/minio` 未 pin 成立；`openai==2.38.0` 说法不严谨。 |
| 22 TS `as` 绕类型 | P3 | 成立 | P3 | 小问题。 |
| 23 分钟精度 | P3 | 基本不成立 | P3 | claim 防重；主要是多扫描，不是重复触发。 |
| 24 `window_start == end` 被拒 | P3 | 不成立 | P3 | 零窗口没有明确业务价值，拒绝合理。 |
| 25 `contains` 字符串匹配 | P3 | 部分成立 | P3 | 当前就是字符串条件；若支持数值语义再改。 |
| 26 import 位置不一致 | P3 | 成立 | P3 | 风格问题。 |

## 重点校正

DeepSeek 的 P0 里，只有 #4 值得接近高优先级，但也不是绝对生产 P0，因为正常 `create_app()` 会注入 `bg_session_factory`。#1、#2、#3 都不该标 P0。尤其 #1，`type` 遮蔽内建函数是代码品味问题，不是生产阻断；我用 `ruff check` 验证当前规则不会报。

#11 是明显误读。`PipelineEditor.onRun()` 里 `setRunStatus("running")` 发生在 `await startRun(pipelineId)` 成功之后，所以 startRun 抛错不会把状态设成 running。最多是上一次 run 的状态没有被清掉，这可以作为 P3 UI polish，不是 P1。

#6 timezone 也偏重。`server/app/db/session.py` 已通过 `connect_args={"init_command": "SET SESSION time_zone='+00:00'"}` 固定 MySQL session UTC；当前 naive UTC 的比较没有 DeepSeek 说的直接偏移风险。

## DeepSeek 遗漏项

### 1. P0：删除运行中的 pipeline 会绕过审核

`service.py` 直接删 `PipelineRun`。后台线程继续生成文章后，`executor.py` 取不到 run/user，`mark_pending_and_group()` 不执行。结果是 AI 文章默认 `approved`，未送审。

### 2. P0：scheduler claim 先 commit，create_run 后失败会漏跑

`scheduler.py` 先更新 `last_scheduled_run_at` 并提交，再创建 run。`create_run()` 失败时 slot 已吞掉。

### 3. P1：`run_pipeline()` 顶层异常不落 failed

`should_skip()` / `apply_input_mapping()` 在节点 try 外。非法 `flow_meta` 可能让线程崩溃，路由只打日志，不把 run 置 failed。

### 4. P1：snapshot 没结构校验

`DraftSave.snapshot` 是裸 `dict`，`snapshot_to_node_dicts()` 直接信任 JSON。没有校验 node type、index、dependsOnIndex、inputMapping、condition、count 上限。

### 5. P1：无全局 pipeline 并发闸

API run、scheduler run、节点内 `ThreadPoolExecutor(max_workers=4)` 叠加，DB 池只有 5+10。这个比“max_workers 硬编码”本身严重得多。

### 6. P1：0040 tags 迁移不兼容存量

`tags` 新列 nullable 且无 backfill；`PipelineRead.tags` 要 list。已有 pipeline 升级后可能列表/详情 500。

### 7. P1：PipelineRun 不冻结版本/snapshot

run 创建后后台线程读取 live nodes；如果期间 publish 新版本，本次 run 执行内容不可追溯。

### 8. P2：PATCH 无法清空时间窗

前端发送 `window_start: null`，服务端 `fields[k] is not None` 直接忽略，设置过后无法清空。

### 9. P2：智能体“编辑流程”忽略 id

`AgentManagementWorkspace` 传了 `p.id`，但 `App.tsx` 里 `onEditFlow={() => handleNavClick("pipelines")}` 丢掉 id，跳到工作流页可能选中第一个，不是目标 pipeline。

### 10. P2：scheduler 查询缺索引/缺 owner

按 `is_enabled + schedule_kind` 周期扫描，但迁移没建对应索引；多 API 实例部署时也没有 scheduler owner/lease。

## 最终修复顺序

### P0

1. 删除 pipeline 前拒绝存在 `pending/running` run，返回 409。
2. scheduler 的 `last_scheduled_run_at` claim 和 `create_run()` 放同一事务，失败 rollback。

### P1

3. `run_pipeline()` 顶层兜底，任何线程异常都写 `failed + error_message + completed_at`。
4. 给 pipeline snapshot 增加 Pydantic 校验，发布前校验 node graph、flow_meta、config。
5. 修 0040 `tags` backfill 和读出兜底。
6. 加 pipeline 全局并发闸，并限制 `ai_generate.count`。
7. `PipelineRun` 冻结 version/snapshot。
8. 统一 `bg_session_factory is None` 行为，scheme run 也要置 failed 或返回 503。

### P2

9. 修 PATCH 显式 null 清空。
10. 修智能体“编辑流程”选中目标 pipeline。
11. `_next_version_no()` 改 DB `max()`。
12. 临时封面 bucket 配置化或删除。
13. scheduler 建索引，明确单 owner/leader 策略。

## 结论

DeepSeek 报告适合作为“问题雷达”，但不能按它的 P0/P1 直接排期。真正先修的是它遗漏的审核绕过和调度漏跑，其次是 run 卡死、并发闸、迁移兼容。
