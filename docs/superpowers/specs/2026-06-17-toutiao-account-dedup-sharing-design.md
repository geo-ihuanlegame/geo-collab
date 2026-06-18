# 头条账号 creator-ID 查重 + 共享账号 设计稿

- 日期：2026-06-17
- 状态：设计已评审（含 v2 代码评审修订），待写实现计划
- 关联 spike：`E:\1\toutiao_creator_id_probe.py`（登录头条创作者平台后抽取「头条号ID」= `media_id`）

> **v2 修订**：基于一轮针对 v1 的代码评审，修正了「刷新 cookie 走 storage_state 无效」「driver hook 未区分 sync/async」「worker 登录结果无字段携带 creator-ID/canonical」「B 临时行假设不成立」「重新识别风险过大」「使用权限覆盖不全」「缺成员撤销」「回填非闭环」「导入与唯一身份冲突」「排队口径夸大」共 10 点。详见各节与 §12 变更记录。

> **v3 修订（本次实现前评审，权威覆盖以下两点）**：
> 1. **合并逻辑抽出 worker 事务**：B 行的「条件合并（软删 / `merged_into`）+ 加成员 + 审计」从 `_worker_finish_login_session` 内联分支抽成**单一幂等函数 `reconcile_duplicate_into_canonical`**（定义见新 §4a），由 登录去重（§4）/ 回填（§5）/ admin 批量回填 三处共用。worker 决议只做「抽取 X → 解析 canonical → 命中则调该函数 / 未命中则在 B 上 claim X」，把最危险的可变逻辑收进一个可脱浏览器单测、可重试的函数。
> 2. **§7 排队可见状态整体推迟到 §11**：串行化机制本就零新增（共享单 canonical + 单 profile_key 天然串行），首版直接复用、不做任何 usage 可见性。**随之不做**：`GET /api/accounts/usage` 端点、§6 权限矩阵里的 usage 行、§10 的「排队状态」测试、§1 的「可见排队状态」目标、前端排队徽标 + usage 轮询。首版前端只保留：owner/成员标识、管理按钮 gating、成员管理 UI、「身份未知」徽标 + admin 批量回填入口。

## 1. 背景与目标

头条等浏览器登录平台的账号，`Account.platform_user_id` 当前**恒为 NULL**，导致：

1. 同一物理头条账号被不同用户/不同 `account_key` 重复登记成多行，互不感知。
2. 现有全局唯一约束 `uq_accounts_platform_user (platform_id, platform_user_id)` 因 MySQL「多个 NULL 互不冲突」而对浏览器平台**实际不生效**（只对公众号等 API 平台生效）。

本设计把 spike 的「登录后抽取 creator-ID」步骤融入头条登录流程，落地：

- **查重**：以 creator-ID 写入 `platform_user_id`，让全局唯一约束对头条真正生效。
- **多用户共享 + 排队（需求 #4）**：物理账号全局唯一；A 登录持有后，B 不知情登录、查重命中后 B 也能**看到并使用**该账号；多人并发使用按既有锁串行化，并向用户**可见排队状态**（口径见 §7）。
- **历史回填（需求 #1）**：让账号有效性检测流程也抽取 creator-ID，回填存量 NULL 行，并自动合并因此暴露的重复；配 admin 批量回填闭环。
- **重复即刷新（需求 #2，首版部分交付）**：首版查重命中只加成员、**不自动刷新 cookie**；成员后续刷新走「直接登录进 canonical 的 profile_dir」，列为后续（理由见 §4、§11）。

### 已确认的产品决策

