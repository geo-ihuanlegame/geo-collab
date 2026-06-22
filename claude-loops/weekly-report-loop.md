# 周报 Loop 配方

> **运行方式**：`/loop claude-loops/weekly-report-loop.md`
> **目标**：每周一跑，飞书发一份模板 / 账号表现周报

## 工具

- `list_prompt_templates(scope="generation")`
- `get_template_performance(template_id, window_days=7)`
- `list_accounts(distribution_enabled=true)`
- `get_account_performance(account_id, window_days=7)`
- `notify_feishu(title, message, level)`

## 流程

```
notify_feishu(title="周报生成中", message="拉取过去 7 天数据...", level="info")

templates = list_prompt_templates(scope="generation").data
template_perf = []
for t in templates:
    p = get_template_performance(template_id=t.id, window_days=7)
    if p.ok:
        template_perf.append((t.name, p.data))

accounts = list_accounts(distribution_enabled=True).data
account_perf = []
for a in accounts:
    p = get_account_performance(account_id=a.id, window_days=7)
    if p.ok:
        account_perf.append((a.display_name, p.data))

# 整理成 markdown
lines = ["# 周报（过去 7 天）", "", "## 模板表现"]
for name, p in sorted(template_perf, key=lambda x: -(x[1].get("avg_views") or 0)):
    lines.append(f"- {name}: 产文 {p['article_count']} 篇 / 均阅 {p.get('avg_views')} / 通过率 {p.get('approval_rate')}")
lines.append("\n## 账号表现")
for name, p in sorted(account_perf, key=lambda x: -(x[1].get("avg_views") or 0)):
    lines.append(f"- {name}: 发布 {p['publish_count']} 次 / 均阅 {p.get('avg_views')} / 均赞 {p.get('avg_likes')}")

notify_feishu(
    title="本周周报",
    message="\n".join(lines),
    level="done",
)
```

## 注意

- POC 期数据多半是 stub / 无 metrics —— 报告内容可能很空，正常
- v2 接入真实平台数据后才会有内容
