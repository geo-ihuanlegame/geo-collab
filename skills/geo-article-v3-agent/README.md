# geo-article-v3-agent

这是基于 `geo-article-v2` 改写的工程化版本。

## 与 v2 的主要差异

- 把长流程改成状态机：`INTAKE -> SELECT_STRUCTURE -> DIFFERENCE_CHECK -> SELECT_COMPANIONS -> CONFIRM_COMPANIONS -> WRITE_ARTICLE -> VALIDATE -> LOG_AND_PERSIST -> STOP`
- 把重复规则收敛为单一真源，降低不同文件互相冲突的风险。
- 强化停止点：陪衬未确认不写，写完必须停。
- 增加 `CHECKS.md`，让输出前校验可执行。
- 保留原有 `references/`、`skeletons/`、`assets/` 业务资产，不重写历史沉淀。

## 推荐用法

把本目录作为独立 skill 使用。原 `geo-article-v2` 保留作为业务经验版；本目录作为 agent 执行版。

## 维护原则

- 产品事实只改 `references/product-knowledge.md`。
- 陪衬库只改 `references/companion-pool.md`。
- 新结构只加到 `skeletons/variants/`，并同步 `skeletons/_INDEX.md`。
- 每次写完必须更新 `assets/article-plan.md`。