| 决策点 | 选择 |
|---|---|
| 共享账号管理权限 | **owner + admin 管理；成员仅可见 + 使用** |
| 历史 NULL 回填 | **本期做，且自动合并重复**（保留发布历史）+ admin 批量入口 |
| 并发使用 UX | **既有锁串行化 + 可见状态**（谁占用 / 排队几个，口径见 §7） |
| ID 抽取范围 | 查重/共享/排队基建**通用**；creator-ID **抽取仅头条**先做，其它平台留 per-driver hook，未实现前保持 NULL（无查重，无回归） |
| 共享账号数据模型 | **方案 A：单 canonical `Account` 行 + 新 `account_members` 表** |
| 重复即刷新（#2） | **首版只加成员、不自动刷新**；刷新走后续「成员直登 canonical profile」 |
| B 行已有历史时的合并 | **按条件：B 干净则软删；B 有历史则标 `merged_into=C`、保留行让待发布记录发完** |

## 2. 数据模型

### 2.1 复用既有字段与约束（不新增 `accounts` 业务列、不改约束）

- `Account.platform_user_id`（`String(200)`, nullable，[models.py:42](../../../server/app/modules/accounts/models.py)）存头条 creator-ID（`media_id`，8–30 位纯数字，正则 `^[1-9]\d{7,29}$`）。
- `uq_accounts_platform_user (platform_id, platform_user_id)`（[models.py:34](../../../server/app/modules/accounts/models.py)，0045 已改全局）原样复用。NULL-allowed → 未抽取的账号不参与约束；写入后约束强制「每个物理账号一行」。

### 2.2 新表 `account_members`

| 列 | 类型 | 说明 |
|---|---|---|
| `account_id` | INT, FK→`accounts.id` ON DELETE CASCADE, 复合 PK | canonical 共享账号 |
| `user_id` | INT, FK→`users.id` ON DELETE CASCADE, 复合 PK | 被授予的成员（不含 owner） |
| `granted_via` | `String(40)` | `login_dedup` / `backfill_merge` / `manual`，溯源 |
| `created_at` | DateTime | |

- 复合主键 `(account_id, user_id)`；owner 仍是 `Account.user_id`，不重复进成员表。
- 账号走软删，故 CASCADE 平时不触发；`delete_account` 中**手动清空**该账号成员行。

### 2.3 `accounts` 新增 `merged_into`（合并 tombstone 指针，评审点 4）

- `merged_into: Mapped[int | None]`，`Integer, FK→accounts.id, nullable, index`。
- 非空表示「这行是某 canonical 的被并入行」：从可用/活跃列表排除，但**不**置 `is_deleted`，使其名下未终态发布记录仍能发完（executor 靠 `is_deleted` 判不可发布，[executor.py:653](../../../server/app/modules/tasks/executor.py)）。
- 用途：①B 有历史时的合并落点；②历史 union（某物理账号全部记录 = `account_id ∈ {C} ∪ {merged_into=C}`）。

### 2.4 `account_login_sessions` 新增结果列（评审点 3）

worker 完成登录后需把查重结果带回 API/前端，现有 `BrowserCheckResult`/会话表无此通道。新增：

- `resolved_account_id: Mapped[int | None]`（worker 写入查重决议后的 canonical id；finish/status 据此返回正确账号）。
- `extracted_platform_user_id: Mapped[str | None]`（抽取到的 creator-ID，便于诊断/审计）。

### 2.5 外键冲突分析（原始问题的正式答复）

**无冲突。** `platform_user_id` 是普通字符串列、非外键；新增的 `account_members`（2 条 FK）、`accounts.merged_into`（自引用 FK）、`account_login_sessions` 两列（非 FK）均为干净增量；`PublishRecord.account_id` 不动（canonical id 稳定）。

### 2.6 Schema / API 增量

`AccountRead` 增：`owner_name`、`member_count`、`can_manage`（= `user_can_manage_account`）、`identity_known`（= 浏览器平台且 `platform_user_id` 非空；供「身份未知」徽标，见 §5/§8）。

### 2.7 迁移

`0047_account_dedup_sharing`（down_revision = `0046`）：建 `account_members`；`accounts` 加 `merged_into`；`account_login_sessions` 加 `resolved_account_id` / `extracted_platform_user_id`。

