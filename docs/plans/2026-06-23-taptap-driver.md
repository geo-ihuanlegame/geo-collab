# TapTap 长帖发布驱动 —— 设计稿

> 日期：2026-06-23 · 分支：`taptap-driver`（基于 `origin/main` 8628f80，含凭据加密）
> 前置 spike：`E:\agent_study\taptap-spike\CONTRACT.md`（接口契约，已实测）
> 关联记忆：`project_taptap_publish_contract`、`project_publish_detached_orm_class`

## 实现状态（2026-06-23 全部完成，未提交，全量门禁绿）

发布主链路 + 三项收尾全部落地：
- **发布链路**：`taptap_contents.py`（转换器，12 测）+ `taptap_client.py`（五步 httpx）+ `taptap.py`（驱动，8 测）+ `base.py`/`runner_api.py`（payload + auth 分叉）+ bootstrap + alembic 0050。
- **① cookie 体检**：`tasks/taptap_health.py`（7 测）。探针端点从已有 HAR 挖到 = `GET account-profile/v1/me`（返回 `data.id`=VID），**D2 已解，无需再抓**。后台线程 `GEO_TAPTAP_COOKIE_CHECK_ENABLED`，main.py 接线；401→expired+飞书，瞬时错不翻状态。
- **② 登录**：发现登录流程**驱动无关**（`auth.py:1057` 用 `driver.detect_logged_in`/`extract_platform_user_id_async`/`home_url`，teardown 由 broker 管）→ TapTap 注册即接入。`home_url` 改 `/creator`（登录态留 /creator、未登录被重定向→detect 易判），`extract_platform_user_id_async` 页内 fetch `/me` 抽 VID。
- **③ 前端配置**：`EditAccountDialog` 加 TapTap 论坛表单（app_id/group_id/x_ua）+ `setTaptapForum` api + 后端 `PUT /accounts/{id}/taptap-forum`（`TaptapForumIn` / `set_taptap_forum`）。
- **关键解耦（实现中发现）**：`is_api_platform_code` 原 = `is_api_driver`，会把 TapTap 误判成「凭据直填」。改成「cookie-auth 的 API 驱动不算凭据直填」→ TapTap **前端走浏览器登录、后端走 API 发布**，两轴正确分离。
- **x_ua 三来源**：表单显式填 / `api_credentials.x_ua` / 由 `platform_user_id`(VID) 经 `build_x_ua` 合成（runner + 体检都有此 fallback）。

**仍需真机验证（无法在此离线确认）**：detect_logged_in 的登录页 DOM 启发式（首次真实 noVNC 登录后按需收紧）、/me 探针是否需严格 X-UA、端到端真发一帖。

---

## 0. 一句话

给 GEO 加 **TapTap 长帖（topic）发布**：做成 **cookie-session 型纯 HTTP 驱动**（`mode="api"`，不起浏览器发布），复用现有凭据加密 + API 驱动管线，新增"第三种 API 鉴权形态"（既非浏览器 state、也非 app_id/secret token，而是 **登录 cookie + X-XSRF-TOKEN + X-UA**）。

## 1. 已实测确认（spike，非推测）

- 发帖 = 纯 HTTP 五步，全在 `https://www.taptap.cn/webapiv2/`，body 为 `application/x-www-form-urlencoded`：
  1. `POST /moment-draft/v1/create-topic` → `data.moment_draft.id_str`（草稿 id，不公开）
  2. `POST /send-file/v1/image-upload-token`（`sdk=qiniu:3.3.3&type=moment`）→ 七牛 token
  3. `POST https://upload.qiniup.com/`（七牛标准 multipart：`token`+`file`）→ `{url, info}`
  4. `POST /moment-draft/v1/update-topic`（id + 完整 contents + image_infos）
  5. `POST /moment-draft/v1/publish-topic` → `data.moment.id_str`，公开链接 `taptap.cn/moment/<id>`
