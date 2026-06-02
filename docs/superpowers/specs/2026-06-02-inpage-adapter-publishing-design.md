# 发文模块重构设计：页内适配器（In-Page Adapter）方案

- 日期：2026-06-02
- 状态：设计待评审
- 适用范围：`server/app/modules/tasks/drivers/` 发布驱动层
- 首批落地平台：头条号（验证架构）

## 1. 背景与动机

现有发布驱动（以 `server/app/modules/tasks/drivers/toutiao.py` 为代表，约 1060 行）走的是
**服务端 Playwright DOM 自动化**：模拟点击编辑器、填标题、传封面、逐段插正文、点发布。

它能跑，但维护成本高、脆性大，典型负担散落在整份文件里：

- Xvfb/Openbox 抢焦点导致 `Ctrl+Alt+1` 被 WM 拦截，被迫改用 `# ` Markdown inputRule（见 `_insert_heading_paragraph`）。
- 草稿自动恢复污染本次发布，需要 `add_init_script` 预清 localStorage（`_install_draft_cleanup_script`）。
- 营销/帮助弹窗反复遮挡编辑器，需要多轮 DOM 扫描关闭（`_dismiss_blocking_popups`）。
- ProseMirror selection/光标位置反复纠正（`_focus_body_editor_end`、`_clear_body_editor`）。
- 图片上传后还要轮询临时 URI 是否替换为 CDN URL（`_wait_publish_images_ready`、`_publish_image_state`）。

平台一旦改版（CSS class、DOM 结构、工具栏），这些选择器编排就会断。**每新增一个平台都是又一份上千行的负债。**