## 3. creator-ID 抽取（spike 集成，区分 sync/async — 评审点 2）

抽取分两层：

1. **纯解析层** `server/app/modules/tasks/drivers/toutiao_creator_id.py`：移植 spike 的正则/JSON 解析（`media_id` 抽取、文本兜底、DOM label、数字校验），**纯函数、无浏览器即可单测**。
2. **I/O 适配层**：登录 broker 是 **async** Playwright（[login_broker.py:64](../../../server/app/modules/accounts/login_broker.py)、`_read` async），账号检测是 **sync** Playwright（[auth.py:992](../../../server/app/modules/accounts/auth.py)）。二者 API 不同，故 driver 暴露**两个薄适配方法**，都委托同一纯解析层：

```python
# PlatformDriver Protocol（两者都可选；缺省=不抽取=保持 NULL，无查重）
def extract_platform_user_id_sync(self, *, page) -> str | None: ...
async def extract_platform_user_id_async(self, *, page) -> str | None: ...
```

- 头条 driver：导航 `mp.toutiao.com/profile_v4/personal/info` → `page.evaluate` 调 `/mp/agw/creator_center/user_info`（`credentials:'include'`）取 `media_id` → DOM label 兜底。sync/async 两版各自做 I/O，解析复用纯函数。
- **best-effort**：任何异常→记脱敏诊断（spike 已自带）→返回 None→不查重；登录/检测照常成功，`platform_user_id` 不变。头条 DOM/API 漂移不拖垮登录。

## 4. 登录流程的查重决议（在 worker 内 — 评审点 1/3/4）

**为何在 worker 内**：登录 broker 的 live async page 活在 worker 进程；抽取必须在 worker 跑。故查重决议放进 `_worker_finish_login_session`（[auth.py:673](../../../server/app/modules/accounts/auth.py)，worker 侧、有 db + live page），结果经 §2.4 新列回传 API。

`_worker_finish_login_session` 在 `_apply_login_result`（[auth.py:832](../../../server/app/modules/accounts/auth.py)）之后插入：

1. 若 `logged_in` → 在 live page 上跑 `extract_platform_user_id_async` 得 `X`；写 `session.extracted_platform_user_id = X`。
2. **`X` 为空**：B 行保持 NULL；`session.resolved_account_id = B.id`，结束。
3. **`X` 有值** → `SELECT` 活账号（`platform_user_id == X`、`is_deleted == False`、`merged_into IS NULL`，排除 B）：
   - **无既有 canonical**：B 升为 canonical → 写 `B.platform_user_id = X`（B 即 owner）；`resolved_account_id = B.id`。
   - **存在 canonical C**（重复命中）：
     1. **不刷新 cookie**（首版决策 R2）：发布读持久化 profile_dir（[runner.py:346](../../../server/app/modules/tasks/runner.py)）而非 storage_state，跨 profile 拷 cookie 对发布无效；故首版不动 C 的会话，B 的新 profile 丢弃。成员后续刷新走 §11 的「直登 canonical profile」。
     2. **调用 `reconcile_duplicate_into_canonical(db, B, C, granted_via="login_dedup")`**（§4a）：该幂等函数内部完成「加成员（缺则插）+ 条件合并 B 行（无史软删 / 有史 `merged_into=C`）+ 审计」。worker 决议本身**不内联**这段分支逻辑。
     3. `resolved_account_id = C.id`。
4. `session.status = FINISHED`、commit。

**API/前端回传**：`_finish_login_browser_via_worker`（[auth.py:484](../../../server/app/modules/accounts/auth.py)）改为读 `session.resolved_account_id` 返回 canonical；`/status` 同样下发，B 的前端立刻看到共享账号。

