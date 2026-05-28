# 飞书问题库 · 服务器端联调清单

> 目的：在服务器上验证「飞书多维表 → 问题库（QuestionPool）→ pending items」整条链路打通。
> 适用模块：`server/app/modules/ai_generation/question_bank.py` + `server/app/shared/feishu_bitable.py`

---

## 1. 前置条件

### 1.1 应用侧（飞书开放平台后台）

| 项 | 要求 |
| --- | --- |
| 应用类型 | 自建应用（企业自建） |
| 权限 scope | 至少包含「读取多维表」相关：`base:app:read`、`base:record:retrieve`、`base:field:read`（旧命名 `bitable:app:readonly` 也可） |
| **版本发布** | ⚠️ 改完权限必须在「版本管理与发布」里**创建版本并发布**，否则只是申请未生效 |

### 1.2 文档侧（每张要读的多维表）

⚠️ 这一步最容易漏，单独列出来：

打开目标多维表 → 右上角 **…** 或 **分享** → **添加文档应用**（不是"添加成员"）→ 搜索你的应用名 → 给「**可阅读**」或以上权限。

> scope 决定"应用能调哪些 API"；文档协作者决定"应用能读哪些具体表"。两者缺一不可。

### 1.3 服务器环境变量

`.env` 里至少要有：

```bash
GEO_FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
GEO_FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

注意 `=` 后**不要有前导空格**（pydantic-settings 不会自动 trim，飞书侧会返回 `app_id invalid`）。

改完 `.env` 后重启服务：

```bash
docker-compose restart app
```

---

## 2. 多维表字段约定

代码里硬编码了两个字段名（见 `question_bank.py:22-23`）：

| 代码常量 | 飞书表里的列名 | 用途 |
| --- | --- | --- |
| `FIELD_QUESTION` | **提问词** | 生文真正用的问题文本，写到 `QuestionItem.question_text` |
| `FIELD_CATEGORY` | **分类板块** | 自动模式按板块轮转抽题，写到 `QuestionItem.category` |

> 表头列名必须**完全一致**（包括是否有空格、是否半角/全角）。其它列会原样存进 `QuestionItem.fields` JSON 备用，不影响主流程。

---

## 3. 测试步骤（走 API，不依赖前端）

假设服务地址 `http://localhost:8000`，已登录拿到 cookie。下面用 `curl` 演示，把 `<cookie>` 换成实际的 `access_token`，把 `<app_token>` / `<table_id>` 换成目标表。

### 3.1 创建问题池

```bash
curl -X POST http://localhost:8000/api/generation/question-pools \
  -H "Content-Type: application/json" \
  -H "Cookie: access_token=<cookie>" \
  -d '{
    "name": "测试问题池",
    "feishu_app_token": "<app_token>",
    "feishu_table_id": "<table_id>"
  }'
```

✅ 期望 `201`，返回 `{"id": <pool_id>, "pending_count": 0, ...}`。

### 3.2 触发同步

```bash
curl -X POST http://localhost:8000/api/generation/question-pools/<pool_id>/sync \
  -H "Cookie: access_token=<cookie>"
```

✅ 期望 `200`：

```json
{
  "total": 50,
  "added": 50,
  "updated": 0,
  "skipped_consumed": 0
}
```

- `total` = 飞书表里拉到的总行数
- `added` = 首次新增的行
- `updated` = 已存在且仍 pending 时刷新内容
- `skipped_consumed` = 已 `consumed` 不复活的行（再同步也不会被拉回 pending）

### 3.3 验证 pending items

```bash
curl http://localhost:8000/api/generation/question-pools/<pool_id>/items \
  -H "Cookie: access_token=<cookie>"
```

✅ 期望返回数组，每条至少有：

```json
{
  "id": 1,
  "record_id": "recvjt8g4hO4kV",
  "question_text": "推荐一款好玩的游戏",
  "category": "综合通用推荐",
  "status": "pending"
}
```

- `question_text` 非空 = 「提问词」列读到了
- `category` 非空 = 「分类板块」列读到了
- `status="pending"` = 进入可消费队列

---

## 4. 常见错误对照表

| 现象 | 错误码 / msg | 真正原因 | 解决 |
| --- | --- | --- | --- |
| `飞书 HTTP 400` + `app_id invalid` | — | `.env` 里 `GEO_FEISHU_APP_ID=` 后有前导空格 | 去掉空格，重启 |
| `获取 tenant_access_token 失败 code=99991663` | — | app_secret 错或未发布版本 | 后台核对 secret，确认版本已发布 |
| `读取多维表失败 code=1254302 msg=RolePermNotAllow` | 1254302 | **应用未被加为该表的文档协作者** | 在表的"分享"里添加文档应用并给"可阅读"权限 |
| `code=91402 NOTEXIST` | 91402 | app_token 或 table_id 写错 | 重新从表 URL 复制 |
| 同步成功但 `question_text` 全空 | — | 表头列名不是「提问词」（多了空格 / 写成"问题词"等） | 把列名改成「提问词」或改 `question_bank.py:22` |
| 同步成功但 `total=0` | — | 表里没数据，或视图过滤了所有行 | 检查源表 |

---

## 5. 本次测试样例

下面这张表已在开发环境验证通过，可用于服务器侧 smoke test：

- `app_token`: `QdyvbWq3Ya7QeNs8m2qccxmYnZd`
- `table_id`: `tblWL03XsDL9Bp5J`
- 期望：`total=50`，`question_text` 全部非空（如「推荐一款好玩的游戏」「TapTap 游戏推荐」），`category` 含「综合通用推荐」「无广告 / 不肝不氪 / 免费良心类」等。

> 服务器上的应用如果不是开发环境同一个 `cli_aa9d104beaba5bb4`，需要重新把服务器应用加为这张表的文档协作者。

---

## 6. 同步语义备忘（避免误判）

- 同步是 **upsert**，主键是 `(pool_id, record_id)`。
- 已 `consumed` 的行**不复活**：即使飞书把它改了，同步也只是 `skipped_consumed +=1`，不会拉回 pending。如果要让它重新参与生文，先在 DB 里把 `status` 改回 `pending`。
- `question_text` / `category` 在 pending 状态下每次同步都会被刷新；其它列原样进 `fields` JSON 备查。
- 自动模式（板块轮转抽题）**不出队**，同一行可被多批次复用；手动模式才会 `mark_consumed`。
