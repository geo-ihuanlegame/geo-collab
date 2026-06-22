# GEO MCP Loop · 老板演示 walkthrough

## 准备

1. 起 GEO 后端：`uvicorn server.app.main:app --reload`
2. 起 GEO 前端：`pnpm --filter @geo/web dev`
3. 飞书群打开 + 确认 webhook 配置正确
4. 起一个新的 Claude Code 会话

## Demo 1: 概念回顾（5 分钟）

打开 `docs/superpowers/specs/2026-06-17-loop-engineering-geo-integration.html`：
- 翻第 1 节"什么是 Loop Engineering · 五件套"
- 翻第 5 节"方案 C · Agent Town"
- 一句话总结：今天演示的是方案 C 用 Claude Code + GEO MCP 的最小落地

## Demo 2: MCP 连通（30 秒）

在 Claude Code 里：
```
/mcp
```
让老板看到 `geo: connected` + 17 个 tool 名字

## Demo 3: 生文 Loop 现场跑（5-10 分钟）

```
/loop claude-loops/generation-loop.md
```

老板看到：
- 飞书群弹出 "生文 Loop 开始" 消息
- Claude Code 实时显示 tool_use 序列（list_question_items → 主对话写 markdown → save_article → illustrate_article → submit_review_decision）
- 切到 GEO 前端「未审核库」tab 看到新文章出现
- 切到 GEO 前端「文章详情」看到 AI 配图插在正文里
- 飞书群最后收到 "生文 Loop 完成 · 产出 N/5 篇"

## Demo 4: 现场点通过/否决（2 分钟）

在 GEO 前端「未审核库」点 1-2 篇文章，标 approved。
讲：这是人工兜底——Loop 评 70+ 进来的，运营再终审。

## Demo 5: 发文 Loop（3 分钟）

```
/loop claude-loops/distribute-loop.md
```

老板看到：
- 飞书消息 "发文 Loop 开始"
- GEO 前端「分发引擎」tab 出现新任务（stop_before_publish=True 状态）
- 飞书消息 "发文 Loop 完成 · 已创建任务 #N"

## Demo 6: 决定下一步（5 分钟）

讲：
- POC 跑通 = "Claude Code 当 Loop 大脑 + GEO 当能力底座" 这条路是可行的
- v2 候选：长跑服务器 / 真 metrics 接入 / 飞书内 OpenClaw 风格交互 / Skill 包装 / 选题 Loop（拉热榜借势）
- 老板拍：v2 投入哪几条