**B 自身已带不同身份（登录路径的重新识别）**：若 B 这行**复用自**已有账号（[auth.py:224](../../../server/app/modules/accounts/auth.py) 按 state_path 复用）且其 `platform_user_id` 已有值并 `!= X`（同一浏览器 profile 被登录成了另一个物理账号）→ 套用 §5「重新识别」收紧规则：B 无历史/成员/任务才自动改写为 X，否则阻断、置「身份冲突」标记、记 warning，留 admin 确认。

**关键顺序**：先查重再决定是否写 X；只有「无既有 canonical」分支写 X，否则撞唯一约束。
**并发兜底**：两用户同时首登同一新账号 → 都写 X → 第二个 `commit` 撞 `IntegrityError`；捕获后重查 canonical、转「存在 canonical」分支。复刻 [service.py:229](../../../server/app/modules/accounts/service.py) 既有模式。

## 4a. 共享去重合并函数 `reconcile_duplicate_into_canonical`（v3 重构）

放在 `accounts/service.py`，是 §4 / §5 / admin 批量回填**唯一**的合并落点——把"最危险的可变逻辑"收进一个可脱浏览器单测、幂等可重试的纯 DB 函数。

```python
def reconcile_duplicate_into_canonical(
    db: Session, dup: Account, canonical: Account, *, granted_via: str
) -> None:
    """把 dup 行并入 canonical：加成员 + 条件合并 dup 行 + 审计。幂等。"""
```

行为（全部幂等，重复调用安全）：

1. **加成员**：`dup.user_id` 既非 `canonical.user_id`（owner）又不在 `account_members` → 插 `account_members(canonical.id, dup.user_id, granted_via)`；已是 owner / 已是成员 → 跳过。
2. **条件合并 dup 行**：
   - `dup` **无**未软删的 `PublishRecord` 且无任务绑定 → 软删 `dup`（`is_deleted=True`，`platform_user_id` 保持 / 置 NULL 以释放槽位）。
   - `dup` **有**记录 / 任务 → 不软删，置 `dup.merged_into = canonical.id`（从可用列表排除、未终态发布记录仍发完）。
   - `dup` 已是 `is_deleted` 或 `merged_into` 已指向 `canonical.id` → 幂等跳过。
3. **审计**：写一条 `account.dedup_merge` 审计（含 `dup.id` / `canonical.id` / `granted_via`）。

**调用方不在函数内 commit 抽取/约束相关写**：函数只做上述合并写，commit 由调用方（worker finish / 检测路径 / 批量回填）统一收口；并发 `IntegrityError` 兜底仍在调用方（§4 / §5）。函数本身不写 `platform_user_id`、不碰 X 的 claim（那是调用方"无既有 canonical"分支的事），避免与唯一约束纠缠。

## 5. 历史回填 + 自动合并（需求 #1，sync 路径 — 评审点 2/5/8）

**触发点 A：账号有效性检测**（[auth.py:991](../../../server/app/modules/accounts/auth.py)，sync）。`detect_logged_in` 通过后用 `extract_platform_user_id_sync` 得 `X`：

- `X` 为空 → 不动。
- `account.platform_user_id == X` → 已回填，不动。
- `platform_user_id` 为 NULL 且拿到 `X` → `SELECT` 其它活账号（`X`、`is_deleted==False`、`merged_into IS NULL`，排除自己）：
  - **无**：`self.platform_user_id = X`（自己即 canonical）。
  - **有 canonical C** → 调用同一个 `reconcile_duplicate_into_canonical(db, self, C, granted_via="backfill_merge")`（§4a），完成加成员 + 条件合并 + 审计。
  - 并发 `IntegrityError` 兜底。
- **`platform_user_id` 已有值但 `!= X`（重新识别 — 评审点 5，收紧）**：
  - 该行**无**发布记录/成员/任务 → 视为干净行，按新 `X` 重跑决议。
  - 该行**有**历史/成员/任务 → **不自动改身份**（避免把旧历史/成员/语义带到另一个物理账号）；置「身份冲突」标记、记 warning，**留 admin 显式确认**后再改。

