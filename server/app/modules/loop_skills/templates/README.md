# `.claude/` — Geo 协作平台 Claude Code 工程目录

> 本目录的所有文件是 `geo-collab` 服务端通过 `/api/mcp/loop-skill-bundle/`
> + `install_loop_skills` MCP 工具分发的「模板」（服务端正本在仓库
> `server/app/modules/loop_skills/templates/`）。你解压 / 安装到本机后
> 可以按需修改 —— 本地改动不会被服务端覆盖，下次 install 时 Claude Code
> 会询问你是否覆盖。
>
> 装好后：重启 Claude Code → 输入 `/goal 帮我产出 1 篇国风游戏文章作为冒烟`

## 文件清单（本地）

| 文件 | 干什么 |
|---|---|
| `commands/goal.md` | `/goal` slash command — Geo 生文 Loop 入口 |
| `skills/geo-goal-orchestrator/` | 主对话调度 playbook（被 /goal 装载） |
| `skills/geo-article-writer/` | writer subagent playbook（每篇文章一个 fresh subagent） |
| `skills/geo-article-verifier/` | verifier subagent playbook（每篇文章一个 Haiku subagent 评分） |

---

## 第一次用 /goal —— 6 步 onboarding

```
1. 把 spec/plan 里的 SKILL.md + command + README 内容复制到本地 .claude/
   （或从同事那拿 zip / 用 install_loop_skills MCP 工具自动装）
2. 一次性配置（每台机器一次）
   - 打开 ~/.claude.json，加 mcpServers.geo 段
   - 把后端管理员发的 GEO_MCP_TOKEN 填到 headers.X-MCP-Token
   - 详细参考 docs/mcp-setup-notes.md
3. 重启 Claude Code
4. 在 Claude Code 里输入 /mcp，确认 geo server 显示 "connected"
5. 打开本机 .claude/skills/geo-article-writer/SKILL.md，找到「矩阵特例」段
   `main_category_id = <REPLACE_ME>` 行；去 GEO 后台「图库管理」→ 主推栏目
   里找你矩阵对应栏目（比如餐厅养成记），把 id 填进去（数字）。
6. 在 Claude Code 里输入：
   /goal 帮我今天产出 5 篇关于国风游戏的文章

之后 /goal 会自动跑（约 10-20 分钟）；完成后飞书群会有播报。
```

---

## 跑 /goal 时主对话会出现什么

干净的状态条，不会被子 agent 写作 / 评分细节污染：

```
[快检] pool=问题池 N=5 matrix=默认 通过
[第 1/15 轮] 选题 qid=123 → 改写中 …
[第 1/15 轮] 改写完成 article_id=824, 评审中 …
[第 1/15 轮] 评审 决策=approved 总分=82
[净产出] 今日通过 goal 评审的文章数: 1/5
[第 2/15 轮] 选题 qid=124 → 改写中 …
...
[完成] 净产出 5/5, 共耗时 12m, 飞书已播报
```

---

## 复用 / 定制路径

| 想改的事 | 怎么改 |
|---|---|
| 默认 N | 直接说：`/goal 今天 8 篇` |
| 默认问题池 | 直接说：`/goal 用 wenti01 池产出 5 篇` |
| 加新内容矩阵 | 本地复制 `skills/geo-article-writer/` 为 `geo-article-writer-<code>/`，**只改 `## 矩阵特例` 段**；调用 `/goal matrix=<code> ...` |
| 单独写一篇（不走 loop） | 主对话 `Skill geo-article-writer` 进入写作模式手动配合写——**不评分、不计 netto** |
| 改评分门槛 | 改本地 `skills/geo-article-verifier/SKILL.md` 的「决策门槛」段 |

---

## 常见排障

| 症状 | 原因 | 处理 |
|---|---|---|
| `/goal` 找不到 | 本地 `.claude/commands/goal.md` 没创建 / Claude Code 没重启 | 抄文档 + 重启 |
| `/goal` 启动后立刻退出，提示 "MCP 不可用" | `~/.claude.json` 没配 / token 错 | 走上面 onboarding 第 2 步 |
| 跑到一半 attempts 用完但 netto=0 | verifier 一直不给 approved（评分门槛太严 / 选题质量差） | 单独 `Skill geo-article-writer` 试一题看写作质量；写作没问题就是 verifier 门槛 |
| writer 报 `save_article 415` | markdown 里塞了不支持的元素（罕见） | 给后端工程师看错误 detail |
| 配图全部失败 | stock_category 没配 / category_id 不对 | 不致命，文章已落库；联系平台扩展同事配 |
| 飞书没收到播报 | webhook 没配 / 配错环境 | 检查后端 `GEO_FEISHU_WEBHOOK_URL` |

---

## 三类同事的接触面

| 角色 | 想做什么 | 看哪几个文件 |
|---|---|---|
| **运营**（90%） | 跑 `/goal` 出文章 | 本 README 就够 |
| **写作风格调优** | 改矩阵风格 / 加新矩阵 | 本地 `skills/geo-article-writer/SKILL.md` 的「矩阵特例」段 |
| **平台扩展** | 加新 stop 条件 / 评分维度 / MCP 工具 | orchestrator skill + 后端 `auto_review/service.py` + `mcp/tools/catalog.py` |
