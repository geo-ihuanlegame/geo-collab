# 发文 Loop 配方

> **运行方式**：在 Claude Code 里 `/loop claude-loops/distribute-loop.md`。
> **目标**：把已审核库待发布文章分发到合适账号 + 回流上一轮 metrics。

## 你是谁

GEO「餐厅养成记」官方矩阵的发文 Loop runner。

## 可用工具

- `list_articles(status?, review_status?, limit)` — 拿候选
- `list_accounts(platform_code?, distribution_enabled?)` — 拿可用账号
- `get_account_performance(account_id, window_days?)`（D6 后可用；先无）
- `create_distribute_task(name, article_ids, account_ids, platform_code?, stop_before_publish?)`
- `record_publish_metrics(record_id, metrics)`（D6 后可用）
- `notify_feishu(title, message, level)`

## 流程

```
notify_feishu(title="发文 Loop 开始", message="拉取已审核 + 分发", level="info")

# 1. 分发阶段
articles_resp = list_articles(status="ready", review_status="approved", limit=20)
articles = articles_resp.data.items if articles_resp.ok else []
if not articles:
    notify_feishu("发文 Loop 跳过", "已审核库无待发布文章", "info")
    return

accounts_resp = list_accounts(platform_code="toutiao", distribution_enabled=True)
accounts = accounts_resp.data if accounts_resp.ok else []
if not accounts:
    notify_feishu("发文 Loop 失败", "无可用 toutiao 账号", "error")
    return

# POC：直接全量分发；v2 用 get_account_performance 选 top-N
article_ids = [a.id for a in articles[:5]]  # 一次最多 5 篇
account_ids = [a.id for a in accounts[:3]]   # 限 3 个账号

r = create_distribute_task(
    name=f"Daily distribute {today}",
    article_ids=article_ids,
    account_ids=account_ids,
    platform_code="toutiao",
    stop_before_publish=True,  # POC 期手动确认，避免误发
)

if not r.ok:
    notify_feishu("发文任务创建失败", r.error, "error")
    return

# 2. 回流阶段（D6 之后启用）
# metrics = ... (placeholder)

notify_feishu(
    title="发文 Loop 完成",
    message=f"已创建任务 #{r.data.task_id}，分发 {len(article_ids)} 篇到 {len(account_ids)} 账号（stop_before_publish=True，请人工确认）",
    level="done",
)
```

## 停止条件

- 创建成功 → 退出，飞书 done
- 任意必要步骤失败 → 退出，飞书 error
- 已审核库无待发布 → 退出，飞书 info

## 注意

- POC 强制 `stop_before_publish=True` —— 防误发。人工去 GEO 前端「分发引擎」tab 确认后再继续。
- 回流阶段在 D6 评估器 API 完成后再启用。
