# 10 · 交接 Runbook

> 关联文档：全部（[00 索引](./README.md)）。本文是"接手第一天"的总入口。

---

## 1. 系统一页纸画像

- **是什么**：多平台内容自动化发布平台。运营写/AI 生成文章 → 选平台账号 → Playwright 自动发布，验证码/失效时 noVNC 人工接管。
- **进程**：`nginx`（80）→ `app`（Web API + SPA + AI 生文后台线程）+ `worker`（单实例发布）+ `mysql` + `minio`。DB 是唯一协调点（租约 + 乐观锁，无 MQ）。
- **代码**：模块化单体，`server/app/modules/<域>/{models,schemas,service,router}.py`；前端 `web/src/features/`。
- **部署**：Linux + Docker Compose。发布依赖 Xvfb/x11vnc/websockify/noVNC，只在容器内运行。
- **唯一事实源**：开发约定看 `CLAUDE.md`；体系化文档看本 `doc/`。

> 详细架构见 [03 技术架构](./03-technical-architecture.md)。

---

## 2. 交接清单

### 2.1 代码与文档
- [ ] 仓库访问权限（GitHub）已转移/授予
- [ ] `CLAUDE.md`、`AI_GENERATION.md`、`DEPLOYMENT.md`、本 `doc/` 已通读
- [ ] CI（`.github/workflows/ci.yml`）与分支保护设置已知悉

### 2.2 凭据与密钥（经安全渠道移交，勿写进文档/仓库）
- [ ] `GEO_JWT_SECRET`（生产）
- [ ] MySQL `root` / `geo_user` 密码
- [ ] MinIO `ROOT_USER` / `ROOT_PASSWORD`
- [ ] `GEO_AI_API_KEY` / `GEO_AI_FORMAT_API_KEY`（AI 模型）
- [ ] `GEO_FEISHU_WEBHOOK_URL`（告警）、`GEO_FEISHU_APP_ID/SECRET`（选题同步）
- [ ] 服务器 SSH / VPN 接入方式（noVNC 走隧道）
- [ ] 异地备份存储（rclone remote / 备份机）凭据

### 2.3 运行环境
- [ ] 生产服务器信息（IP、规格、机房）
- [ ] 域名 / 证书 / HTTPS（确认 `GEO_SECURE_COOKIE=true`）
- [ ] 各平台运营账号清单与归属（账号登录态在 `browser_states/`）
- [ ] 备份 cron 正在运行且已验证可恢复（`DEPLOYMENT.md §7`）

### 2.4 知识
- [ ] 头条自动化选择器现状（平台 DOM 会变，见 `CLAUDE.md → Toutiao`）
- [ ] 并发/单 worker 约束、账号锁 finally 规则等关键约束（见 [03](./03-technical-architecture.md) / [06](./06-development-guide.md)）

---

## 3. "出事先看这里"

| 场景 | 去哪 |
|------|------|
| 部署/重启/备份恢复 | [07 部署运维](./07-deployment-operations.md) + `DEPLOYMENT.md` |
| 某接口怎么调 | [05 API 接口](./05-api-reference.md) 或 `/docs` |
| 发布卡住/状态含义 | [02 §5 状态机](./02-product-design.md) + [03 §5 执行引擎](./03-technical-architecture.md) |
| 加平台/加模块 | [06 开发指南 §4/§5](./06-development-guide.md) |
| 表结构 | [04 数据库设计](./04-database-design.md) |
| 踩坑 | `CLAUDE.md → Gotchas` + [06 §6](./06-development-guide.md) |

---

## 4. 接手第一周建议

1. **Day 1**：跑通本地开发（[06](./06-development-guide.md)）：起 MySQL → `alembic upgrade head` → uvicorn + Vite，登录看 UI。
2. **Day 2**：在 staging 跑一次完整发布（建任务 → execute → 看 SSE/记录），体验人工接管闭环。
3. **Day 3**：读 `tasks/executor.py` + `runner.py` + `drivers/toutiao.py`（系统最复杂处）。
4. **Day 4**：跑测试（[08](./08-testing.md)），改一个小 bug 走通 PR + CI。
5. **Day 5**：通读 AI 生文模块（[09](./09-ai-capability-research.md) + `AI_GENERATION.md`）。

---

## 5. 交接确认

- [ ] 交接人已演示：本地开发、一次完整发布、备份恢复
- [ ] 接手人已独立完成：起服务、跑测试、提一个 PR
- [ ] 凭据已安全移交并确认可用
- [ ] 关键约束与运维流程已对齐

---

> 文档集结束。返回 [00 索引](./README.md)。
