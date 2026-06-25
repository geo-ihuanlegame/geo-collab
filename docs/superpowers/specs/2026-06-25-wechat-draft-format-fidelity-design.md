# 微信公众号草稿格式保真设计

- 日期：2026-06-25
- 状态：已与需求方逐段确认
- 范围：仅后端，微信公众号驱动正文 HTML 生成；无 DB / schema 改动、无迁移
- 关联：[2026-06-10 微信公众号平台接入设计](2026-06-10-wechat-mp-platform-design.md)

## 1. 问题与目标

当前微信草稿正文经由有损中间层 `BodySegment` 重建，导致从平台传到草稿箱编辑器后**原有格式丢失**：

| 原格式 | 现状（丢失后） |
|---|---|
| 斜体 / 行内代码 | 丢标记，只剩纯文字 |
| 超链接 `<a>` | 丢成纯文字 |
| 有序 / 无序列表 | 拍平成普通段落，没了项目符号 / 编号 |
| 引用 blockquote / 代码块 | 普通段落 |
| 标题 h3–h6 | 普通段落（仅 h1/h2 保留） |
| 段内"普通**加粗**普通"混排 | 被拆成三个独立 `<p>`（行内粗体被当块级） |

根因：正文走 `content_json → parse_body_segments → BodySegment[] → segments_to_html → HTML`，
中间的 `BodySegment` 是**扁平列表**，结构上只能装下「纯文本 + 整段 bold + heading_level(仅认 1/2)」，
装不下嵌套结构与行内混排。微信草稿 `content` 字段本身**接收 HTML**（h1-h6 / strong / em / ul·ol / blockquote / a / img 等都吃），
是我们在 `BodySegment` 这一层提前丢了信息。

**目标（结构保真）**：把 GEO 文章实际会有的全部格式如实带进草稿——
标题层级、粗体 / 斜体 / 行内代码、有序 / 无序列表（含嵌套）、引用、代码块、超链接、图片位置。

**非目标**：公众号美化排版（标题配色卡片、字号行距、居中、分割线、底色块等"秀米 / 135"风格）——
GEO 文章源里不存在这些样式，属另一独立特性，本设计不做。自动发布（freepublish / 群发）已另行搁置，不在本设计内。

GEO 文章正文的实际格式空间（`ai_generation/converter.py:markdown_to_tiptap`）：
`heading(1-6)` / `paragraph` / `bulletList` / `orderedList` / `listItem`，行内 marks `bold` / `italic` / `code`，
图片，外加前端编辑器手改可能加的 `link`。结构保真正好全覆盖。

## 2. 方案选型（已确认：路线一）

**路线一（采纳）**：新写 `content_json → 保真 HTML` 纯函数转换器，绕开有损的 `BodySegment`。
- 优点：结构全保真、纯函数零 I/O 可单测、行内 marks 留行内（修掉拆段 bug）、未知节点优雅降级。
- 代价：一个新转换器 + 改载荷构建 + 改图片上传循环。

**路线二（否）**：复用 `content_html` 正则换 `<img>` src。
- 否因：`content_html` 标签 / class 形态不完全受控（可能夹带微信会过滤的 class / wrapper），
  `<img>` 反查 asset / stock 再换 src 脆弱（原平台设计文档已明确"不对 content_html 做正则替换"），不可控降级，不好单测。

**路线三（否）**：给 `BodySegment` 加字段。
- 否因：扁平列表结构上装不下嵌套列表与段内混排，根因即此抽象；硬补是跟错误抽象较劲，
  且 `BodySegment` / `parse_body_segments` 被头条驱动与 runner 共用，改动牵连面大。

> 说明：路线一在"递归走 content_json、按节点 key 换图、生成目标格式"这个**代码形状**上与既有
> `taptap_contents.py` 同构，但**无任何运行时依赖**——新转换器不 import / 不调用 TapTap 代码，
> 有独立单测；TapTap 平台是否已实测与本转换器正确性无关。微信真正复用的是生产已验证的
> `wechat_client.py`（token / 传图 / draft·add）与 `wechat_images.py`（压图），本次只换"正文 HTML 怎么生成"这一步。

## 3. 转换器：`wechat_html.py`

新文件 `server/app/modules/tasks/drivers/wechat_html.py`，纯函数、零 I/O、可单测：

```python
def tiptap_to_wechat_html(content_json: dict | list,
                          image_urls: dict[str, str] | None = None) -> str
```

- `image_urls`：节点 key（`image_node_key`：asset_id 或 `stock:<id>`）→ 微信图床 URL，
  由驱动**先上传图片**后喂进来；缺 key 的图片节点跳过（图被删 / 未传成功，照常发布，沿用现有 #36 行为）。
- 返回：草稿 `content` 字段的 HTML 串。

**块级映射**

| Tiptap 节点 | 输出 HTML |
|---|---|
| `heading(level n)` | `<h1>`…`<h6>`（n 夹到 1–6，不再降级） |
| `paragraph` | `<p>…</p>`；空段落 → `<p><br></p>`（保留有意空行） |
| `bulletList` / `orderedList` | `<ul>` / `<ol>` + `<li>`（支持嵌套） |
| `blockquote` | `<blockquote>…</blockquote>` |
| `codeBlock` | `<pre><code>…</code></pre>` |
| `image` | `<p><img src="URL" style="max-width:100%;"></p>`（仅当有 url） |
| 未知块 | 有块级子节点则递归，否则按段落输出其行内（优雅降级，不阻塞） |

**行内映射**（marks 嵌套；文字 `html.escape`、href `html.escape(quote=True)` 转义）