**触发点 B：admin 批量回填（闭环 — 评审点 8）**。新增 `POST /api/accounts/backfill-identity`（`require_admin`）：扫描「浏览器平台 + `status==valid` + `platform_user_id IS NULL` + `merged_into IS NULL`」的账号，逐个走触发点 A 的检测+抽取+决议，返回汇总 `{processed, backfilled, merged, conflicts, still_unknown, failed}`。前端给「身份未知」账号一个徽标（`identity_known=false`）+ admin 一键批量回填入口。背景定时任务列为后续（§11）。

**已知取舍（本期不展开）**：canonical 选取靠「谁先占 X」，确定但非按业务优先级；历史 union 靠 `merged_into` 可查但本期不建聚合视图。

## 6. 共享可见性 + 权限矩阵（评审点 6）

两个统一鉴权判定，全模块复用：

- `user_can_use_account(db, account, user)` → `admin` 或 owner 或 `user.id ∈ account_members`。
- `user_can_manage_account(account, user)` → `admin` 或 owner。

**权限矩阵（逐条落地，不留「规划期审计」）**：

| 操作 | 入口 | 判定 |
|---|---|---|
| 账号列表可见 | `list_accounts(viewer_id)` 下沉 [router.py:124](../../../server/app/modules/accounts/router.py) | `use`（owner ∪ 成员；admin 全量）；排除 `merged_into IS NOT NULL` |
| 账号详情 | `GET /api/accounts/{id}` | `use` |
| 任务内使用账号 | [service.py:559](../../../server/app/modules/tasks/service.py) `account.user_id != user_id` | 改 `not user_can_use_account(...)` |
| pipeline 动态派号 | [distribute_node.py:64](../../../server/app/modules/pipelines/nodes/distribute_node.py) `Account.user_id == owner` | 改为 `owner ∪ EXISTS(account_members)`（admin 全量） |
| 任务派号预览 | `preview_task_assignment` | 同上，用 `use` |
| ~~账号 usage 状态~~ | ~~`GET /api/accounts/usage`~~ | **随 §7 推迟，本期不做** |
| 账号有效性检测 / 主动重登 canonical | check / relogin 入口 | `manage` |
| 改名 / 改分发开关 / `update_account_fields` / 删除 | 对应入口 | `manage`（成员 403） |
| 成员查看 / 移除 | §6 成员管理 | `manage` |

**成员管理（评审点 7）**：
- `GET /api/accounts/{id}/members` → 成员列表（含 owner 标识、granted_via）。`manage` 可见。
- `DELETE /api/accounts/{id}/members/{user_id}` → owner/admin 移除成员；幂等。
- 自动授予造成的误授可由 owner/admin 撤销，无需删整个 canonical。

**删除语义**：owner/admin 软删 canonical → 释放槽位（`platform_user_id=NULL`，[service.py:355](../../../server/app/modules/accounts/service.py)）+ 清空 `account_members` + 保留发布历史；成员随之失访。本期「use-only，无自助 leave」。

**刷新即重登的口子（成员）**：成员不能点「重登这个共享账号」的管理按钮；成员保活共享会话靠 §11 的「直登 canonical profile」（后续）。

## 7. 排队 + 可见状态（需求 #4）—— **v3：整体推迟到 §11，本期不做**

**串行化机制：零新增、首版直接复用。** 共享账号 = 单 canonical 行 + 单 `state_path`/`profile_key`，成员的发布/重登天然落在同一把 `BrowserProfileLock`（按 profile_key）+ 同一把 per-account 锁（按 canonical id）+ 全局信号量上自动串行；登录与发布在同一 profile_key 互斥。**这部分无需任何新代码。**