- 鉴权 = **cookie 会话 + `X-XSRF-TOKEN` 头（取自 `XSRF-TOKEN` cookie）+ `X-UA` 查询参数**。无 Authorization、无 secret。
- **零浏览器可行**：`replay_httpx.py` 用 storage_state 里的 cookie 纯 httpx 建草稿 HTTP 200（draft_id 6382565），抓后 75+ 分钟、跨独立进程仍有效。
- 正文 `contents` = Slate 风格 block，和 Tiptap 同构：paragraph / heading(level 1/2) / list(numbered/default + list-item) / image；行内叶子 `{text}`、`{text,bold:true}`、`{type:link,...}`。
- 长帖必带 `forum_bindings[].group_id`（论坛版块）、X-UA 里 `VID`（用户 id）。

## 2. 落点：GEO 集成点（均已读真实代码核实）

| 关注 | 现状（origin/main 8628f80） | TapTap 怎么接 |
|---|---|---|
| 驱动注册 | `drivers/bootstrap.py` 唯一注册点 | 加一行 `import ...drivers.taptap` |
| mode 分叉 | `executor.py:build_publish_runner_for_record` → `is_api_driver()`（看 `mode=="api"`）→ `runner_api.run_publish_api` | TapTap 设 `mode="api"` 自动走 API 路径 |
| 凭据加密 | `Account.api_credentials` / `api_token_cache` = `EncryptedJSON`（透明加解密）；`secret_files.read_state/write_state` 加密读写 storage_state | **零新加密**：cookie 罐走 `read_state`，论坛配置走 `api_credentials` |
| cookie 罐 | `Account.state_path`（storage_state.json 相对路径；API 账号现为 NULL） | TapTap **设 state_path**（混合体：API 发布 + 浏览器登录拿 cookie） |
| 前端平台选项 | `AccountsWorkspace.tsx:21` 已有 `{code:"taptap",label:"TapTap"}` 桩 | 复用；补论坛配置表单 |
| 迁移 head | `0049_encrypt_account_secret_columns` | 新增 `0050_seed_taptap_platform`（API 账号列 0044 已加，只需幂等插 platforms 行） |

### 核心改造点：`run_publish_api` 现写死 wechat token

`runner_api.run_publish_api` 当前**无条件** `_resolve_access_token(account.id)`（拉/缓存微信 token），并把 `access_token` 烤进 `ApiPublishPayload`。TapTap 无 token。需最小泛化：

- 在驱动上加类属性 **`auth`**：`"token"`（默认，wechat）/ `"cookie"`（taptap）。
- `run_publish_api` 按 `driver.auth` 分叉：
  - `token`：维持现状（resolve token → payload.access_token）。
  - `cookie`：**不拉 token**；`read_state(abs(state_path))` 得解密 cookie dict，`account.api_credentials` 得论坛配置，注入 payload。
- `ApiPublishPayload` 加可选字段（frozen dataclass 末尾加默认值，对 wechat 零回归）：
  - `access_token: str = ""`（改为有默认）
  - `state: dict | None = None`（解密后的 storage_state，含 cookies）
  - `forum: dict | None = None`（`{app_id, group_id, x_ua}`）

驱动仍 **ORM-free**：runner 读 DB/磁盘，注入纯数据（守 CLAUDE.md「驱动不碰 ORM」，见 `project_publish_detached_orm_class`）。

## 3. 数据落点 / 粒度（一账号 = 一罐 cookie + 一组论坛配置）

| 数据 | 存哪 | 加密 | 备注 |
|---|---|---|---|
| 登录 cookie | `state_path` → `browser_states/taptap/<account_key>/storage_state.json` | `write_state`/`read_state`（Fernet 透明） | noVNC 登录写入；含 XSRF-TOKEN cookie |
| X-XSRF-TOKEN | **不单独存** | — | 发请求时从 cookie 罐里的 `XSRF-TOKEN` cookie 现取（spike 已验证） |
| 论坛配置 | `api_credentials = {app_id, group_id, x_ua}` | `EncryptedJSON` 自动 | 一账号固定一个论坛 |
| VID（用户 id） | `platform_user_id`（兼存 x_ua 内） | 否（非密） | 唯一约束 (platform_id, platform_user_id) |
| token 缓存 | `api_token_cache` | — | **TapTap 不用** |

