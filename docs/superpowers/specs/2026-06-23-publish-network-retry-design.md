# 断网 / 弱网下的发布重试设计（at-most-once + 提交边界模型）

- 日期：2026-06-23
- 状态：已与需求方逐段确认（4 个关键边界经 AskUserQuestion 锁定）
- 范围：仅后端。覆盖两类发布驱动（API 型 + 浏览器型），两个重试层级（传输层 + 记录层）
- 性质：调研可行性 + 抽象通用方案。本文是交付物本身；实现计划另起 `writing-plans`
- 来源：发布链路全量代码调研（`tasks/executor.py` / `runner.py` / `runner_api.py` / `drivers/*` / `service.py`）

## 1. 需求与可行性结论

**需求**：发布过程中遇到断网 / 弱网时，能正确处理网络层异常并重试，把方案做成**抽象通用层**，不写死在某个驱动文件里。

**可行性结论：可行。** 现有链路完全没有「网络感知重试」与「提交边界」概念，任何网络异常一律落 `failed` 等人工。本设计补上一个平台无关的弹性层 + 一个 at-most-once 安全护栏，覆盖两类驱动、两个层级。

**真正的 exactly-once 不可能**——那需要平台侧幂等键（被否的 at-least-once 路线）。本方案交付的是 **at-most-once（绝不重发）**：提交点之前激进自动重试；一旦跨过提交点出网络问题，绝不自动重发，转人工对账。

## 2. 已锁定的边界（需求方确认）

| 维度 | 决定 | 理由 |
|---|---|---|
| 幂等取舍 | **at-most-once（绝不重复发布）** | 重复发布比漏发更不可接受；自动重试只覆盖「提交前」幂等步骤 |
| 覆盖范围 | **两类驱动都覆盖（分层设计）** | 通用传输层服务两类；终态提交守卫按驱动可选挂钩 |
| 重试层级 | **传输层 + 记录层都要** | 网络抖动（进程未崩）走传输层；进程/会话整挂走记录层 |
| 状态表达 | **加字段、不动 status** | 追加式迁移，不改 `publish_records.status` 的 CHECK 约束，最小侵入 |

非目标：平台侧幂等键 / 对账自动化（at-least-once）；litellm / hot_lists / feishu 等其它调用路径的接入（`resilience.py` 设计上可复用，但本期不主动改它们）；前端 UI 大改（仅 `failure_kind` 渲染一条告警 + 禁用一键重试按钮）。

## 3. 调研事实基线（现状）

发布链路与网络 I/O 触点（带证据）：

- 编排：`tasks/router` → `executor.execute_task` (`executor.py:108`) → `_start_runnable_records` (`:320`) → 线程池 `_publish_record` (`:744`) → `build_publish_runner_for_record` (`:1025`) 按驱动 `mode` 分叉。
- 浏览器运行器：`runner.run_publish` (`runner.py:287`)；网络 I/O = `page.goto` / 上传 / 点发布 / 页内 `fetch`，散落在 `drivers/toutiao.py`、`drivers/toutiao_inpage.py`。导航超时默认 30s（`runner.py:350` `set_default_navigation_timeout`）。`page.on("requestfailed")` 仅作诊断采集、**不拦截发布**（`runner.py:55-96`）。
- API 运行器：`runner_api.run_publish_api` (`runner_api.py:123`)；网络 I/O = `wechat_client.py` 的 `fetch_access_token` / `upload_thumb` / `upload_content_image` / `add_draft`，全部经单点 `_request` (`wechat_client.py:57-62`) 收口，`httpx.HTTPError → WeChatApiError`；HTTP 4xx / errcode 非 0 → `WeChatApiError`（`_parse_response`，`:39-54`）。默认 client 超时 `connect=20s / read=60s / write=60s`（`:151`）。
- 异常协议：`drivers/base.py:58-84` 的 `PublishError` / `UserInputRequired`；`shared/errors.py` 的 `ClientError` 家族。`_finish_record_future` (`executor.py:768`) 按异常类型分流，`UserInputRequired` 在 `PublishError` 之前捕获（子类优先，顺序敏感）。
- 现有「重试」：① 手动 `retry_record` (`service.py:296`)——只允许原始 `failed` 记录，防重检查只认「同 文章/账号 仍有活跃或 succeeded 记录」（`:318-335`）；② `recover_stuck_records` (`service.py:358`)——lease 过期的 `running` 拨回 `pending` 重跑，有租约保护。**没有任何针对网络异常的自动退避重试**。
- 幂等：`PublishTask.client_request_id` 只防重复**建任务**（`service.py:125-133`），不防单条记录重复发布；无「这篇是否已发」对账。
- 依赖：`requirements.txt` 有 `httpx==0.28.1`、`playwright==1.57.0`，**无 tenacity / backoff**。本方案自带轻量退避，不新增依赖。