**可见状态（usage 端点 / 前端徽标 / 排队计数）= 本期不实现**，移入 §11。即：不加 `GET /api/accounts/usage`、不加 owner_id→用户 解析器、不加前端排队徽标 / usage 轮询、§10 不含排队状态测试、§6 矩阵的 usage 行不落地。理由：纯展示、性价比最低，砍掉零正确性损失，先把「查重 + 共享 + 回填」主干跑通。v2 的口径设计保留在 git 历史备查。

## 8. 错误处理 / 边界

**错误处理（全程不阻断登录）**：
- 抽取 best-effort：异常→记诊断→返回 None→不查重，登录/检测照常成功。
- 查重并发：`IntegrityError` 兜底重查转并入分支。
- storage_state / 文件操作失败：记日志、不影响加成员（首版本就不拷 cookie）。
- 头条 DOM/API 漂移：抽取返回 None，降级为「无查重」，不崩。

**边界**：
- 软删行 `platform_user_id` 恒 NULL → 不与新抽取的 X 撞（释放槽位语义成立）。
- `merged_into` 行从查重 SELECT 与可用列表排除，但其未终态发布记录正常发完。
- 非头条浏览器平台无 hook → NULL → 行为与今日完全一致，**无回归**。
- 行被重登成另一物理账号（`platform_user_id != X`）→ §5「重新识别」收紧分支（有历史则阻断待确认）。

## 9. 导入/导出（评审点 9）

- **导出**：照常带 `platform_user_id`（[auth.py:1281](../../../server/app/modules/accounts/auth.py)）。
- **导入（浏览器账号）**：**不信任包内 `platform_user_id`**——导入时一律置 NULL（[auth.py:1198/1210](../../../server/app/modules/accounts/auth.py) 改为不写该字段），由后续账号检测/登录再抽取 + 走查重并入。避免「另一用户导入同一账号包」直接撞唯一约束导致整包 flush 失败。
- **导入（API 账号，如公众号）**：保持现有 `app_id` 即 `platform_user_id` 的 dedup 不变。

## 10. 测试计划

MySQL / `build_test_app`；抽取 hook 用 monkeypatch 注入 X，免浏览器。

- **单元**：`toutiao_creator_id.py` 解析（json `media_id` / 文本兜底 / DOM label / 数字校验）。
- **查重决议（worker 路径）**：首登无 canonical→升 canonical 写 X；首登有 canonical→加成员 + **不拷 cookie** + B 干净则软删/有历史则 `merged_into` + `resolved_account_id=C` + finish 返回 C；并发 `IntegrityError` 兜底。
- **回填（sync 路径）**：NULL→写 X；NULL+已有 canonical→合并（条件软删/`merged_into` + 成员 + 历史保留）；`!=X` 有历史→阻断待确认、无历史→重识别。
- **批量回填**：`POST /api/accounts/backfill-identity` 汇总计数正确；非 admin 403。
- **可见性/权限矩阵**：成员见共享账号、非成员不见、admin 全量；`merged_into` 行不出现在列表；成员能在任务/distribute 用共享账号；成员改名/删除/改字段/移除成员→403。
- **成员管理**：列表 / 移除 / 幂等。
- **删除**：软删 canonical 清成员 + 释放槽位。
- **导入**：浏览器账号包内 `platform_user_id` 被忽略（导入后为 NULL），不撞约束。
- ~~**排队状态**~~：随 §7 推迟，本期无此测试。
- **迁移**：`0047` 建表 + 加列。

## 11. 后续（本期不做，显式记账）

- **成员刷新 cookie（#2 完整版）**：让已知成员的「重新登录」直接 target canonical 的 `state_path`/`profile_dir`，使发布即时见新 cookie（无需拷贝/重指）；首版仅加成员、不刷新。
- **canonical 会话过期且成员有新会话的一次性收养**（R4 重指/R1 拷贝）按需再评估。
- **历史 union 视图**：基于 `merged_into` 聚合某物理账号全部发布记录。
- **后台定时批量回填**：把 §5 触发点 B 包成定时任务 + 进度可见。
- **排队 + 可见状态（§7 整体）**：`GET /api/accounts/usage` + owner_id→用户 解析器 + 前端占用/排队徽标 + 轮询。串行化本身首版已天然生效，这里只补"可见"。
- **进行中 dedup 登录映射回 canonical 的排队展示**（§7 口径扩展，依赖上一条）。

