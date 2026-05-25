# AI 生文模块需求文档

## 功能定位

在现有 Geo 协作平台中新增一个独立的 AI 生文入口，用户选择 Skill（写作指令包）和 Prompt（提示词模板）后，由 AI 自动生成一批文章并直接写入文章管理，供后续人工审核和发布。

**不在此模块范围内：** Agent 对话、文章重新生成（生成完成即定稿）。

---

## 交互需求

### 导航

- 侧边栏新增「AI 生文」单行导航项，位置在「内容」上方
- 进入后顶部有两个 Tab：**一键生成** | **技能与提示词**

### Tab 1：一键生成

**左侧配置区**

| 元素 | 说明 |
|------|------|
| Skill 选择器 | 下拉，仅显示「已启用」的 Skill |
| Prompt 选择器 | 下拉，仅显示「已启用」的 Prompt |
| 补充说明 | 可选 textarea，追加到生成指令末尾 |
| [生成] 按钮 | 触发生成；生成中禁用 |
| [清空] 按钮 | 有结果时显示，清空配置和结果，开启新会话 |

**右侧结果区（三种状态）**

1. **空状态**：引导文字
2. **生成中**：进度条 + "正在生成…" + 骨架卡片占位
3. **完成**：文章卡片列表，每张卡片含：
   - 标题（可内联编辑）
   - 正文摘要（可内联编辑）
   - [在文章管理中打开 ↗]（新标签页）
   - 「已保存」badge

**关键行为**

- 生成的文章直接写入文章管理，无需用户手动保存
- 无重新生成功能；需要重来则点「清空」重新配置
- 内联编辑只修改 demo 展示内容，真正编辑需跳转文章管理页
- 跳转文章管理后回来，结果仍保留（不清空）

### Tab 2：技能与提示词

两列并排布局。

**左列：技能库（Skill）**

- 搜索框（前端过滤）
- Skill 卡片：名称 + 描述摘要 + 文件数统计 + 启用/停用 toggle + [删除]
- 停用的 Skill 灰色显示，且从「一键生成」的选择器中移除
- [＋ 拖拽文件夹导入] 按钮

**Skill 导入流程（Modal）**
1. 用户拖拽整个 Skill 文件夹到左列
2. Modal 显示模拟上传进度
3. 解析 `SKILL.md` frontmatter，展示：名称、描述、文件结构摘要（references/N个、skeletons/N个、assets/N个）
4. [取消] / [确认导入] → 成功后卡片出现，默认启用

**右列：提示词库（Prompt）**

- 搜索框（前端过滤）
- Prompt 卡片：名称 + 内容摘要（`{{参数}}` 高亮）+ 启用/停用 toggle + [编辑][删除]
- 停用的 Prompt 灰色显示，且从「一键生成」的选择器中移除
- [＋ 新建提示词] 按钮（触发编辑 Modal，含名称 + 正文编辑器 + [保存]）

**Skill 与 Prompt 的关系**

两者是并列独立资产，在生成时组合使用，无从属关系。Skill 提供写作知识和骨架，Prompt 提供用户的具体写作指令。

---

## API 需求

### Skills

```
GET    /api/skills                     列表（含启用状态）
POST   /api/skills                     上传 Skill 文件夹（multipart）
PATCH  /api/skills/{id}                更新启用状态 / 元数据
DELETE /api/skills/{id}                删除
```

### Prompt Templates

```
GET    /api/prompt-templates           列表（含启用状态）
POST   /api/prompt-templates           新建
PUT    /api/prompt-templates/{id}      编辑
PATCH  /api/prompt-templates/{id}      更新启用状态
DELETE /api/prompt-templates/{id}      删除
```

### 生成会话

```
POST   /api/generation/sessions        发起生成（异步，返回 session_id）
GET    /api/generation/sessions/{id}   查询进度和结果
```

---

## 数据模型

### skills 表（新增）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | int PK | |
| name | str | SKILL.md frontmatter |
| description | str | SKILL.md frontmatter |
| storage_path | str | 服务器上的文件夹路径 |
| file_stats | JSON | `{"references": N, "skeletons": N, "assets": N}` |
| is_enabled | bool | 默认 true |
| created_at | datetime | |

### prompt_templates 表（新增）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | int PK | |
| name | str | 用户命名 |
| content | text | 提示词正文，支持 `{{参数}}` 占位符 |
| is_enabled | bool | 默认 true |
| created_at | datetime | |
| updated_at | datetime | |

### generation_sessions 表（新增）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | int PK | |
| skill_id | int FK | |
| prompt_template_id | int FK | |
| extra_instruction | text nullable | 补充说明 |
| status | enum | `pending / running / done / failed` |
| article_ids | JSON | 生成成功的文章 ID 数组 |
| error_message | text nullable | 失败原因 |
| created_at | datetime | |
| completed_at | datetime nullable | |

### articles 表（零改动）

生成的文章直接 `INSERT` 进现有表，`client_request_id` 保证幂等。

---

## 技术约束

- **模型调用**：全部通过 LiteLLM，禁止直接使用 `anthropic` / `openai` SDK
- **流程编排**：LangGraph 两阶段架构
  - 规划 Agent（顺序）：读取 Skill 共享文件，输出 N 份写作任务规格
  - 写作 Agent × N（并发，max_workers=4）：各自独立执行，调用 `save_article` tool 写库
- **格式转换**：AI 输出 Markdown，后端转换为 Tiptap JSON + HTML
  - 新增 `server/app/modules/ai_generation/converter.py`
  - `markdown_to_tiptap(md: str) -> dict`
  - `markdown_to_html(md: str) -> str`（用 python-markdown）
- **并发安全**：每篇文章独立 INSERT，`client_request_id` 幂等，无共享槽位冲突

---

## 产品路线图

| 阶段 | 内容 | 状态 |
|------|------|------|
| P1 | Skill + Prompt 一键批量生文 | 开发中 |
| P1 | 自动拆分主标题 / 副标题 | 待排期 |
| P1.5 | 发布后采集标题链接到飞书表格 | 待排期 |
| P2 | 图片库 + 文章自动配图 | 待排期 |
| P3 | 每日定时生文（复用 worker/executor.py） | 待排期 |
| P3 | 问题库生文 | 待排期 |
| P4 | 渠道后台可扩展 | 待排期 |

---

## 交互 Demo

`ai-generation-demo.html`（根目录），直接浏览器打开，覆盖一键生成、技能库、提示词库三个模块，用于需求对齐和设计验证。