## 4. 论坛绑定：初版"不选论坛" = 每账号固定一个论坛

- 长帖（topic）**天生必带** `forum_bindings.group_id`，无"无论坛长帖"（那是动态/moment，另接口、未抓）。所以"不选论坛"= **不做发帖时的选择 UI**，而非"无论坛"。
- **不做下拉**（TapTap 板块太多，已与你确认不现实）。
- group_id / app_id 来源（按落地难度排）：
  1. **试点硬编码 / 一次性 PATCH**（MVP，最快）：建账号后 PATCH `api_credentials` 写死 `{app_id:43639, group_id:4444}`。
  2. **登录落地页捕获**（零手填，推荐 fast-follow）：noVNC 登录后编辑页 URL 自带 `app_id=…&group_id=…`，登录完成回调时解析存入 `api_credentials`。
- X-UA：**存登录时捕获的整串**（VID 之外基本是静态客户端描述符；spike 用捕获串 75min 后仍有效）。不在线合成，避免风控。

## 5. 内容保真：v2 吃 content_json 全保真（D1 已拍：2026-06-23 用户选 v2）

**背景（已核实）**：GEO 的 `parser.parse_body_segments` 是**扁平模型** —— `_append_segments` 把 `orderedList/bulletList` 只当 `depth` 计数、**不输出列表标记**（列表降级为段落）；`link` mark **完全不处理**（链接降级为纯文字、URL 丢失）。只有 bold+heading+image 能存活 = wechat_mp 上限。**所以 TapTap 不走 body_segments**，直接吃 `content_json` 做全保真转换（列表/链接/嵌套全保留）。

**v2 架构（采用）**：

- **转换器 `taptap_contents.py`（纯函数，好测）**：`tiptap_to_contents(content_json: dict, image_urls: dict[str,str]) -> list[block]`。
  - 走 `content_json["content"]` 顶层块：paragraph / heading(`info.level=min(level,2)`) / bulletList→list(`style:default`) / orderedList→list(`style:numbered`) / listItem→list-item(`info.li-level`=嵌套深度) / image→`{type:image,info:{img_url}}`。
  - 行内叶子：text→`{text}` 或 `{text,bold:true}`；link mark→`{type:link,children:[{text}],info:{url}}`（前后垫空 `{text:""}`，Slate 规则）。
  - 兜底：未知 mark（斜体/下划线/删除线）丢标记留字；未知块（引用/代码块）降级 paragraph。**不阻塞**。
  - image 节点 key = `asset_id` 或 `stock:<id>`（复用 parser 的 `_asset_id_from_image_node`/`_stock_image_id_from_image_node`）；`image_urls` 缺该 key（图删了）→ 跳过该 image 块。**按 key 查不按顺序**，重复用图/删图都稳。
- **图片上传（I/O）在驱动里、转换器之前**：驱动 `publish_api` 先 DFS 收 content_json 里的 image 节点 → 用 `payload.image_paths[key]` 取本地路径 → 传七牛 → 得 `key→(url,info)`；再调转换器（喂 `key→url`）得 contents，同时攒 `image_infos`。转换器零 I/O。
- **测试**：用 spike 抓的真实 `contents`（`captures/rich_contents.txt`：段落+加粗+一级二级标题+有序/无序列表+链接）做 fixture，逐块断言。

**payload 改动**：`ApiPublishPayload` 为 taptap 加 `content_json: dict|None` + `image_paths: dict[str,Path]|None`（runner 解析 asset/stock→本地路径，含 stock 临时文件 cleanup）；taptap 的 `body_segments` 传空。content_json 为空时回退 plain_text 包单段。

