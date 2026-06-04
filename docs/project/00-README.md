# Geo 协作平台 · 项目文档集

> 多平台内容自动化发布平台 —— 需求 / 设计 / 开发 / 部署 / 测试 / AI 实践 / 交接 全套文档。
>
> 所有结论均对照仓库实际代码核对（标注 `文件:行号`），如实描述系统已实现的能力。

---

## 一句话项目定义

运营在平台内写好（或用 AI 生成）文章 → 选定目标平台与账号 → 系统用 Playwright 自动登录、填写、发布；遇验证码 / 登录态失效时通过 noVNC 远程人工接管，处理完继续。

- **后端**：FastAPI + SQLAlchemy/Alembic（MySQL only）
- **前端**：React 19 + Vite + TypeScript + Tiptap
- **浏览器自动化**：Playwright + Xvfb / x11vnc / websockify / noVNC
- **AI 生文**：LiteLLM + LangGraph
- **对象存储**：MinIO（图片库）
- **部署**：Docker Compose（Linux 服务器）

---

## 文档导航

| # | 文档 | 读者 | 内容 |
|---|------|------|------|
| 00 | [README（本文）](./README.md) | 全员 | 文档索引、阅读顺序 |
| 01 | [需求分析](./01-requirements-analysis.md) | 产品 / 评审 / 新成员 | 背景痛点、用户画像、用户场景、价值假设、关键指标 |
| 02 | [产品设计](./02-product-design.md) | 产品 / 前端 / 测试 | 功能规格、信息架构、核心流程、状态机、角色权限 |
| 03 | [技术架构设计](./03-technical-architecture.md) | 后端 / 架构 | 系统架构、模块划分、并发模型、浏览器自动化、AI 管线、关键设计决策 |
| 04 | [数据库设计](./04-database-design.md) | 后端 / DBA | ER 关系、表结构、迁移、全文检索、软删除约定 |
| 05 | [API 接口文档](./05-api-reference.md) | 前后端 / 集成 | 全部端点：方法 / 路径 / 鉴权 / 参数 / 返回 / 错误码 |
| 06 | [开发指南](./06-development-guide.md) | 开发者 | 环境搭建、命令清单、代码规范、如何扩展平台驱动 / 模块 |
| 07 | [部署与运维](./07-deployment-operations.md) | 运维 / SRE | Docker Compose、配置项清单、备份恢复、监控、故障排查 |
| 08 | [测试文档](./08-testing.md) | 测试 / 开发 | 测试策略、覆盖、如何运行、CI 门禁 |
| 09 | [AI 能力与工程实践](./09-ai-capability-research.md) | 团队 / 评审 | AI 在产品与交付中的应用、选型结论 |
| 10 | [交接 Runbook](./10-handover-runbook.md) | 接手人 / 主管 | 系统速览、交接清单、上手路径 |

### 建议阅读顺序

- **第一次了解项目**：01 → 02 → 03
- **要动手开发**：03 → 06 → 04 → 05
- **要上线/运维**：07 → 10
- **评审**：01（需求）→ 03（设计）→ 09（AI 实践）→ 10（交接）

---

## 核心模块一览

| 模块 | 能力 |
|------|------|
| 文章 CRUD 与状态管理 | `draft / ready / archived` 状态，Tiptap 三份并行正文结构，软删除 |
| 多账号授权与续期 | 账号登录会话状态机 + noVNC 人工接管 + 续登 / 状态检查 |
| 批量发布 | 文章分组轮询（group round-robin），M 篇 × N 账号生成发布记录 |
| 人工接管 | `waiting_user_input`（验证码/失效）与 `waiting_manual_publish`（发布前确认）闭环 |
| 异常告警通知 | 任务终态飞书 webhook 推送；发布诊断事件落 TaskLog（含失败截图） |
| 操作日志（审计） | `AuditLog` + `/api/audit-logs`（admin），游标分页，敏感字段脱敏 |
| 系统状态总览 | `/api/system/status`：文章/账号/任务计数 + worker 在线 + 运行时就绪 |
| AI 生文 | LiteLLM + LangGraph 批量生成并入库，飞书多维表选题同步 |
| AI 智能排版 | 小标题识别 + 按图库分类自动配图 |
| 图片库 | MinIO 分桶存储 + 选图 / 配图 |

---

## 文档维护约定

- **唯一事实源**：开发约定与命令以仓库根 [`CLAUDE.md`](../../CLAUDE.md) 为准；本文档集是面向人的体系化补全，与 `CLAUDE.md` 相互引用而不重复底层细节。
- 文档随代码演进；新增/变更能力时同步更新对应文档。