**现状的洞**：commit-uncertain 失败（提交请求发出、成功响应丢了）会落成普通 `failed`，而 `retry_record` 的防重只认 succeeded 记录——这种失败没有 succeeded 记录，所以**手动一键重试会被放行 → 可能重发**。这是 at-most-once 必须堵的口子。

## 4. 核心模型：提交边界（commit boundary）

发布流程天然两段：

| 阶段 | API 驱动（微信） | 浏览器驱动（头条） | 网络中断可否安全重试 |
|---|---|---|---|
| **提交前**（幂等、可重放） | 换 token、传封面、传正文图 | 打开发布页、上传、填表单 | ✅ 重放无副作用 |
| **提交**（不可逆那一下） | `add_draft` | 点「确认发布」+ 等成功确认 | ❌ 响应丢了就不知道发没发 |

设计的全部安全性都围绕「精确标出提交点」展开：

- **提交前**任意网络抖动 → 传输层 `retry_call` 激进退避重试。
- **跨提交点**出网络问题 → 由提交守卫判定为「结果未知」，锁定为 `commit_uncertain`，**永不自动重发**。

## 5. 抽象层设计

### 5.1 `server/app/shared/resilience.py`（新增，通用、零 ORM、零平台逻辑）

放 `shared/` 因为它平台无关，未来 hot_lists / feishu / litellm 可复用。

```python
@dataclass(frozen=True)
class RetryPolicy:
    enabled: bool = True
    max_attempts: int = 3          # 含首次，总尝试次数
    base_delay: float = 1.0        # 秒
    multiplier: float = 2.0        # 指数退避因子
    max_delay: float = 15.0        # 单次退避上限
    jitter: float = 0.2            # ±20% 抖动，打散并发重试
    max_elapsed: float | None = 60.0  # 总时限，超时立即放弃

class TransientClassifier:
    """可插拔：把异常判为 transient(可重试) / permanent(立即抛)。"""
    def is_transient(self, exc: BaseException) -> bool: ...
    def register(self, predicate: Callable[[BaseException], bool | None]) -> None: ...

def retry_call(fn, *, policy, classifier, on_retry=None, sleeper=time.sleep):
    """同步退避重试循环。permanent 异常立即抛；transient 退避重试到次数/时限耗尽再抛最后一次。
    on_retry(attempt, exc, delay) 用于观测；sleeper 可注入假实现供测试。"""
```

默认分类器注册：
- **transient**：`httpx.ConnectError` / `ConnectTimeout` / `ReadTimeout` / `WriteTimeout` / `PoolTimeout` / `RemoteProtocolError` / `NetworkError`；HTTP 5xx、429；Playwright 导航/等待 `TimeoutError`。
- **permanent**：HTTP 4xx；`WeChatApiError`（errcode 非 0 = 服务端明确回错）；`ValueError`；其它未注册的默认 permanent（保守，不盲目重试未知异常）。
- 调用方可 `register` 追加平台特例（如微信 `-1 系统繁忙` 可视为 transient）。

**纯函数、不碰 DB**。异步变体 `retry_call_async` 仅在确有异步触点时才加（当前发布路径是同步的，YAGNI 暂不加）。

### 5.2 `drivers/base.py`（编辑：+1 异常 + 提交守卫）

```python
class CommitUncertainError(PublishError):
    """跨提交点后发生网络失败：请求已发出，平台是否收到未知。绝不自动重发。"""

class CommitGuard:
    """runner 构造并注入驱动；把不可逆提交包进 committing()。驱动不碰 ORM——
    标记落库由 runner 提供的回调完成，驱动只调一个不透明 callable。"""
    def __init__(self, mark_pending: Callable[[], None], classifier: TransientClassifier): ...

    @contextmanager
    def committing(self):
        self._mark_pending()        # 进入:DB 落 commit_attempted_at 并 commit
        try:
            yield
        except BaseException as exc:
            if self._is_network_uncertain(exc):   # 网络类 → 结果未知
                raise CommitUncertainError(str(exc)) from exc
            raise                                  # 服务端明确回错 → 原样,干净失败可重试
```

驱动用法（平台知识只有「提交点在哪」一句）：

```python
# 微信 API 驱动
with commit_guard.committing():
    media_id = add_draft(access_token, article, client=client)

# 头条浏览器驱动
with commit_guard.committing():
    click_confirm_publish(page); wait_for_publish_success(page)
```

