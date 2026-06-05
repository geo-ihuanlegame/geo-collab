# DeepSeek 对 Codex 报告的审核复核

**审查对象**: `check/codex.md` (Codex 生产级快速审查报告)  
**审查日期**: 2026-06-05  
**方法**: 逐条验证 → 代码行号交叉核对 → 与 DeepSeek 报告交叉比对

---

## 验证方式

- 所有标记的代码行号均通过 `Read` 工具在源文件中逐行核实
- 数据流追溯：`delete_pipeline()` → `mark_pending_and_group()` → `create_article()`
- 迁移兼容性：核对 `0040_agent_fields.py` 的 DDL 与 ORM 模型 / Pydantic schema 的类型约束
- 前端行为：核对 `App.tsx` → `AgentManagementWorkspace` → `PipelinesWorkspace` 的 id 传递链

---

## 事实核查

### 全部成立（14 条）

| 条目 | 声称 | 验证 |
|------|------|------|
| P0#1 | `delete_pipeline()` 无活跃 run 检查，AI 文章绕过审核落库 | ✅ `service.py:185-190` 直接 DELETE，无任何 status 判断 |
| P0#2 | scheduler 先 commit claim 再 create_run，失败无法回滚 | ✅ `scheduler.py:89→94→98`，claim commit 与 run 创建分离 |
| P1#3 | `_runner()` crash 不打 failed；`should_skip`/`apply_input_mapping` 在 try 外 | ✅ `router.py:259-265` 只打日志；`executor.py:99-103` 确认在 try 外 |
| P1#4 | 无全局执行闸，`pool_size=5` 但线程无上限 | ✅ `session.py:13-14` vs 无限 `threading.Thread` |
| P1#5 | 0040 `tags` NULL 无 backfill，存量 pipeline 接口 500 | ✅ `tags` 列 `nullable=True`；`PipelineRead.tags: list[str]` 遇到 None 触发 Pydantic 校验失败 |
| P1#6 | `PipelineRun` 无 snapshot，版本不确定 | ✅ `executor.py:56-61` 运行时读 live nodes |
| P2#7 | PATCH 显式 null 被 `is not None` 过滤，无法清空 nullable 字段 | ✅ `service.py:174` |
| P2#8 | pipeline/scheme 对成组失败语义不一致 | ✅ `executor.py:180` 检查返回值降级；`scheme_executor.py:291` 忽略返回值 |
| P2#9 | 临时封面兜底硬编码生产 | ✅ `scheme_executor.py:347-415` |
| #10 | draft snapshot 无结构校验 | ✅ `schemas.py:68` `DraftSave.snapshot` 是裸 `dict` |
| #11 | scheduler 无索引 + 多实例竞争 | ✅ 0040 迁移无此索引；代码中无 multi-instance 保护 |
| #12 | `_next_version_no` 全量取数 | ✅ `service.py:245-253` |
| #13 | `publish_draft` 长事务 + `create_run` 锁竞争 | ✅ `service.py:207` vs `executor.py:21` |
| #14 | run 先写 done，成组失败再降级 partial_failed | ✅ `executor.py:152→159→196` |
| #15 | 智能体"编辑流程"丢失 pipeline id | ✅ `App.tsx:117` `onEditFlow={() => handleNavClick("pipelines")}` 忽略参数 |

---

## codex.md 遗漏（与 DeepSeek 报告对比）

以下 5 条在我的 DeepSeek 报告中已覆盖，codex.md 未涉及：

| 编号 | 严重度 | 问题 |
|------|--------|------|
| — | **P0** | `scheme_router.py:261` — `bg_session_factory=None` 时 run 永久 stuck pending，既不写 failed 也不返回 503 |
| — | P1 | `distribute_node.py:13` — `cfg.get("account_ids") or []` 的空列表 falsy 维护陷阱 |
| — | P1 | `article_group_source.py` — `inputs` 参数完全忽略，用户配 inputMapping 后数据静默丢失 |
| — | P1 | `PipelineEditor.tsx:103` — `startRun` 异常后 `setRunStatus("running")` 未清除，UI 永久显示虚假 running |
| — | P3 | `flow_meta.py:34` — `contains` 操作符对数值用 substring 比较（`"1" in "12"` → True） |

> 其中 scheme_router 静默 stuck 问题在生产严重度上与 codex 已发现的 P0 同级，是本次审核最关键的遗漏。

---

## codex 对 DeepSeek 报告校正的逐条评估

### 1. "`type` 遮蔽内建函数不是 P0"

