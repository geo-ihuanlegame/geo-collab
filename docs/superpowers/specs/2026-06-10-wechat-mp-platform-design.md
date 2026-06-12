# 微信公众号平台接入（草稿箱发布）设计

- 日期：2026-06-10
- 状态：已与需求方逐段确认
- 范围：仅后端（前端「媒体矩阵」改版在另一窗口并行，本设计为其提供 API 依赖）
- 来源：`E:\agent_study\wechat_spike` 验证脚本（单图文草稿闭环）+ 产品交互稿 `GEO(2).PEN`（媒体矩阵系列画板）

## 1. 目标与边界

把微信公众号登记为继头条之后的第二个发布平台，完整融入现有
Platform / Account / PublishTask / PublishRecord / worker / pipeline distribute 体系。

明确定下的边界：

| 维度 | 决定 |
|---|---|
| 链路终点 | **仅草稿箱**：`draft/add` 成功即整条发布记录完成；不调 `freepublish/*`（2025-07 起个人主体/未认证企业号该接口权限已被微信回收） |
| 融入深度 | 完整进任务体系，pipeline `distribute`、审核门禁、round-robin 派号、发布记录页自动可用 |
| 凭据存储 | `accounts` 表通用 JSON 字段；AppSecret 永不回传原文；token 缓存进 DB 跨进程共享 |
| 图片 | 封面自动转 JPG 压至 ≤64KB；正文图自动压至 ≤1MB 并转传微信换 URL |
| 微信图文字段 | 全自动推导（digest/author 留空、评论关），不做配置入口（交互稿中无此配置） |
| 连带范围 | 通用账号字段：`distribution_enabled`（分发开关，影响自动派号）、`contact`（绑定联系方式）、`avatar_asset_id`（头像），与微信凭据同一迁移落地 |
| 授权交互 | 「添加账号」第 2 步 = 后端实调微信 token 接口验证凭据 → 授权成功/失败 |

非目标：定时群发、多图文（一条草稿多篇 article）、`freepublish` 自动发布、
素材库管理 UI、微信侧数据回流（阅读量等）。

## 2. 架构选型（已确认：方案 A）

驱动注册表仍是发布的唯一入口，注册表扩出「API 驱动」一类：

- 微信驱动声明类属性 `mode = "api"`；无此属性的驱动默认浏览器型，行为零变化。
- `build_publish_runner_for_record` 在解析 `state_path` 之前先判驱动类型：
  - 浏览器驱动：维持现状（state_path 解析平台码 → 浏览器会话 → `driver.publish(page, context, ...)`）。
  - API 驱动：平台码取自 `record.platform.code`，**全程不起浏览器**、不碰
    Xvfb / noVNC / profile 锁；每账号串行锁、全局并发闸、重试、TaskLog、诊断截图以外的
    横切逻辑全部复用。
- 备选方案 B（executor 按平台类型绕开注册表、独立 publisher 模块）被否：会出现两条平行发布
  代码路径，重试/诊断等横切逻辑重复或漏掉，第三个 API 平台进来时仍要回到方案 A 的抽象。

收益：百家号等未来 API 平台是纯增量；`GEO_<PLATFORM>_DRIVER` 变体灰度机制天然可用；
微信发布不需要浏览器，Windows 本地与 CI 都能跑全链路。

## 3. 数据模型与迁移（一次迁移）

`accounts` 新增：

| 字段 | 类型 | 说明 |
|---|---|---|
| `api_credentials` | JSON，可空 | API 型平台凭据；微信存 `{"app_id": "...", "app_secret": "..."}`；未来平台同字段不同 key |
| `api_token_cache` | JSON，可空 | `{"access_token": "...", "expires_at": <epoch>}`；独立列避免凭据更新与 token 刷新互相覆盖；web（验证）与 worker（发布）共享，沿用 spike 的 300 秒提前刷新窗口 |
| `distribution_enabled` | Boolean，非空，默认/server_default true | 全平台通用「分发」开关；交互稿：授权成功即默认启用 |
| `contact` | String(200)，可空 | 绑定联系方式（手机号/QQ，号失效时联系负责人） |
| `avatar_asset_id` | FK → assets.id，可空 | 账号头像，复用现有 assets 上传链路 |
| `state_path` | **改可空** | API 账号无 Playwright 状态文件；浏览器账号语义不变 |

`platforms`：迁移内幂等 INSERT `code='wechat_mp'`（名称「微信公众号」）。
**不加 kind 列**——平台类型由驱动对象自身声明。

约定：微信账号 `platform_user_id` 存 AppID，天然吃进现有
`uq_accounts_platform_user` 唯一约束，同一公众号不会被同一用户重复登记。

`publish_records` 零改动：草稿无 URL，`publish_url` 留空，`media_id` 写入结果
message 与 TaskLog。

凭据以明文 JSON 入库（与现有 storage_state cookie 明文落盘同一安全等级；DB 访问已受控）。

## 4. 账号 API

- `AccountCreate` / `AccountUpdate` 新增 `api_credentials`、`contact`、`avatar_asset_id`、
  `distribution_enabled`；平台为 `wechat_mp` 时校验 `app_id` / `app_secret` 必填，
  不要求 state_path。
- `AccountRead` **绝不回传 `api_credentials` 原文**；新增只读摘要字段：AppID 明文 +
  AppSecret 尾 4 位（对齐交互稿掩码 `••••••3a7f`）。
