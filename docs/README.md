# Geo 协作平台 · 文档中心

> 多平台内容自动化发布平台的全部文档。面向人的体系化文档在 [`project/`](./project/)，开发过程的计划与设计稿归档在 [`plans/`](./plans/)、[`specs/`](./specs/)，调研分析在 [`analysis/`](./analysis/)。
>
> 开发约定与命令的唯一事实源是仓库根 [`CLAUDE.md`](../CLAUDE.md)，本目录是面向人的补全，不重复底层细节。

## 目录结构

| 目录 | 内容 | 适合谁看 |
|------|------|----------|
| [`project/`](./project/) | **项目文档集**（需求 / 产品 / 架构 / 数据库 / API / 开发 / 部署 / 测试 / AI 实践 / 交接，共 11 篇） | 汇报、评审、新成员、交接 ⭐ |
| [`plans/`](./plans/) | 各功能的实现计划（按日期 / 主题） | 开发者、回顾迭代过程 |
| [`specs/`](./specs/) | 设计稿 / 技术方案 | 开发者、设计评审 |
| [`analysis/`](./analysis/) | 调研与差距分析（geo-collab vs geo-full、飞书问题库测试） | 产品、规划 |

## 工作汇报入口

从 [`project/00-README.md`](./project/00-README.md) 开始 —— 那里有完整的文档导航与建议阅读顺序：

- **第一次了解项目**：[01 需求](./project/01-requirements-analysis.md) → [02 产品设计](./project/02-product-design.md) → [03 技术架构](./project/03-technical-architecture.md)
- **评审 / 汇报**：[01 需求](./project/01-requirements-analysis.md) → [03 架构](./project/03-technical-architecture.md) → [09 AI 实践](./project/09-ai-capability-research.md) → [10 交接 Runbook](./project/10-handover-runbook.md)
- **上手开发**：[03 架构](./project/03-technical-architecture.md) → [06 开发指南](./project/06-development-guide.md) → [04 数据库](./project/04-database-design.md) → [05 API](./project/05-api-reference.md)
- **上线 / 运维**：[07 部署与运维](./project/07-deployment-operations.md) → [10 交接 Runbook](./project/10-handover-runbook.md)

## 实现计划索引（`plans/`）

| 日期 | 主题 |
|------|------|
| 2026-05-21 | [AI 格式调整](./plans/2026-05-21-ai-format-adjustment.md) · [头条富格式](./plans/2026-05-21-toutiao-rich-format.md) |
| 2026-05-26 | [前端多图库分类](./plans/2026-05-26-frontend-multi-stock-category.md) · [图库 UI 升级](./plans/2026-05-26-image-library-ui-upgrade.md) · [用户测试修复](./plans/2026-05-26-user-testing-fixes.md) |
| 2026-06-02 | [头条页内适配器](./plans/2026-06-02-toutiao-inpage-adapter.md) · [页内适配器 M2](./plans/2026-06-02-toutiao-inpage-adapter-m2.md) |
| 2026-06（已合入 #13） | [问题池 / 方案池重构](./plans/question-scheme-pipeline-plan.md) · [方案自动排版配图 + Skill 下线](./plans/scheme-autoformat-plan.md) |

## 设计稿索引（`specs/`）

- [图库 UI 升级设计](./specs/2026-05-26-image-library-ui-upgrade-design.md)
- [用户测试修复设计](./specs/2026-05-26-user-testing-fixes-design.md)
- [页内适配器发布设计](./specs/2026-06-02-inpage-adapter-publishing-design.md)