| 文本 mark | 包裹 |
|---|---|
| `bold` | `<strong>` |
| `italic` | `<em>` |
| `code` | `<code>` |
| `link(href)` | `<a href="…">` |
| 组合（bold+italic 等） | 嵌套：`<strong><em>…</em></strong>` |
| `hardBreak` | `<br>` |

- 未知 mark 丢标记留字；未知块降级段落；**绝不抛异常阻塞发布**。
- 行内混排（"普通 + 加粗 + 链接 + 普通"）输出在**单个 `<p>` 内**，不再拆段。

## 4. 解耦：`image_node_key` 归位 parser

`image_node_key`（图片节点 → 稳定 key）当前定义在 `taptap_contents.py`。
将其**移到 `articles/parser.py`**（与 `_asset_id_from_image_node` / `_stock_image_id_from_image_node` 同处，本就是通用解析器），
`wechat_html.py` 与 `taptap_contents.py` 均从 `parser` 导入。

结果依赖图（两个驱动平级、互不引用）：

```
parser.py（通用，已验证）
  ├── image_node_key（从 taptap 移入）
  ↑                       ↑
wechat_html.py        taptap_contents.py
  ↑                       ↑
wechat_mp.py          taptap.py
  ↑
wechat_client.py / wechat_images.py（生产已验证）
```

## 5. 驱动与载荷改动

**载荷**：微信走 `auth='token'`（`runner_api._build_api_payload`），现产出 `body_segments`。
改为产出 `content_json` + `image_paths`（节点 key→本地路径）——二者已是 `ApiPublishPayload` 字段。
`cover_path` 仍由 `article.cover_asset` 解析（WeChat 专属，保留）。

**顺手小重构**（targeted，不扩范围）：`_build_cookie_payload`（TapTap）已内联做
"从 content_json 解析 image_paths（asset 图 + 图库临时图 + temp_files）"；微信改完后两边逻辑同构。
抽共享 helper `_resolve_content_body(article) -> (content_json, image_paths, temp_files)`，两个 builder 共用，消重复。
content_json 为空时回落 plain_text 构造极简 doc（沿用 `_build_cookie_payload` 现有回落语义）。

**驱动 `_publish_api`**（结构对齐 `taptap.py:_upload_images`）：

```
1. 封面：payload.cover_path；无则回落 image_paths 第一张（dict 按文档顺序，next(iter(...)) ）
   → compress_cover_to_jpeg → upload_thumb → thumb_media_id          # 不变
2. 遍历 image_paths.items()：compress_content_image → upload_content_image
   → image_urls[node_key] = 微信 URL                                 # 同图复用 / 删图跳过自动成立
3. content_html = tiptap_to_wechat_html(content_json, image_urls)    # ← 替代 segments_to_html
4. 空串 → PublishError("正文为空")                                    # 不变
5. build_draft_article + add_draft（进 commit_guard.committing()）     # 不变，at-most-once 不动
```

**移除**：`wechat_mp.py` 的 `segments_to_html` 及对 `body_segments` 的依赖。
`BodySegment` / `parse_body_segments` **保留**（头条驱动与 runner 仍用）。

**幂等 / 重试边界零改动**：图片上传走 `retry_call`（幂等）；`add_draft` 仍只进 `commit_guard`（非幂等、不盲重试）。
本次改的纯粹是"正文 HTML 怎么生成"，提交语义不动。

## 6. 微信侧 HTML 行为（实现期用真实草稿核实）

- 已证明渲染：`<h1> <h2> <p> <strong> <img>`（现状草稿在用）。
- 新增标签 `<h3>–<h6> <em> <code> <pre> <ul>/<ol>/<li> <blockquote> <a> <br>` 是公众号文章常见标签，
  实现阶段**建一篇真实草稿肉眼验收**其渲染结果；若微信吞掉某标签，则在转换器单点加降级
  （如 h3–h6 退 `<p><strong>`）。转换器是单一纯函数，降级只改一处。
- **外链**：账号未认证，微信正文外链本就不可点（平台限制，与本代码无关）。默认照常输出 `<a href>`，
  微信去链接化后锚文本以纯文字留存、信息不丢。链接后补"（原网址）"为可选项，默认不做（YAGNI）。

## 7. 测试

1. **转换器单测**（纯函数、无 DB / 网络）：h1–h6；嵌套列表；**段内 bold+italic+link 混排 → 单 `<p>` 内嵌套、不拆段**；
   blockquote；codeBlock；按 node key 换图；缺 url 图跳过；未知节点降级；转义（`< > & "` 与 href）；空文档 → 空串。
2. **驱动测**（改写 `test_wechat_publish.py`，MockTransport 打桩）：上传顺序；草稿 `content` 含保真 HTML
   （`<ul>` / `<em>` / 换好的图 url）；封面回落；无图报错；errcode 映射。引用 `segments_to_html` 的旧用例删除 / 改写。
3. **载荷构建测**：`_build_api_payload` 产出 content_json + image_paths；asset 图 / 图库图路径解析 + temp_files 清理。
4. **真实草稿验收**（实现期手动一次）：对真实公众号建一篇含列表 / 斜体 / h3 / 链接的草稿，肉眼确认渲染——
   锚定第 6 节白名单（"核实而非复述"）。

## 8. 影响面与回滚

- 无 DB / schema 改动、无迁移、无配置项。纯代码替换正文生成路径。
- 回滚 = 还原 `wechat_mp.py` / `runner_api.py` 改动并删 `wechat_html.py`；`image_node_key` 移回不强制（留在 parser 更合理）。
- 本地 Windows / CI 可全链路跑（微信发布纯 HTTP，不依赖浏览器）。