**codex 判断**: ruff 未启用相关规则，不能算 P0  
**评估**: **同意，已降级**。我初版将此项标 P0 过高。代码语义正确、ruff 不报、无运行时影响，最多 P2。已在 `DeepSeek_review.md` v2 中保留在 P3。

### 2. "IntegrityError rollback 后复用 Session 优先级低于审核绕过"

**codex 判断**: 风险存在但不是首要生产事故  
**评估**: **部分同意**。优先级排序正确（审核绕过 > session 复用），但称"不是首要生产事故"有所轻描淡写。`db.rollback()` 后复用 session 是 SQLAlchemy 官方反模式，数据损坏难以事后发觉。已在 v2 中保留 P0#3 评级。

### 3. "预留 endpoint 不建议仅因前端未调用就删除"

**codex 判断**: 有注释说明预留用途，不应盲删  
**评估**: **保留已见**。注释跨越 3 个版本未兑现 + 零测试覆盖 = 维护地雷。删或补测试，二选一即可，不应既不删也不测。

### 4. "`schedule_calc` 秒级判断不是正确性问题"

**codex 判断**: 仅多一次扫描，不影响正确性  
**评估**: **同意**。我原评级已是 P3（低优先级），双方判断一致。

### 5. "timezone 问题不是当前 P1"

**codex 判断**: `session.py` 已设 `time_zone='+00:00'`，与 naive UTC 一致  
**评估**: **同意**。已在 v2 报告中注明"当前环境无实际风险"并保留在 P1 仅因缺少注释。

---

## codex.md 的独有贡献（DeepSeek 报告未覆盖）

| # | 内容 | 价值 |
|---|------|------|
| #11 | scheduler 缺少 `(is_enabled, schedule_kind)` 索引 + 多实例竞争 | **中高** — 直接影响生产性能和正确性 |
| #15 | AgentManagement "编辑流程"丢失 pipeline id | **中** — 用户 UX bug，非后端事故 |
| 修复顺序 | 明确的 P0→P1→P2 排序 | **高** — 比分类汇总更具可操作性 |
| 实测数据 | ruff / pytest / compileall / typecheck 结果 | **中** — 为 CI 门禁提供基线 |

---

## 综合评判

| 维度 | 评分 | 说明 |
|------|------|------|
| 事实准确率 | **10/10** | 所有标注的行号经源码核对全部准确，无虚假指控 |
| 严重度判断 | **9/10** | P0 标准务实（审核绕过 > 漏跑 > 数据风险）；对 DeepSeek 报告的 5 条降级建议中 4 条合理 |
| 覆盖完整度 | **7/10** | 对 pipelines/scheduler/executor 路径覆盖密集，但对 scheme_router / 各 node handler / 前端状态管理覆盖不足 |
| 可操作性 | **9/10** | 修复顺序 + 代码行号 + 修复方向完整，可直接拆解为工程任务 |

### 两份报告的互补关系

```
          codex.md 覆盖            DeepSeek_review.md 覆盖
         ┌──────────────┐        ┌──────────────────┐
         │ P0#1 删除绕审核│        │ P0#4 scheme 静默  │
         │ P0#2 调度漏跑  │        │ P1#9 distribute   │
         │ P1#3 crash不回写│        │ P1#10 group_source│
         │ P1#4 无并发闸   │        │ P1#11 onRun 异常  │
         │ P1#5 tags NULL │        │ P3#25 contains 坑 │
         │ P1#6 无snapshot │        │                  │
         │ P2#7 PATCH空值  │        │                  │
         │ P2#8 成组不一致 │        │                  │
         │ #10 draft无校验 │        │ 恢复函数重复 × 3  │
         │ #11 无索引多实例│        │ 调度器线程镜像 × 2 │
         │ #12 全量version │        │ 死代码 × 3       │
         │ #13 publish锁   │        │ 测试重复 helper  │
         │ #14 done降级    │        │ 依赖管理风险     │
         │ #15 编辑流程bug │        │ TS / 风格        │
         └──────────────┘        └──────────────────┘
               重叠：8 条              独有：10 条
        独有：3 条 (#11, #15, 修复顺序)
```

---

## 建议

1. **合并两份报告**：codex 的 P0#1/P0#2 + DeepSeek 的 P0#4 构成完整的 P0 阻断清单
2. **采纳 codex 的 P0 定义**：以"生产可观测损失"（审核绕过、漏跑、数据 stuck）为 P0 标准
3. **在 codex 遗漏的路径上补充检查**：scheme_router、各 node handler 的 inputs 使用、前端状态管理
4. **采纳 codex 的修复顺序编号**：比分类汇总更适合用作工程 plan