「未知 vs 干净失败」的判定通用（复用 `classifier`），集中在守卫里，不散进各驱动。

### 5.3 `PublishRecord` + 迁移（追加式）

```python
commit_attempted_at: Mapped[datetime | None]   # 跨提交点前置时间戳
failure_kind: Mapped[str | None]               # commit_uncertain / transient_exhausted / permanent / None
```

追加式 Alembic 迁移，**不改 `status` 的 CHECK 约束**。`status` 仍为 `failed`，`failure_kind` 承载语义、驱动 UI 与重试护栏。

## 6. 两层重试落地

### 6.1 传输层（单次执行内，进程未崩）

- **API 驱动**：`fetch_access_token` / `upload_thumb` / `upload_content_image` 由驱动用 `retry_call(lambda: ..., policy, classifier)` 包裹；`add_draft` **不进 retry_call**（等价 max_attempts=1），只进 `commit_guard.committing()`。`wechat_client.py` 保持纯函数，重试编排在驱动层，不污染客户端。
- **浏览器驱动**：新增 helper（如 `runner.nav_with_retry(page, url, policy)`）包 `page.goto` 与提交前的 `wait_for`；点发布那一下进 `commit_guard.committing()`。`publish_step` 诊断上下文保留，retry 在其内层。

### 6.2 记录层（进程 / 会话整挂，跨执行）

- `recover_stuck_records`（`service.py:358`）加护栏：lease 过期的 `running` 记录——
  - `commit_attempted_at IS NULL` → 安全拨回 `pending` 自动重跑（**维持现状行为**）。
  - `commit_attempted_at IS NOT NULL` → 标 `failed` + `failure_kind=commit_uncertain`，**绝不重跑**（死在提交中途，无法安全自动重发）。
- `retry_record`（`service.py:296`）手动一键重试：`failure_kind=commit_uncertain` 默认**拦截**，报「已提交但结果未知，请先到平台核对」；新增显式 `force=true` 旁路，让运营核对后仍能强制重发。at-most-once 默认成立，又不把人困死。

### 6.3 executor 分流（`_finish_record_future`，`executor.py:768`）

在 `PublishError` 分支**之前**加 `CommitUncertainError` 捕获（子类优先，顺序同 `UserInputRequired`）：标 `failed` + `failure_kind=commit_uncertain` + 截图存证 + 停会话，消息「已提交但结果未知，请人工核对平台后再决定是否重发」。其余 `PublishError` 分支补写 `failure_kind=permanent`（默认）。

## 7. 配置 / 观测

- 配置（`core/config.py`，`GEO_` 前缀，`get_settings()` 走 lru_cache）：

  | 键 | 默认 | 说明 |
  |---|---|---|
  | `GEO_PUBLISH_RETRY_ENABLED` | `true` | 总开关 |
  | `GEO_PUBLISH_RETRY_MAX_ATTEMPTS` | `3` | 含首次 |
  | `GEO_PUBLISH_RETRY_BASE_DELAY_SECONDS` | `1.0` | 首次退避 |
  | `GEO_PUBLISH_RETRY_MAX_DELAY_SECONDS` | `15` | 单次退避上限 |
  | `GEO_PUBLISH_RETRY_MAX_ELAPSED_SECONDS` | `60` | 重试总时限 |

  **硬约束**：重试总时限（`max_elapsed` × 受影响步骤数的合理上界）必须 **< 单记录执行预算** `_record_execution_budget()`（`executor.py:713`），否则 watchdog 先于重试杀掉记录。本期取默认值时 60s 总时限远小于默认 300s+ 预算，安全；调大重试参数时需同步评估预算，spec 把这条列为实现期校验项。

- 观测：每次重试经 `on_retry` 钩子吐一条 `PublishDiagnosticEvent`（level=warn，如「网络抖动重试 2/3，退避 2.0s」），复用现有 `capture_publish_diagnostics` 落库（`executor.py:758`）。`commit_uncertain` 失败打独立日志便于排障。

## 8. 测试策略（全程无真实网络）

- `resilience.py` 纯单测：分类器正误（各 httpx / playwright 异常归类）、退避序列（注入假 `sleeper`，不真等）、次数/时限耗尽行为、permanent 立即抛。
- API 驱动：`httpx.MockTransport` 失败 N 次后成功 → 断言幂等步骤被重试；`add_draft` 终态注入 `ConnectError` → 断言抛 `CommitUncertainError` + `commit_attempted_at` 已落 + **未重试**；`add_draft` 注入 errcode 40164 → 断言普通 `PublishError`（干净失败、可重试）。
- 浏览器驱动：monkeypatch `page.goto` 先抛后成 → 断言 `nav_with_retry` 重试；提交点 `wait_for` 超时 → 断言 `CommitUncertainError`。
- executor / service：`CommitUncertainError` 路由到 `failed`+`failure_kind=commit_uncertain`；`recover_stuck_records` 对 `commit_attempted_at` 非空记录不重跑；`retry_record` 拦截 `commit_uncertain`，`force=true` 放行。
- 测试落 `server/tests/`，遵守 `build_test_app` + MySQL only + `@pytest.mark.mysql` 约定；纯 `resilience` 单测无需 DB。