## 12. 影响文件清单（实现期细化）

- `server/app/modules/accounts/models.py` — 新 `AccountMember`；`Account.merged_into`；`AccountLoginSession` 加两列。
- `server/alembic/versions/0047_account_dedup_sharing.py` — 迁移。
- `server/app/modules/accounts/auth.py` — worker 内查重决议（§4）、回填+重识别收紧（§5）、finish 返回 `resolved_account_id`、导入不信任 `platform_user_id`（§9）、登录完成注入 async 抽取。
- `server/app/modules/accounts/login_broker.py` — `_read`/`read_login_state` 增 async 抽取器回调。
- `server/app/modules/accounts/service.py` — `user_can_use_account`/`user_can_manage_account`、**`reconcile_duplicate_into_canonical`（§4a，去重合并唯一落点）**、`list_accounts(viewer_id)` 排除 `merged_into`、`delete_account` 清成员、成员管理 service、批量回填 service。
- `server/app/modules/accounts/router.py` — 列表可见性下沉、管理路径鉴权、成员管理端点、`POST /api/accounts/backfill-identity`。（`GET /api/accounts/usage` 随 §7 推迟，不做。）
- `server/app/modules/accounts/schemas.py` — `AccountRead` 增字段、成员/usage/回填响应体。
- `server/app/modules/tasks/service.py` — 账号使用校验改 `user_can_use_account`、`preview_task_assignment` 同步。
- `server/app/modules/pipelines/nodes/distribute_node.py` — 派号过滤改 owner ∪ 成员。
- `server/app/modules/tasks/drivers/__init__.py` — Protocol 增 sync/async 两个可选抽取方法。
- `server/app/modules/tasks/drivers/toutiao.py` + 新 `drivers/toutiao_creator_id.py` — 移植 spike（纯解析 + sync/async I/O）。
- `web/src/features/accounts/*`、`web/src/api/accounts.ts`、`web/src/types.ts` — owner/成员标识、管理按钮 gating、成员管理 UI、「身份未知」徽标 + admin 批量回填入口。（排队徽标 + usage 轮询随 §7 推迟，不做。）
- `server/tests/` — 见 §10。

## 13. v2 变更记录（对 v1 评审的回应）

1. 刷新 cookie：v1 用 storage_state 覆盖无效（发布读 profile_dir）→ 首版改**只加成员、不刷新**，刷新列入 §11。
2. driver hook sync/async：→ 拆**纯解析 + sync/async 两适配方法**（§3）。
3. worker 结果无字段携带：→ `AccountLoginSession` 加 `resolved_account_id`/`extracted_platform_user_id`，决议在 worker、finish 读回（§2.4/§4）。
4. B 临时行假设不成立：→ **条件合并** + 新 `merged_into` tombstone（§2.3/§4/§5）。
5. 重新识别风险：→ 有历史/成员/任务则**阻断待 admin 确认**，仅干净行自动重识别（§5）。
6. 使用权限不全：→ 完整**权限矩阵**含 distribute/preview/usage/check/relogin（§6）。
7. 缺成员撤销：→ 成员**列表 + 移除** API/UI（§6）。
8. 回填非闭环：→ admin **批量回填**端点 + 计数 + 「身份未知」徽标（§5/§8）。
9. 导入冲突：→ 浏览器账号导入**不信任包内 `platform_user_id`**（§9）。
10. 排队口径夸大：→ 明确**只覆盖 canonical 自身占用**，进行中 dedup 登录不计入（§7）。
