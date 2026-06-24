---
description: Geo 协作平台生文 Loop 入口。自然语言目标 → Ralph 风格自动产出 N 篇过自评文章入未审核库 → 飞书播报。
---

# /goal — Geo 生文 Loop

你刚被 `/goal $ARGUMENTS` 调用。把这条命令当作 `geo-goal-orchestrator`
skill 的入口包装：

1. **立刻** invoke the `geo-goal-orchestrator` skill（用 Skill tool）来装载完整 playbook。
2. 装载后，按 skill 里的 Required Checklist 一项一项执行，把 `$ARGUMENTS` 当作用户的自由文本目标传给「Goal Parsing 规则」段。
3. **不要**在装载 skill 之前先自己解析目标或调 MCP；skill 内部第一步就是 sanity check，让它来跑。

## 同事第一次用 /goal 之前要看的

如果是你（同事）第一次用 `/goal`，先看 `.claude/README.md`（本地不入库）
完成 5 步 onboarding（MCP token 配置 + skill 文件本地放置）；不然 sanity
check 会立刻失败。

## 这条命令做什么 / 不做什么

**做**：
- 自然语言目标解析（"今天 5 篇国风游戏文章"）
- 自动选题（从问题池避重）
- 启动多个 fresh-context subagent 分别写文章 + 评分
- 把净产出查 GEO 拿 ground truth 作停止条件
- 完成后飞书群播报

**不做**：
- 不发布（分发走独立 loop）
- 不直接改 `article.review_status`（人审兜底）
- 不在主对话里写文章草稿（子 agent 干，不污染主 context）