## 6. 运维：登录刷新 + cookie 体检（守 zombie 锁坑）

- **登录（低频）**：仍走浏览器一次（手机验证码 + 腾讯滑块躲不开），noVNC 人工接管。TapTap 驱动需实现 **`detect_logged_in`（给登录路径，不是发布）** + `extract_platform_user_id_async/sync`（抽 VID，best-effort）。登录完成**必须自动 teardown 会话 + 释放 profile 锁**（见 `gotcha-novnc-tab-not-opening-zombie-login-lock`，别靠人记得叉标签）。
- **每晚 cookie 体检（纯 HTTP，不起浏览器）**：后台线程（仿 `sync_scheduler` / pipeline `scheduler` 形态，带开关 + 周期 env），对每个 taptap 账号发一个**只读探针**请求；401 → `account.status="expired"` + `shared/feishu.notify` 喊人重登。**不做自动登录**（SMS 躲不开）。
  > 决策点 D2：探针需要一个**只读端点**（现 spike 只抓了写接口）。下次登录顺手补抓一个 GET（如个人资料/草稿列表），1 个请求即可。在此之前体检先用"建草稿后即删"兜底或暂缓。

## 7. 文件清单（实现阶段）

**改动（共享层，wechat 零回归）：**
- `server/app/modules/tasks/drivers/base.py` — `ApiPublishPayload` 加 `state` / `forum` / `content_json` / `image_paths` 字段，`access_token` 给默认 `""`。
- `server/app/modules/tasks/runner_api.py` — 按 `driver.auth` 分叉（token / cookie）；cookie 分支 `read_state`(cookie) + 读 `api_credentials`(论坛) + 解析 content_json 图片→本地路径 map，注入 payload。

**新增：**
- `server/app/modules/tasks/drivers/taptap.py` — 驱动主体（`mode="api"`, `auth="cookie"`）：`publish_api`（收图→传七牛→转换器→五步 HTTP）+ `detect_logged_in` + `extract_platform_user_id_*`。
- `server/app/modules/tasks/drivers/taptap_contents.py` — `content_json + 图url映射 → contents` 转换器（纯函数，零 I/O，好测）。
- `server/app/modules/tasks/drivers/taptap_client.py` — httpx 封装（create/upload-token/qiniu/update/publish + cookie/XSRF/X-UA 头构造），仿 `wechat_client.py`。
- `server/alembic/versions/0050_seed_taptap_platform.py` — 幂等插 `platforms('taptap','TapTap','https://www.taptap.cn',1)`，down 删之。
- `server/app/modules/tasks/drivers/taptap.py` 末尾 `register(TapTapDriver())`；`bootstrap.py` 加 import。
- `server/tests/test_taptap_*.py` — 转换器单测 + `publish_api` 用 `httpx.MockTransport` 打桩（无需 DB，仿 `test_hot_lists_service`）。

**前端（小）：**
- 账号 API 凭据表单加 TapTap 变体（app_id / group_id / x_ua）；或 MVP 先用 PATCH 脚本，UI fast-follow。

**运维：**
- 体检后台线程 + env 开关（`GEO_TAPTAP_COOKIE_CHECK_ENABLED` / `_INTERVAL_SECONDS`）。

## 8. 风险 / 未决

- **风控**：纯 HTTP 发帖头要齐（User-Agent / Origin / Referer / sec-*，spike 已含）；group_id 不属于账号会被拒。先单账号试点。
- **cookie 寿命**：spike 仅测到 75min/跨进程；建议隔天复跑 `replay_httpx` 测耐用度（决定体检周期）。
- **省略 update-topic**：可能可"图先传→create 带全量→publish"省一步；实现时验证，省则更稳。
- **D1（内容保真 v1/v2）**、**D2（体检只读端点）** 见上，待你拍 D1，D2 下次登录补抓。

## 9. 不做（本期）

- 动态/moment（无论坛短贴）、视频/投票等富媒体块、发帖时选论坛 UID 下拉、自动登录（SMS）。