- PATCH 带新 secret 即整体覆盖 `api_credentials`，不带则不动（注意现有
  「`exclude_unset` + service 过滤 None」的 PATCH 语义，凭据按整对象替换，不做字段级合并）。
- 新端点 `POST /api/accounts/{id}/verify-credentials`（`@limiter` 限流）：
  强制刷新调 `GET /cgi-bin/token`；成功 → `status='valid'`、`last_checked_at` 更新、
  token 写入 `api_token_cache`；失败 → `status='expired'`，微信 errcode/errmsg 原样透出，
  `40164` 附加「将服务器出口 IP 加入公众平台 IP 白名单」提示。
  这是交互稿「前往授权」第 2 步（授权成功/失败弹窗）的后端实现。
- 微信账号不走 login-session / noVNC 流；对 API 型平台调用登录会话端点返回 400
  （`ValidationError`）。
- 分发开关语义：pipeline `distribute` 自动派号路径过滤 `distribution_enabled=false`
  的账号；可用账号被全部过滤时安静跳过（对齐空 `article_ids` 行为）。手动建任务不强拦，
  前端自行展示停用态。

## 5. 微信客户端与驱动

新文件：

- `server/app/modules/tasks/drivers/wechat_client.py` —— spike 逻辑移植为纯函数集，
  **httpx 同步 client**（项目既有依赖，不引 requests）：token 获取/解析、thumb 素材上传、
  `uploadimg` 正文图上传、`draft/add`、`WeChatApiError`（携带 errcode/payload）。
  不碰 ORM、不读环境变量，凭据/token 全由参数传入。
- `server/app/modules/tasks/drivers/wechat_mp.py` —— 薄驱动，`code='wechat_mp'`、
  `mode='api'`，实现 `publish_api(payload) -> PublishResult`；`main.py` 顶部加一行
  import 触发注册。

token 的 DB 读写留在 runner 侧（驱动不碰 ORM 的铁律不破）：发布前 runner
读 `api_token_cache` → 过期则经 client 刷新 → 写回 → 把有效 `access_token` 放入 payload。
微信 token 有效期 2 小时，单次发布分钟级，中途过期不构成风险。

新增 `ApiPublishPayload` dataclass（与 `PublishPayload` 并列）：`title`、
`body_segments`、`cover_asset_path`、`display_name`、`platform_code`、`access_token`、
`credentials_summary`（仅诊断用，不含 secret）。

## 6. 图片与内容管线（驱动内纯函数，Pillow）

- 封面：读 `cover_asset_path` → Pillow 转 RGB JPEG → 迭代降质 + 等比缩边直到 ≤64KB →
  上传 thumb 素材 → `thumb_media_id`。无封面时回落正文第一张图；
  一张图都没有 → `PublishError("公众号草稿需要封面图")`（与头条封面必填同语义）。
- 正文：从 `body_segments` 重组 HTML（不对 `content_html` 做正则替换）：
  image 段压至 ≤1MB（保持 JPEG/PNG）→ `uploadimg` 换微信 URL；text 段照排。
  外链图会被微信过滤，因此所有本站图必须转传。
- 微信 article 字段：`digest` 留空（微信自动取正文前 54 字）、`author` 留空、
  `need_open_comment=0`、`only_fans_can_comment=0`、`content_source_url` 留空。

## 7. 状态映射与错误处理

- `draft/add` 成功 → record `succeeded`；`publish_url=None`；message 含 `media_id`。
- `stop_before_publish` 对微信是 no-op：草稿箱本身就是「停在发布前」，
  不会出现 `waiting_manual_publish` / `waiting_user_input`，也绝不抛 `UserInputRequired`。
- `WeChatApiError` → `PublishError(errcode + errmsg)` → `failed`，复用现有重试。
- `40001`（secret 失效）best-effort 将账号 `status` 置 `expired`；`40164` 文案附 IP 白名单提示。
- 上游网络错误（连接/超时）→ `PublishError`，文案区分「微信接口不可达」。

## 8. 测试

- `test_wechat_client.py`：`httpx.MockTransport` 打桩——token 刷新与缓存命中、
  errcode 映射、thumb / uploadimg / draft 三个请求的参数与错误路径；无 DB。
- 图片压缩纯函数：Pillow 现场生成测试图，验证 64KB / 1MB 边界与格式转换。
- `test_accounts_api.py` 扩展：微信账号创建校验（缺凭据 400）、secret 掩码回传、
  `verify-credentials` 成功/失败（打桩 client）、PATCH 覆盖 secret、
  state_path 可空兼容。
- runner 分叉：stub 驱动 monkeypatch 模式验证 API 驱动路径的 record 状态流转、
  不触浏览器代码。
- `test_auto_distribute.py` 扩展：`distribution_enabled=false` 账号被自动派号过滤、
  全部停用时安静跳过。

## 9. 部署与运维注意

- 微信接口有 **IP 白名单**：服务器出口公网 IP 必须加入公众平台后台白名单，
  否则 token 接口报 `40164`。`verify-credentials` 的错误透传即为此项的自助诊断入口。
- access_token 全局唯一：同一 AppID 在别处（如旧 spike 脚本）刷新 token 会使 DB 缓存失效，
  下次发布会自动重刷，仅多一次请求，无需处理。
- 发布 worker 单实例约束不变；微信发布不依赖 Xvfb，本地 Windows / CI 可全链路运行。