引子：开源插件 [wechatsync](https://github.com/wechatsync/Wechatsync) 用「每个平台一个适配器、直接调平台官方 Web 编辑器 API」的模式覆盖了 31 个平台。它的**适配器/驱动模式**在可扩展性上明显优于手写 DOM 自动化，值得借鉴。

## 2. 关键事实：wechatsync 不能直接「融进来」

| | wechatsync | 本项目 Geo |
|---|---|---|
| 运行位置 | **Chrome MV3 扩展，跑在运营者自己的浏览器里** | **服务端无头浏览器，Docker（Xvfb/x11vnc/noVNC）** |
| 用谁的 cookie | 运营者本人已登录的会话 | 每账号服务端持久化的 `state_path` |
| 工作方式 | 人在浏览器里点「同步」 | **无人值守 worker**：DB 队列、乐观锁、重试、审计 |
| 验证码/登录 | 依赖用户已预先登录 | **noVNC 远程人工接管**（`UserInputRequired`） |

结论：wechatsync 是**客户端扩展**模型，Geo 是**服务端无人值守**模型，两者部署形态根本不同，无法把扩展原样塞进 FastAPI worker。能借鉴的是它的**适配器思想**和**各平台 API 知识**，不是它的运行时。

## 3. 方案选型

- **方案 A — 无浏览器 API 驱动（`httpx` + 持久化 cookie）。** 抛开 Playwright，直接用 cookie 调平台 Web API。
  - 优点：无 Xvfb/Chromium 开销，快、轻、易并发、易单测。
  - 致命风险：**请求签名**。头条 `_signature`/`a_bogus`/`msToken`、小红书 `x-s`/`x-t` 等由站点自身混淆 JS 计算，在页面外复刻是逆向苦役且随时被轮换打破，且恰好集中在头条（首要平台）。
- **方案 B — 页内适配器（`page.evaluate`）。✅ 采用**。保留服务端浏览器，导航到编辑器后在**真实页面上下文里**调平台 API（签名、cookie、`msToken` 都现成）。
  - 优点：签名/CSRF 自动解决；服务端 worker/锁/审计/重试/**noVNC 接管**全部不变；单平台驱动从上千行 DOM 编排缩到几次 API 调用；可逐平台迁移，头条优先，爆炸半径小。
  - 成本：仍付浏览器开销——**但本就必须保留浏览器**（登录/验证码接管离不开它），所以是零新增成本；页内脚本不如纯 Python 好单测。
- **方案 C — 整体改客户端模型。❌ 否决**。会废掉无人值守 worker、服务端多账号自动化、noVNC 接管，等于推翻已建成的产品形态。

**选 B 的核心理由**：浏览器删不掉（接管刚需），所以方案 A 的「省掉浏览器」省的是你删不掉的东西；而方案 A 的签名风险真实且集中在头条。B 把「本就要留的浏览器」用在对的事上——页内 API 调用替代脆弱点击。

> 后续可对 WordPress 等**无签名、纯 cookie** 平台叠加方案 A 式无浏览器快路径，作为优化而非地基。

## 4. 架构设计：每个驱动拆成两层

1. **编排层（Python，共享，平台无关）**：导航到编辑器、确认登录态、处理 `UserInputRequired`→noVNC、保存 `storage_state`、失败截图、组装 `PublishResult`。沉淀到新的共享基类。
2. **适配层（平台相关）**：经 `page.evaluate` 执行的 JS 模块，在**活页面内**调平台 Web API 完成发布：上传图片 → 按平台格式组装正文 → 存草稿 → 发布。以结构化 JSON 把结果回传 Python。

`PlatformDriver` Protocol（`drivers/__init__.py`）基本不变；变化集中在 `publish()` 的内部实现从「点击编排」换成「加载适配器 + `page.evaluate` + 映射结果」。

## 5. 适配器契约（JS ⇄ Python 边界）

- Python 传入纯 JSON `payload`：
  - `title`
  - 封面与正文图片：**base64**（决策见 §11；图片已在 `_maybe_resize_for_upload` 压到 ≤2MB，payload 体积有界）
  - 正文：有序 block 列表（text / heading / 加粗 runs / 图片引用）
  - `stop_before_publish`
- 页内 JS：base64 → `Blob` → POST 平台上传端点 → 拿 CDN URL → 嵌入正文。
- 适配器返回：
  - `{ status: "ok", url }` → `PublishResult`
  - `{ status: "needs_input", reason }` → `UserInputRequired`（→ noVNC）
  - `{ status: "error", message }` → `PublishError`（+ 截图）

## 6. ⚠️ 决定性验证 Spike（实施第一步，门控全局）

未决问题：**头条页内 `fetch()` 能否自动带上签名**？

- 若头条全局 hook 了 `XMLHttpRequest`/`fetch` 自动附加 `_signature`/`a_bogus`/`msToken` → 我们的页内 `fetch` 自动被签 → **纯适配器成立** ✅
- 若签名内联在其业务代码里 → 需调用其内部请求模块/签名函数，或退回**混合方案**（仅最后「确认发布」这一签名请求走 DOM 点击，图片上传 + 正文组装走 API——而后者本就是脆性的大头）。

Spike 做法：用已登录账号在 Playwright 下打开编辑器，抓取真实发布 XHR（端点、参数、headers），再尝试复刻一个页内 `fetch` 看是否成功。

**决策门**：页内 `fetch` 干净可用 → 纯适配器；不可用 → 混合方案。两种结果都能删掉绝大部分 DOM 代码，**风险有界**。

Spike 产出的真实请求记录，转为契约测试 fixture。

### Spike 结论（2026-06-02，未登录静态/动态探测）

工具：Playwright 1.57 直连 `mp.toutiao.com` + `www.toutiao.com`，仅加载公开页并探测 JS，**未登录、未发布**。探测脚本见仓库根 `spike_toutiao_signing.py`。

观测：

- `mp.toutiao.com`（未登录跳转登录页）：`fetch`/`XHR` 被 **Slardar APM**（`lf3-short.ibytedapm.com`）包裹——是监控（`setTraceHeader`），**非签名**；页面已在调 `mssdk.bytedance.com/web/common` 取 `msToken`；cookie 含 `x-web-secsdk-uid` / `passport_csrf_token` / `csrf_session_id`。登录页精简，未加载 acrawler。
- `www.toutiao.com`（未登录）：加载 `acrawler.js` + `bdms.js`，`window.byted_acrawler` 为 object，全局含 `byted_acrawler` / `useWebSecsdkApi` / `bdms`。真实 API 请求 query 串自带 `_signature=_02B4Z6wo…`（如 `/api/pc/info?_signature=…`），`msToken` 来自 mssdk。

推论（决定性）：

- **签名由全局 SDK 机制自动附加**（acrawler 出 `_signature`/`a_bogus`，mssdk 出 `msToken`），不是各调用点内联手写。
- **方案 B 成立、方案 A 否决得到实证**：页面外 httpx 复刻 acrawler+mssdk = 逆向苦役。页内 `fetch` 跑在真实签名上下文里，要么被全局 hook 自动签，要么可直接调 `window.byted_acrawler.sign()`，都远胜页面外。即便发布端点内联签名，acrawler 暴露可调 sign 助手，页内仍只需几行。DOM 混合兜底保留但大概率用不上。

仍需人工确认（需已登录账号，在 Docker 环境）：编辑器里真实发一篇，抓 **1 个真实发布请求**，确认：(a) 发布端点用 `_signature` 还是 `a_bogus`；(b) 全局 hook 自动加 vs 需显式 `byted_acrawler.sign()`；(c) 是否需要 CSRF header（`x-tt-csrf` 之类，cookie 已有 `csrf_session_id`/`passport_csrf_token`）。`spike_toutiao_signing.py` 加 `storage_state` 即可复用为该确认脚本。

### Spike 结论 · phase 2（2026-06-02，已登录真实抓包）

工具：`spike_toutiao_publish_capture.py`（headed 持久化上下文，人工登录 + 真实发一篇测试文，敏感值已脱敏）。

**发布端点**：`POST https://mp.toutiao.com/mp/agw/article/publish`

- Query：`source=mp` · `type=article` · `aid=1231` · `mp_publish_ab_val=0` · `msToken`(~184) · `a_bogus`(~188)；**正式发布那一笔额外带 `_signature`(~147)**。
- 必需 header：`x-secsdk-csrf-token`(~92) · `content-type: application/x-www-form-urlencoded;charset=UTF-8` · `referer: …/profile_v4/graphic/publish`。
- Body（form-urlencoded 关键字段）：
  - `title` —— 标题。
  - `content` —— 正文，**就是简单 `<p data-track="N">…</p>` HTML**（如 `<p data-track="1">我说今天是</p>`）。比当前 ProseMirror 逐段 DOM 插入简单一个数量级。
  - `pgc_id` —— 草稿/文章 id，首次 `save` 生成后续复用。
  - `save` —— `0`=自动存草稿/预览；`1`+`entrance=main`=正式发布（**唯一带 `_signature` 的一笔**）。
  - `pgc_feed_covers` —— JSON 数组，封面上传后的 `tos-cn-i-…` uri + 签名展示 url。
  - `extra` —— JSON（`content_source` / `content_word_cnt` / `gd_ext`…）。
  - 常量 flag：`source=29` · `article_ad_type=3` · `is_fans_article=0` · `draft_form_data={"coverType":2}` · `is_refute_rumor=0` 等。

**图片/封面**：先上传得 `tos-cn-i-6w9my0ksvp/<hash>` uri，再 `POST /mp/agw/article_material/photo/info?app_id=1231`（body `{"uris":[…]}`）解析图片信息；封面写进 `pgc_feed_covers`，正文图走 `<img>` 引用 tos uri。

**签名机制（决定性）**：自动存草稿(`save=0`)、预览(`is_app_preview=1`)、正式发布(`save=1`) **每一笔** `/mp/agw/*` 都带 `a_bogus`+`msToken` → 签名是**请求层全局 hook 统一附加**，非各调用点手写。→ 页内 `fetch('/mp/agw/article/publish?…', {method:'POST', body})` 会被同一 hook 自动签名，**B-direct 几乎零签名代码**；终发布额外的 `_signature` 由 hook 按动作附加。

**对方案的最终影响**：
- 方案 A 需复刻 `msToken`+`a_bogus`+`_signature`+csrf 四件套 → **彻底否决，已实证**。
- **方案 B-direct（页内重建 fetch + 全局 hook 自动签）为首选**，DOM 几乎归零。
- 正文/封面格式比预想简单得多（`<p>` HTML + tos uri），转换成本低。
- 唯一残留：用一笔 `save=0` 草稿验证「我们重建的 fetch 能否拿全 3 个 token」→ 落为实现 **Task 1**，非架构风险。

### Spike 结论 · phase 3（2026-06-02，M1 完成后真实 live 验证）

用 `test_toutiao_inpage_live.py` 对已登录 profile 跑了一次真实 `save=0` 草稿（headed，无人工干预）。

**关键正面结论 —— 架构被实证**：页内 `XMLHttpRequest` **被全局 hook 自动签名并被服务端受理**：`httpStatus=200`，返回结构化业务响应
`{"code":7050,"data":{"pgc_id":"0"},"err_no":7050,"message":"保存失败","reason":"保存失败"}`。
若是签名/鉴权失败，响应形态会完全不同（verify/403/风控页），而这里请求**进到了业务校验层**。→ **B-direct 签名彻底证实，#1 风险消除。**

**保存被拒（待解决）**：`code=7050 保存失败`、`pgc_id=0`（未建出草稿）。已尝试把抓包里 `save=0` 的**完整字段集**补齐（含 `title_id`、`timer_time`、`educluecard`、`star_order_*`、`activity_tag`、`trends_writing_tag` 等 8 个缺失字段），**仍是 7050** → 根因**不是字段完整度**。

**主假设（→ M2）**：干净保存需要**封面**（头条发布强制封面：DOM 驱动 `_handle_cover` 与 `run_publish` 都强制）和/或一次**草稿初始化 / 取 pgc_id** 的前置调用（我们只抓到了发布请求、没抓响应，也没抓编辑器加载时的建稿链路）。两者都属 **M2**（图片上传 + 草稿生命周期）。

**M1 结论修正**：M1 在「不依赖图片上传 API」的前提下能证的都证了 —— 签名、请求受理、表单构造、整条页内管线直到**服务端业务校验**。一笔**被受理的成功保存**被卡在封面/建稿环节（M2）。`save=0`-免封面 的原始假设不成立。

**下一步（M2 Task 0 capture）**：扩展 `spike_toutiao_publish_capture.py` —— 记录**响应体** + 编辑器加载时的**全部 mp.toutiao.com 请求**，定位建稿/取 pgc_id 调用并确认封面是否为保存必需。

## 7. 组件与文件布局（新增）

- `drivers/adapters/toutiao.js` —— 头条页内适配器。
- `drivers/adapters/runtime.js` —— 极薄 shim，提供 fetch/upload 帮助函数，替换 wechatsync 适配器假设的 `chrome.*` 运行时。
- `drivers/inpage.py` —— 共享 `InPageDriver`：加载适配器 JS、由 `PublishPayload` 构建 JSON payload（复用 `_group_paragraphs`/`BodySegment`，下沉到共享）、跑 `page.evaluate`、映射结果。
- `drivers/toutiao_inpage.py` —— `ToutiaoInPageDriver(InPageDriver)`，复用现有 `detect_logged_in`。
- `drivers/toutiao.py`（DOM 版）—— **迁移期保持不动**。

## 8. 数据流（外层循环不变）

worker 抢占记录 → 构建 `PublishPayload` → runner 启动浏览器、导航、认证 → `driver.publish()` →
〔页内：构建 JSON + base64 → `page.evaluate(adapter)` → 映射结果〕→ 保存 `storage_state` → `PublishResult` → 记录更新 + 审计。

## 9. 迁移与共存策略（决策）

- **不删 DOM 驱动**。用环境变量切换：`GEO_TOUTIAO_DRIVER=dom|inpage`（默认 `dom`，验证稳定后翻成 `inpage`）。
- 两个驱动都 `register`；选择逻辑放在 `get_driver` / `build_publish_runner_for_record`。
- 好处：可在真实发布上 A/B，问题随时秒回滚。`inpage` 稳定后再删 DOM 路径。

## 10. `stop_before_publish` / 手动确认

- `stop_before_publish=true` 时适配器停在草稿/预览，返回哨兵，记录进 `waiting_manual_publish`。
- 手动确认（`POST /api/publish-records/{id}/manual-confirm`）时：**重新导航并发布已存草稿**，而非依赖暂停期间仍开着的预览会话（更健壮——会话/页面未必存活）。
- 待办：规划阶段核实当前 manual-confirm 的具体机制后再定细节。

## 11. 错误处理

- 平台返回非 200 → `PublishError` 并附响应体片段（诊断信息远好于「selector not found」）。
- 登录/验证码（预检 `_ensure_publish_page` 或适配器 `needs_input`）→ `UserInputRequired` → noVNC，逻辑不变。

## 12. 测试策略

- **纯 Python 单测**：payload 构建 / block 切分 / base64，`page.evaluate` 用 monkeypatch 打桩返回预设适配器结果——无浏览器、快，契合现有 `monkeypatch.setattr(...build_publish_runner_for_record...)` 风格。
- **契约测试**：Spike 抓到的真实发布请求作为 fixture。
- **集成测试**：对真实已登录头条测试账号，按 `@pytest.mark.mysql` 风格打 `@pytest.mark.live`，CI 跳过。

## 13. 许可证与复用边界

- wechatsync 主仓 `LICENSE` 显示 **GPLv3**（有检索聚合声称 MIT-0，与 LICENSE 文件矛盾，按未定处理）。
- GPLv3 为强 copyleft：把其源码粘进（闭源）服务端会触发开源义务。
- **规则**：把其适配器当作端点/payload 形态的**事实参考**（API 事实不受版权保护），自己重写实现；**不照抄其源码**。复制任何 `@wechatsync/drivers` 文件的字面代码前，先确认该具体文件的许可证。

## 14. 决策记录与待确认项

已为你拍板的决策（评审时可推翻）：

1. 图片走 base64 传入页内。
2. DOM / in-page 用环境变量共存，可回滚。
3. GPL → 只参考不照抄。
4. 手动确认时「重发已存草稿」。

待确认项：

- ~~Spike 结论（页内 fetch 是否自动签名）→ 决定纯适配器 vs 混合。~~ **已彻底解决（2026-06-02 live 验证）**：页内 XHR 被全局 hook 自动签名且被服务端受理（见 §6「Spike 结论 · phase 3」）。B-direct 成立。
- ~~新残留：`save=0` 草稿被 `code=7050 保存失败` 拒绝。~~ **已定位（2026-06-02）= 环境性**：编辑器**自己**的原生保存同样 7050，请求头 `x-secsdk-csrf-token: DOWNGRADE`（secsdk 握手退化）。我们的请求与编辑器等价、无 bug。修复在环境层（干净网络 / 重新登录刷新 secsdk）。详见「M2 调查记录」。
- 现有 manual-confirm 机制细节（规划阶段核实）。
- `@wechatsync/drivers/toutiao` 具体文件许可证（照抄任何字面代码前确认；当前按只参考不照抄处理）。

## M2 调查记录（2026-06-02 capture）

工具：`spike_toutiao_m2_capture.py`（抓编辑器加载链路 req+resp）、`spike_toutiao_probe_outgoing.py`（抓我们自己 XHR 的出参/请求头）。

编辑器加载链路（均 GET / 200）：

- `/mp/agw/article/new?article_type=0&format=json&compat=1` → 返回账号/权限上下文（`media.id=1837538131248139` = `title_id` 第二段），**不返回 pgc_id**（不是建稿接口）。
- `/mp/agw/creator_center/draft_list` → 已有草稿列表；样本草稿 `pgc_id=7644385158866551348` 的 `pgc_feed_covers:"[]"`（**空封面**）、`is_draft:true` → **草稿不需要封面**。
- `/mp/agw/article/edit?pgc_id=…` → 打开已有草稿（编辑器加载时自动载入最近草稿，故我的合成打字没触发"新建"自动保存）。
- `/mp/agw/diversity/publish/strategy/v1/check/`、`publish_suppression_tip`、`fan_article_count_remained` → 权限/提示。

7050 排查（逐一实证排除）：

- ❌ 字段完整度：补齐完整字段集（含 `title_id` 等 8 个缺失字段）仍 7050。
- ❌ 封面：草稿允许空封面（draft_list 实证）。
- ❌ CSRF / 签名：`spike_toutiao_probe_outgoing.py` 实测我们的 `POST /article/publish` 出参带 `a_bogus`+`msToken`、请求头带 `x-secsdk-csrf-token`（全局 hook 已全加）。`_signature` 仅终发布才有，`save=0` 不需要。
- **✅ 根因已定位 = 环境性，不是我们的请求**：驱动编辑器触发其**原生自动保存**（`spike_toutiao_editor_save.py`），编辑器**自己**的 `POST /article/publish?save=0` **同样返回 7050**，且请求体与我们逐字段等价（同 endpoint/参数、`save=0`、**无 pgc_id**、`pgc_feed_covers=[]` 空封面）。**冒烟证据**：编辑器自己的请求头 `x-secsdk-csrf-token: DOWNGRADE` —— secsdk 在本机环境**无法完成安全握手、退化为占位 CSRF**，服务端因此拒绝所有保存（编辑器自身也中招）。代理开/关均 7050（`spike_noproxy_probe.py`）。

**结论**：页内适配器构造的 save 请求**是正确的**（与编辑器原生请求等价），无 payload / 生命周期 bug；7050 由本机 secsdk 退化（`DOWNGRADE`）造成，对**编辑器原生流程与 DOM 驱动一视同仁**——不是 in-page 特有问题。**修复在环境层**：在 secsdk 能正常握手的**干净网络**（生产 / Docker，非被标记 IP）上验证。

**已验证（2026-06-02，`spike_toutiao_fresh_login_save.py`）**：用**全新 profile 扫码重登**后，save **仍 7050** —— 排除"会话过期/secsdk 状态陈旧"，确认是**本机网络/secsdk 环境层**问题，**本地无法绕过**（代理开/关、旧/新登录都一样）。→ **M2 的保存验证必须在生产/干净网络进行。**

附带发现（M2 polish）：适配器 `goto` 后立即 `_is_logged_out(page.url)` 偶发误报"需人工接管"（goto 后短暂重定向的时序问题）。M2 应改为"等编辑器标题框就绪 / 短重试后再判定登出"，而非 goto 后一次性判定。

## 15. 不在本期范围（YAGNI）

- 头条以外的平台适配器（知乎/掘金/小红书/公众号/百家号…）——本期只做头条验证架构。
- 方案 A 式无浏览器快路径——后续优化。
- 把 wechatsync 作为运行时依赖——只做知识参考。
