# 08 · 测试文档

| 项 | 内容 |
|----|------|
| 框架 | pytest（后端）、tsc/eslint（前端类型与静态检查） |
| 关联文档 | [06 开发指南](./06-development-guide.md) · [03 技术架构](./03-technical-architecture.md) |

> 本项目以**后端集成测试**为主：用真实 MySQL 测试库 + `TestClient` 端到端验证 API 与状态机。前端通过 `tsc`（strict）类型检查 + eslint 保证质量。

---

## 1. 测试策略

| 层 | 手段 | 说明 |
|----|------|------|
| API / 集成 | pytest + FastAPI `TestClient` + 真实 MySQL | 主力；覆盖路由、service、状态机、迁移、FTS |
| 并发 / 线程 | pytest（确定性化） | 并发发布、worker 抢占、账号锁 |
| 浏览器自动化 | 单元化 + stub | 驱动选择器/payload 用单测；真实 Playwright 不进 CI |
| 前端 | `tsc -b`（strict）+ eslint | 类型与静态检查 |

**为何用真实 MySQL 而非 SQLite**：项目依赖 MySQL 专属特性（`FULLTEXT ... WITH PARSER ngram`、TEXT 列约束、外键行为），SQLite 无法等价，故放弃 SQLite 测试便利换取与生产一致（见 [03 §9 设计决策](./03-technical-architecture.md)）。

---

## 2. 运行测试

```bash
# 全量（需测试库；DB 名必须含 "test"）
GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test pytest server/tests/ -q

# 单文件 / 单用例 / 关键字
pytest server/tests/test_assets_api.py -q -k chunked
pytest server/tests/test_articles_api.py::test_function_name -q

# CI 同款（超时保护 + 覆盖率）
pytest server/tests/ -q --timeout=120 --cov=server/app --cov-report=term-missing
```

- **未设 `GEO_TEST_DATABASE_URL`**：`conftest.py` 自动给 `@pytest.mark.mysql` 用例打 skip —— 裸跑 `pytest` 只跑无 DB 用例。
- **安全闸**：测试库名必须含 `test`，否则 `utils.get_test_database_url()` 拒绝运行（确需绕过用 `GEO_ALLOW_NON_TEST_DATABASE_FOR_TESTS=1`）。

---

## 3. 测试夹具（`server/tests/utils.py`）

核心是 `build_test_app(monkeypatch)`，每个集成测试用它拿一个隔离的 app：

它会：
1. 设 `GEO_DATA_DIR`（临时目录）、`GEO_DATABASE_URL`（测试库）、`GEO_JWT_SECRET=test-secret`，并 `get_settings.cache_clear()`。
2. **重建一次性 schema**：drop 所有表 + `alembic_version` → `create_all` → 手动加 `articles` 的 ngram FULLTEXT 索引。
3. 重置跨测试可能泄漏的全局态：tasks 的 `_task_locks`/`_account_locks`/`_task_cancel`、`browser._reset_globals()`、security 用户缓存。
4. 把 `bg_session_factory` 与 `SessionLocal` monkeypatch 到 `TestingSessionLocal`（让后台线程也用测试库）。
5. 建一个 `testadmin`（admin，`must_change_password=False`），把 JWT 写进 client cookie。
6. 返回 `TestApp{client, data_dir, session_factory, engine}`。

**纪律**：
- 用 `build_test_app` 的测试必须在 `finally` 里 `test_app.cleanup()`（drop schema、dispose engine、删临时目录、清配置缓存）。
- 执行任务的测试要传 `"stop_before_publish": false`，否则记录停在 `waiting_manual_publish`。
- Mock 发布运行器：`monkeypatch.setattr("server.app.modules.tasks.executor.build_publish_runner_for_record", lambda r: stub_runner)`。

---

## 4. 测试覆盖矩阵（`server/tests/`，约 34 个测试文件）

| 域 | 测试文件 | 覆盖点 |
|----|----------|--------|
| 鉴权 / 用户 | `test_admin_users`、`test_security_boundaries` | 登录、用户管理、权限边界、user_id 隔离 |
| 账号 | `test_accounts_api`、`test_accounts_import_export`、`test_browser_sessions` | 账号 CRUD、授权导入导出、浏览器会话 |
| 文章 | `test_articles_api`、`test_article_groups_api`、`test_articles_published_count`、`test_tiptap_parser` | 文章 CRUD、分组、发布计数、Tiptap 解析 |
| 资源 | `test_assets_api`（含 `-k chunked`） | 小文件 + 分块上传、限制、415 处理 |
| 任务 / 发布 | `test_tasks_api`、`test_tasks_state_machine`、`test_publish_payload`、`test_publish_runner`、`test_publish_validation`、`test_concurrent_publish`、`test_worker_executor` | 任务 CRUD、状态机、payload 构建、运行器、校验、并发、worker 抢占/租约 |
| 驱动 | `test_drivers`、`test_toutiao_group_paragraphs` | 驱动注册/选择、头条段落处理 |
| AI 排版 | `test_ai_format`、`test_ai_format_error_messages` | 标题识别、错误信息分类 |
| AI 生文 | `test_question_bank` | 问题库同步/消费、选题 |
| 图片库 | `test_image_library_inserter` | 配图插入 |
| 技能 / 模板 | `test_skills_api`、`test_prompt_templates_api` | CRUD、scope、系统模板权限 |
| 审计 | `test_audit_log_api` | 审计写入、过滤、游标分页、脱敏 |
| 系统 | `test_system_status` | 系统状态计数与健康 |
| 搜索 / 迁移 | `test_search`、`test_fts_and_migrations` | ngram 全文检索、迁移正确性 |
| 通用 | `test_models`、`test_time_serialization`、`test_feishu`、`test_phase4` | 模型、UTC 时间序列化、飞书、阶段性回归 |

> 重点子系统（任务发布、状态机、并发、迁移/FTS）有专门测试，符合考核"交付质量稳定、无重大返工"的要求。

---

## 5. CI 门禁（`.github/workflows/ci.yml`）

每次 push 到 `main` 和所有 PR 触发，并发取消旧跑。

| Job | 步骤 | 门禁 |
|-----|------|------|
| backend | `ruff check` / `ruff format --check` / `mypy` | 非阻塞（`continue-on-error`） |
| backend | **pytest**（mysql:8.0 service，库 `geo_test`，`--timeout=120 --cov`） | **硬门禁** |
| frontend | `eslint` | 非阻塞 |
| frontend | **typecheck**（`tsc -b`）+ **build**（`vite build`） | **硬门禁** |

- **测试 + 类型 + 构建是硬门禁**；lint / format / mypy / eslint 当前为非阻塞步骤（`continue-on-error`）。
- CI 用 `PYTHONUTF8=1` 统一 UTF-8（输出含中文）。
- 配合分支保护把 CI 设为 `main` 的 required check，即可"红了不准 merge"。

---

> 下一篇：[09 AI 能力研究与实践](./09-ai-capability-research.md)。