## 9. 诚实的残留与权衡

- **不可消除的窗口**：标 `commit_attempted_at` 并 commit 后、提交响应回来前进程被 SIGKILL → 正确锁为 `commit_uncertain`（待人工）。这是 at-most-once 的正确行为（不自动重发），代价是需人工核对一次。
- **exactly-once 不做**：需平台幂等键 + 对账（at-least-once 路线，已被否）。
- **浏览器记录层重跑较重**：从头跑一遍浏览器发布开销大，但仅在「未到提交点」时发生，正确性优先、可接受。
- **`force=true` 是人控逃生口**：默认不放行 `commit_uncertain` 重试，运营核对平台后可强制——把「绝不重发」的最终判断权交还给人，而非系统盲目执行。

## 10. 改动清单（实现期参照）

| 文件 | 改动 |
|---|---|
| `server/app/shared/resilience.py` | 新增：`RetryPolicy` / `TransientClassifier` / `retry_call` |
| `server/app/modules/tasks/drivers/base.py` | 新增 `CommitUncertainError` + `CommitGuard` |
| `server/app/modules/tasks/drivers/wechat_mp.py` | 幂等步骤包 `retry_call`；`add_draft` 进提交守卫 |
| `server/app/modules/tasks/drivers/toutiao.py`(+`_inpage`) | 导航/等待包重试；点发布进提交守卫 |
| `server/app/modules/tasks/runner.py` / `runner_api.py` | 构造并注入 `CommitGuard`（含 mark_pending 落库回调）+ `nav_with_retry` helper |
| `server/app/modules/tasks/executor.py` | `_finish_record_future` 加 `CommitUncertainError` 分流 + `failure_kind` |
| `server/app/modules/tasks/service.py` | `recover_stuck_records` / `retry_record` 加 `commit_attempted_at` 护栏 + `force` |
| `server/app/modules/tasks/models.py` | `PublishRecord` 加 `commit_attempted_at` / `failure_kind` |
| `server/alembic/versions/*` | 追加式迁移（两列，不改 CHECK）。**排序:本迁移后于加密 spec 的 accounts 迁移**——见 §11 |
| `server/app/core/config.py` | 5 个 `GEO_PUBLISH_RETRY_*` 设置 |
| `web/`（小） | 发布记录页按 `failure_kind=commit_uncertain` 渲染告警 + 禁用一键重试 |
| `server/tests/test_*` | resilience 纯单测 + 驱动/executor/service 护栏测 |

## 11. 与并行设计（账号凭据加密）的协调

并行在途：`docs/superpowers/specs/2026-06-23-secret-encryption-design.md`（账号敏感凭据静态加密）。两者**无设计冲突、可并行推进**，但有三个机械对接点（非语义冲突）：

1. **Alembic 迁移顺序（已拍板：加密先、本设计后）** — 两边各加一条迁移，改的是不同表（加密改 `accounts` 列 JSON→TEXT；本设计给 `publish_records` 加两列），**无数据冲突**。约定：**加密迁移先落 main**，本设计的迁移 `down_revision` 指向加密迁移的 head（即本迁移在加密迁移之后生成）。实施期：在加密迁移合入 main 后再 `alembic revision`，自然链在其后，避免迁移 DAG 分叉 / 双 head。

2. **`server/app/core/config.py`** — 两边都往 `Settings` 追加新字段（加密：`secret_key`/`secret_keys`；本设计：5 个 `GEO_PUBLISH_RETRY_*`）。语义独立，顶多同区域 textual merge，手动并排即可。

3. **`server/app/modules/tasks/drivers/toutiao.py`** — 加密改 `:1072` 发布后存 storage_state（`write_state`）；本设计改导航重试 + 点发布提交守卫。**不同段落、不同职责**，顺序执行不嵌套；先后合并留意别 textual 擦碰。

**正向交互**：加密 spec 的 `EncryptedJSON` 是 ORM 层透明加解密，本设计在 `runner_api._resolve_access_token` 读 `account.api_credentials` 时拿到的已是解密 dict，`fetch_access_token` 包进 `retry_call` **零耦合、零改动**。
