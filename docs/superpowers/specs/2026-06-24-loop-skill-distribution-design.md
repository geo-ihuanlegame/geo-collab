# `/goal` Loop Skill 分发通道 · 设计

- 状态：设计稿（v0），待 review 后进入 writing-plans 阶段
- 日期：2026-06-24
- 上游：[`2026-06-24-goal-loop-engineering-design.md`](./2026-06-24-goal-loop-engineering-design.md)（已合并 PR #144）
- 受众：实施 plan 评审 + 团队接入文档对齐
- 不动的部分：MCP 架构 / `list_today_loop_articles` 工具 / 上游设计稿描述的 Loop 行为
- 动的部分：补齐**新人怎么拿到 5 个 `.claude/` 模板文件**这个 gap —— 同事不会 `git clone`，要靠平台分发

---

## 0. 一句话

把上游 spec §5 / plan Tasks 5-9 的 5 个 skill / command / README 模板**搬进
git 服务端模板目录**（`server/app/modules/loop_skills/templates/`），同时提供
两条分发通道：

1. **Web 端**：`McpConnectWorkspace` 加 Section ⑤，user JWT 鉴权下载 ZIP
2. **MCP install 工具**：`install_loop_skills()` 一口气返回 5 个文件 dict，
   让 Claude Code 用 Write 工具写到本机 `.claude/`

两条通道共用 `loop_skills.service.build_bundle()` 的纯函数；带 version +
sha256 让本地知道是否需要更新。

---

## 1. 锁定决策（brainstorming 已答）

| # | 决策 | 选项 |
|---|------|------|
| 1 | 源文件落点 | **入 git** 到 `server/app/modules/loop_skills/templates/`，唯一可信源 |
| 2 | Web 下载鉴权 | **user JWT**（同 McpConnectWorkspace 其他 endpoints） |
| 3 | MCP install 工具粒度 | **单工具一口气打包返回全部 5 个文件**，Claude Code 主对话调用后用 Write 落盘 |
| 4 | 版本意识 | **加 `LOOP_SKILL_BUNDLE_VERSION` + `bundle_sha256`**，让本地能感知是否过期 |
| 5 | UI 位置 | **McpConnectWorkspace 现有 4 个 section 之后加 ⑤**，复用同套 card 样式 |

---

## 2. 架构总览

```
┌─────────────────────────────────┐   ┌──────────────────────────────────┐
│ 浏览器用户                       │   │ Claude Code 用户                  │
│ https://geo.huanchanghuyu.com/  │   │ /goal 接入指南 → 已配 MCP token   │
└──────────────┬──────────────────┘   └────────────────┬─────────────────┘
               │ user JWT cookie                       │ X-MCP-Token header
               │                                       │
               ▼                                       ▼
┌─────────────────────────────────┐   ┌──────────────────────────────────┐
│ GET /api/mcp/loop-skill-bundle  │   │ MCP tool                          │
│   /info     (JWT)               │   │   install_loop_skills()           │
│   /download.zip   (JWT)         │   │   返回 {version, sha256,          │
│                                 │   │   files: [{path, content, ...}]} │
└──────────────┬──────────────────┘   └────────────────┬─────────────────┘
               │                                       │
               └────────────────┬──────────────────────┘
                                │
                                ▼
              ┌────────────────────────────────────┐
              │ server/app/modules/loop_skills/    │
              │ ├── service.py                     │ ← bundle 打包逻辑（公用）
              │ ├── router.py                      │ ← user JWT + MCP token endpoints
              │ ├── version.py                     │ ← LOOP_SKILL_BUNDLE_VERSION
              │ └── templates/                     │ ← 源文件入 git
              │     ├── README.md                  │
              │     ├── commands/goal.md           │
              │     └── skills/                    │
              │         ├── geo-goal-orchestrator/SKILL.md
              │         ├── geo-article-writer/SKILL.md
              │         └── geo-article-verifier/SKILL.md
              └────────────────────────────────────┘
                                │
                                │ MCP tool 单独在
                                │ server/mcp/tools/action.py 注册
                                ▼
              ┌────────────────────────────────────┐
              │ install_loop_skills (MCP tool)     │
              │ - HTTP self-call 到 /install-payload│
              │   返回完整 payload                  │
              └────────────────────────────────────┘
```

### 2.1 关键设计点

1. **源文件入 git** —— `server/app/modules/loop_skills/templates/`，唯一可信
   源；前后端打包都从这里读
2. **两条入口，一份逻辑** —— `service.build_bundle()` 是公用纯函数，Web 端 +
   MCP 端都调它
3. **新建独立模块** `loop_skills` 而不是塞到 `mcp_catalog`，因为这是**资产分发**
   不是只读 catalog 列表（语义不同）
4. **MCP install 工具放 `action.py` 组**——它让 Claude Code 写本地文件，有副作
   用，不属于只读 catalog
5. **版本管理**：手工 bump `LOOP_SKILL_BUNDLE_VERSION` 字符串 + CI 测试强制
   「改 templates 必同步加新 sha 到 `KNOWN_BUNDLE_SHAS`」

---

## 3. 文件清单 + 源文件迁移

### 3.1 新增/修改的文件

```
server/app/modules/loop_skills/                            # 新建模块
├── __init__.py
├── service.py                                             # build_bundle() + build_zip()
├── router.py                                              # user + MCP token endpoints
├── version.py                                             # LOOP_SKILL_BUNDLE_VERSION
└── templates/                                             # 源件（唯一可信源）
    ├── README.md
    ├── commands/
    │   └── goal.md
    └── skills/
        ├── geo-goal-orchestrator/SKILL.md
        ├── geo-article-writer/SKILL.md
        └── geo-article-verifier/SKILL.md

server/app/main.py                                          # +2 行 include_router
server/mcp/tools/action.py                                  # +install_loop_skills tool

server/tests/test_loop_skill_bundle.py                     # 新建：7 个用例

web/src/api/mcp.ts                                          # +loop skill bundle 客户端
web/src/features/mcp/McpConnectWorkspace.tsx               # +Section ⑤ UI
```

### 3.2 源文件来源

把已经在前作者本机 `.claude/`（gitignored）写好的 5 个文件搬进 git：

| 来源（本机 gitignored） | 目标（入 git） |
|---|---|
| `.claude/README.md` | `server/app/modules/loop_skills/templates/README.md` |
| `.claude/commands/goal.md` | `templates/commands/goal.md` |
| `.claude/skills/geo-goal-orchestrator/SKILL.md` | `templates/skills/geo-goal-orchestrator/SKILL.md` |
| `.claude/skills/geo-article-writer/SKILL.md` | `templates/skills/geo-article-writer/SKILL.md` |
| `.claude/skills/geo-article-verifier/SKILL.md` | `templates/skills/geo-article-verifier/SKILL.md` |

**轻微改造**（搬进去时同步修订）：

- `README.md` 第一段从「**不进 git**」改为「本目录是 `geo-collab` 服务端分发
  的 `/goal` Loop skill 模板（版本：`<LOOP_SKILL_BUNDLE_VERSION>`）」
- 各文件出现「克隆仓库后」类暗示的话改成「装到 `~/.claude/` 或 `<repo>/.claude/`」
- 这些**搬进 git 的版本**才是「正本」；上游 spec / plan 里 §5 / Tasks 5-9
  的内容**不再是 source of truth**，加注释指向 templates/

### 3.3 给上游 spec / plan 加一句指引

PR #144 已合的 [`2026-06-24-goal-loop-engineering-design.md`](./2026-06-24-goal-loop-engineering-design.md)
+ [`../plans/2026-06-24-goal-loop-engineering.md`](../plans/2026-06-24-goal-loop-engineering.md)
里 §5 / Tasks 5-9 的内容会变成**重复且过时**——本 PR 一个轻量 commit 给它们
加一条「source-of-truth 已迁移到 `server/app/modules/loop_skills/templates/`，
本节内容仅作历史快照参考」。

### 3.4 边界 / 不做的事

| 不做 | 原因 |
|---|---|
| `.zip` 文件持久化到磁盘缓存 | 5 个小文件总共 ~15 KB，每次请求 in-memory zip 完全可接受 |
| 用户级 install 偏好（"我已装过此版本，别再提示"） | YAGNI；POC 期人脑记 |
| install 时 diff 显示 | 让 Claude Code 用 Read+Write 自己呈现 diff；MCP 工具只返回最新内容 |
| distribute-loop / weekly-loop 的 bundle | 范围聚焦 `/goal` skill 包；后续可加 `bundle_name` 参数扩展 |

---

## 4. 后端 API + MCP 工具签名

### 4.1 `service.py` — 纯函数 bundle 打包（Web + MCP 共用）

```python
"""loop_skills.service —— 服务端「正本」模板的扫描 + 打包逻辑。

无 IO 副作用、无 DB 访问；纯文件读 + 内存 zip。Web 端 + MCP 端共用。
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

from server.app.modules.loop_skills.version import LOOP_SKILL_BUNDLE_VERSION

_TEMPLATES_DIR = Path(__file__).parent / "templates"


@dataclass(frozen=True)
class SkillFile:
    """单个模板文件的元信息 + 内容。"""
    path: str           # 相对 templates/ 的 posix 路径
    size: int           # bytes
    sha256: str         # hex digest
    content: str        # utf-8 文本


@dataclass(frozen=True)
class SkillBundle:
    version: str
    bundle_sha256: str
    files: list[SkillFile]


def build_bundle() -> SkillBundle:
    """扫描 templates/ 下所有文件，返回排好序的 bundle。

    遇到非 utf-8 文件直接抛 ValueError —— 模板就该是文本，加二进制是 bug。
    """
    files: list[SkillFile] = []
    for path in sorted(_TEMPLATES_DIR.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(_TEMPLATES_DIR).as_posix()
        raw = path.read_bytes()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"loop_skills template not UTF-8: {rel}") from exc
        files.append(
            SkillFile(
                path=rel,
                size=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
                content=content,
            )
        )

    # bundle 级 sha256: 对 (path, file_sha) 排序后串接再 hash
    h = hashlib.sha256()
    for f in files:
        h.update(f.path.encode("utf-8"))
        h.update(b"\x00")
        h.update(f.sha256.encode("ascii"))
        h.update(b"\x00")
    bundle_sha = h.hexdigest()

    return SkillBundle(
        version=LOOP_SKILL_BUNDLE_VERSION,
        bundle_sha256=bundle_sha,
        files=files,
    )


def build_zip(bundle: SkillBundle) -> bytes:
    """打包成 zip bytes。文件路径保持模板的目录结构。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in bundle.files:
            zf.writestr(f.path, f.content)
    return buf.getvalue()
```

### 4.2 `version.py` — 一行常量 + 已知 sha 白名单

```python
"""手工维护的 bundle 版本号 + 已审核 sha 集合。

CI 测试 (test_loop_skill_bundle.py::test_bundle_sha_is_known) 会校验：
如果 build_bundle().bundle_sha256 没记录在 KNOWN_BUNDLE_SHAS 集合里，
fail + 提示开发者：把新 sha 加进 KNOWN_BUNDLE_SHAS 并 bump LOOP_SKILL_BUNDLE_VERSION，
强制「改模板必同步 bump 版本」纪律。
"""

LOOP_SKILL_BUNDLE_VERSION = "2026-06-24-v1"

KNOWN_BUNDLE_SHAS = frozenset({
    # 在 Task 1 完成后跑 build_bundle 拿到 sha 填进来
})
```

### 4.3 Web 端 endpoints（`router.py`）

```python
"""loop_skills HTTP 路由。

两组路由：
- router (user JWT)：/info + /download.zip，给 Web Section ⑤ 用
- mcp_router (MCP token)：/install-payload，给 install_loop_skills 工具用

两条用户群不同 + 鉴权不同，必须拆 router；service 层 build_bundle 共用。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel

from server.app.core.mcp_auth import require_mcp_token
from server.app.modules.loop_skills.service import build_bundle, build_zip

router = APIRouter()


class LoopSkillFileMeta(BaseModel):
    path: str
    size: int
    sha256: str


class LoopSkillBundleInfo(BaseModel):
    version: str
    bundle_sha256: str
    files: list[LoopSkillFileMeta]
    install_hint: str


@router.get("/loop-skill-bundle/info", response_model=LoopSkillBundleInfo)
def get_loop_skill_bundle_info() -> LoopSkillBundleInfo:
    """[user] /goal Loop skill 包元信息 — 给前端 Section ⑤ 显示版本 + 校验。"""
    b = build_bundle()
    return LoopSkillBundleInfo(
        version=b.version,
        bundle_sha256=b.bundle_sha256,
        files=[LoopSkillFileMeta(path=f.path, size=f.size, sha256=f.sha256) for f in b.files],
        install_hint=(
            "解压到本机 ~/.claude/（全局，所有 Claude Code 会话可见）"
            " 或项目根 <repo>/.claude/（仅该项目可见）。"
        ),
    )


@router.get("/loop-skill-bundle/download.zip")
def download_loop_skill_bundle_zip() -> Response:
    """[user] 下载完整 zip。前端 Section ⑤ 「下载 ZIP」按钮。"""
    b = build_bundle()
    data = build_zip(b)
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="geo-loop-skills-{b.version}.zip"'
            ),
            "X-Bundle-Version": b.version,
            "X-Bundle-Sha256": b.bundle_sha256,
        },
    )


# MCP token 鉴权 (router-level dependency)
mcp_router = APIRouter(dependencies=[Depends(require_mcp_token)])


@mcp_router.get("/loop-skill-bundle/install-payload")
def get_loop_skill_install_payload() -> dict:
    """[MCP] install_loop_skills 工具的后端入口 — 返回完整文件 dict。"""
    b = build_bundle()
    return {
        "ok": True,
        "data": {
            "version": b.version,
            "bundle_sha256": b.bundle_sha256,
            "install_hint": (
                "Write each file to the user's .claude/ directory, preserving "
                "the relative path. Prefer project-level <repo>/.claude/ over "
                "~/.claude/ when the user is currently inside a git repo. "
                "If a file already exists, show diff and ask user before overwriting."
            ),
            "files": [
                {"path": f.path, "content": f.content, "sha256": f.sha256, "size": f.size}
                for f in b.files
            ],
        },
        "error": None,
    }
```

### 4.4 MCP install 工具（`server/mcp/tools/action.py` 末尾追加）

```python
@mcp.tool()
async def install_loop_skills() -> dict[str, Any]:
    """Fetch the /goal Loop skill bundle so Claude Code can install it locally.

    Returns a dict containing all 5 template files (README, slash command, 3 SKILL.md).
    The calling Claude Code session should then use its Write tool to write each
    file to the user's `.claude/` directory.

    Use this when the user asks something like "install geo loop skills" or
    "set me up to use /goal". Before writing files, check whether the user has
    a local `.claude/` directory (project-level or `~/.claude/`) and ask
    which they prefer.

    Returns:
        {"ok": True, "data": {
            "version": str,                # e.g. "2026-06-24-v1"
            "bundle_sha256": str,
            "install_hint": str,           # plain-English placement guidance
            "files": [
                {"path": str, "content": str, "sha256": str, "size": int},
                ...
            ],
        }, "error": None}
    """
    return await _aget("/api/mcp/loop-skill-bundle/install-payload")
```

### 4.5 路由挂载（`main.py` 末尾）

```python
from server.app.modules.loop_skills.router import (
    router as loop_skills_user_router,
    mcp_router as loop_skills_mcp_router,
)

app.include_router(
    loop_skills_user_router,
    prefix="/api/mcp",
    tags=["loop-skills"],
    dependencies=[Depends(get_current_user)],
)
app.include_router(
    loop_skills_mcp_router,
    prefix="/api/mcp",
    tags=["loop-skills-mcp"],
)
```

---

## 5. 前端 Section ⑤ UI

放在 `McpConnectWorkspace.tsx` 现有 4 个 section 之后。

### 5.1 区块结构

```
┌─────────────────────────────────────────────────────────────────────┐
│ ⑤ 装 /goal 自动生文 Skills（可选）                                  │
│                                                                     │
│ 想用 /goal 一句话让 Claude 帮你跑生文 Loop？需要先在本机 .claude/ 装 │
│ 5 个 skill 模板。                                                    │
│                                                                     │
│ ┌─ 当前服务端版本 ──────────────────────────────────────────────┐    │
│ │ 版本：  2026-06-24-v1                                         │    │
│ │ SHA-256: a1b2c3d4... [📋 复制]                                │    │
│ │ 含 5 个文件 / 总 14.8 KB                  [展开看文件清单 ▾]  │    │
│ └────────────────────────────────────────────────────────────────┘    │
│                                                                     │
│ ┌─ 方式 A · 让 Claude Code 自己装（推荐）─────────────────────┐    │
│ │ 前提：上面 ④「测试连接」已通过                                │    │
│ │                                                              │    │
│ │ 在 Claude Code 里说：                                         │    │
│ │   ┌──────────────────────────────────────────────────┐       │    │
│ │   │ 帮我装 geo loop skills                            │ [📋]  │    │
│ │   └──────────────────────────────────────────────────┘       │    │
│ │                                                              │    │
│ │ Claude 会调 install_loop_skills 工具拿到 5 个文件 + 询问你    │    │
│ │ 装到全局 ~/.claude/ 还是项目根 .claude/，然后用 Write 工具    │    │
│ │ 写到本地。                                                    │    │
│ └──────────────────────────────────────────────────────────────┘    │
│                                                                     │
│ ┌─ 方式 B · 下载 ZIP 手动解压（无需配 MCP）────────────────────┐    │
│ │ [⬇  下载 geo-loop-skills-2026-06-24-v1.zip (14.8 KB)]        │    │
│ │                                                              │    │
│ │ 解压到 ~/.claude/（全局）或 <repo>/.claude/（项目级）         │    │
│ │ 文件位置要保留 zip 里的目录结构：                             │    │
│ │   .claude/                                                    │    │
│ │   ├── README.md                                               │    │
│ │   ├── commands/goal.md                                        │    │
│ │   └── skills/...                                              │    │
│ └──────────────────────────────────────────────────────────────┘    │
│                                                                     │
│ 装好以后：重启 Claude Code → 输入 /goal 帮我产出 1 篇国风游戏文章 │
└─────────────────────────────────────────────────────────────────────┘
```

### 5.2 行为细节

| 元素 | 行为 |
|---|---|
| 「展开看文件清单 ▾」 | 点击展开/折叠 `<table>`，列：`path` / `size` / `sha256[:12]` |
| 「复制 SHA-256」 | `navigator.clipboard.writeText`，复制成功 toast |
| 「复制 提示语」 | 同上 |
| 下载按钮 | `<a href="/api/mcp/loop-skill-bundle/download.zip" download>`，浏览器自动带 cookie 走 user JWT；不需要 JS 拉 blob |
| 「方式 A」前置检查 | 如果 Section ④「测试连接」结果是失败 / 未测，方式 A 卡片置灰 + 提示 "请先到 ④ 完成连接测试" |
| 版本号取回 | 页面挂载时调 `GET /api/mcp/loop-skill-bundle/info` 一次；失败展示「无法加载，请刷新或联系管理员」+「重试」按钮 |

### 5.3 API 客户端 (`web/src/api/mcp.ts` 追加)

```typescript
export interface LoopSkillFileMeta {
  path: string;
  size: number;
  sha256: string;
}

export interface LoopSkillBundleInfo {
  version: string;
  bundle_sha256: string;
  files: LoopSkillFileMeta[];
  install_hint: string;
}

export async function getLoopSkillBundleInfo(): Promise<LoopSkillBundleInfo> {
  const res = await fetch("/api/mcp/loop-skill-bundle/info", { credentials: "include" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// 下载走原生 <a download>，不需要 fetch wrapper
```

---

## 6. 错误处理 + 测试策略

### 6.1 失败矩阵

| 层 | 故障 | 响应 | 用户能看到什么 |
|---|---|---|---|
| **Web `/info`** | 后端读 templates 报 IOError | 服务端 raise → FastAPI 全局 500 | 前端 Section ⑤ 顶部红色提示「无法加载 skill 包元信息，请联系管理员」+ 「重试」按钮 |
| **Web `/info`** | 未登录访问 | user JWT 依赖 → 401 | 前端会被 `getCurrentUser` 拦截到登录页（已有机制） |
| **Web `/download.zip`** | 模板某文件不是 utf-8 | service raise ValueError → 500 | 浏览器报"下载失败"（罕见；CI lint 会先抓到） |
| **MCP `/install-payload`** | token 错 | `require_mcp_token` → 401 | Claude Code 主对话看到 MCP 工具回错 |
| **MCP install_loop_skills** | 后端读模板报错 | mcp_exception_response → 500 detail 含 context | Claude Code 主对话能看到具体错 |
| **前端 「复制」** | 浏览器禁用 clipboard API | toast 红「复制失败，请手动选中文字」 | 不致命，用户可手动复制 |
| **前端 「下载 ZIP」** | 浏览器拦截下载 | 用户手动允许 | 不归我们 |
| **CI** | 改了 templates/ 但忘 bump version + 加 sha 到 KNOWN | `test_bundle_sha_is_known` fail → CI 红 | 提示开发者按错误信息把新 sha 加进 KNOWN + bump version |

### 6.2 3 个边界纪律

1. **service 层抛 ValueError 不抛 ClientError** —— 这是「模板坏」类内部错误，
   不该走全局 400 handler，应该走 500 让 ops 看到
2. **MCP endpoint 不复用 user JWT endpoint 的实现细节** —— 共享只能在
   `service.build_bundle()` 这一层
3. **下载 URL 直接 `<a download>`，不要 fetch + blob** —— Chrome 大文件
   fetch+blob 会全量加载到内存

### 6.3 自动测（CI 跑）

`server/tests/test_loop_skill_bundle.py`，共 7 个用例：

| # | 测试 | 验证什么 |
|---|---|---|
| 1 | `test_build_bundle_lists_all_template_files` | service.build_bundle() 返回 files 包含 5 个预期路径 |
| 2 | `test_build_bundle_sha_stable` | 同一份模板调两次 build_bundle，bundle_sha256 完全一致 |
| 3 | `test_build_bundle_sha_changes_when_content_changes` | 用 `tmp_path` fixture 复制 templates/ 到临时目录，monkeypatch `service._TEMPLATES_DIR` 指过去；在临时目录里改一个文件后 build_bundle 拿新 sha，断言两次 sha 不等。不要直接改 git 里的 templates 文件（会污染其它测试 / 仓库工作树） |
| 4 | `test_bundle_sha_is_known` | `build_bundle().bundle_sha256 in KNOWN_BUNDLE_SHAS`；不在就 fail + 提示开发者 |
| 5 | `test_build_zip_round_trip` | build_zip 解出来内容 + path 跟 bundle.files 一对一 |
| 6 | `test_user_endpoint_requires_jwt` | `/api/mcp/loop-skill-bundle/info` 不带 cookie → 401 |
| 7 | `test_mcp_endpoint_requires_mcp_token` | `/api/mcp/loop-skill-bundle/install-payload` 不带 X-MCP-Token → 401 |

1-5 是纯函数 / in-memory zip，不需要 DB，跑得快；6-7 需要 `build_test_app`。

### 6.4 手工冒烟

| # | 步骤 | 期望 |
|---|---|---|
| 1 | 浏览器打开 https://geo.huanchanghuyu.com/ 登录 → MCP 接入 tab | Section ⑤ 显示 |
| 2 | 看到 Section ⑤ 顶部版本号 + sha256 + 文件清单 | 都加载出 |
| 3 | 点 「下载 .zip」 | 浏览器下载 `geo-loop-skills-2026-06-24-v1.zip` |
| 4 | 解压检查 | 5 个文件在正确路径、跟服务端 templates/ 内容一致 |
| 5 | Claude Code 主对话说 "帮我装 geo loop skills" | Claude 调 install_loop_skills MCP 工具拿到 payload，用 Write 写到本机 |
| 6 | 跑 `/goal 1 篇国风游戏文章作为冒烟` | 走通（=验证装好的 skill 能让 /goal 正常 orchestrate） |

### 6.5 有意不测

| 不测 | 原因 |
|---|---|
| install_loop_skills 工具实际触发 Claude Write | 这是 Claude Code 行为，不在我们能控制范围；冒烟覆盖 |
| zip 在不同操作系统的解压表现 | 标准 zip 格式，平台问题不归我们 |
| Web 下载在防火墙/代理后的可达性 | 部署环境问题 |
| Section ⑤ 视觉对齐 / 响应式 | 沿用现有 McpConnectWorkspace 样式，肉眼检查即可 |

---

## 7. 工作量估算 + 实施顺序

### 7.1 工作量

| 模块 | 改动行 | 工时 |
|---|---|---|
| `server/app/modules/loop_skills/templates/*`（5 个文件搬移 + 轻度修订） | +~400 行 | 0.5 h |
| `server/app/modules/loop_skills/service.py` | +~80 行 | 1 h |
| `server/app/modules/loop_skills/version.py` | +~15 行 | 0.1 h |
| `server/app/modules/loop_skills/router.py` | +~80 行 | 1 h |
| `server/app/modules/loop_skills/__init__.py` | +~5 行 | 0.05 h |
| `server/app/main.py` | +~10 行 | 0.1 h |
| `server/mcp/tools/action.py` | +~30 行 | 0.3 h |
| `server/tests/test_loop_skill_bundle.py` | +~180 行 | 1.5 h |
| `web/src/api/mcp.ts` | +~25 行 | 0.2 h |
| `web/src/features/mcp/McpConnectWorkspace.tsx` | +~120 行 | 2 h |
| 上游 spec/plan 加 source-of-truth 指引（轻量 commit） | +~20 行 | 0.2 h |
| 手工冒烟 + 调通 | — | 1 h |
| **合计** | **~960 行** | **~8 h（约 1 天）** |

### 7.2 实施顺序

```
1. 模板搬移到 templates/（独立，最容易）
   └ 顺手轻度修订（README 第一段、路径 hint）

2. version.py + service.py（无 IO，纯函数，TDD 友好）
   ├ test_build_bundle_*
   ├ test_build_zip_round_trip
   └ test_bundle_sha_is_known（依赖 templates 内容稳定，跑过一次填入 KNOWN）

3. router.py + main.py 挂载（依赖 service）
   ├ test_user_endpoint_requires_jwt
   └ test_mcp_endpoint_requires_mcp_token

4. MCP action.py install_loop_skills（依赖 router）

5. Web 前端 Section ⑤ + api/mcp.ts

6. 上游 spec/plan 加指引（任何时候都能加）

7. 冒烟 + push + PR
```

---

## 8. 与已有 spec / 实现的关系

| 参考 | 关系 |
|---|---|
| [`2026-06-24-goal-loop-engineering-design.md`](./2026-06-24-goal-loop-engineering-design.md) | **上游**——本设计是它的补丁；§5 的 SKILL.md 内容**搬到** `loop_skills/templates/`，spec 里加一条「source 已迁移」指引 |
| [`../plans/2026-06-24-goal-loop-engineering.md`](../plans/2026-06-24-goal-loop-engineering.md) | Tasks 5-9 内容同样迁移；本 PR 给它加注释指向 templates/ |
| [`2026-06-18-claude-code-loop-with-geo-mcp-design.md`](./2026-06-18-claude-code-loop-with-geo-mcp-design.md) | MCP server 架构基础——install_loop_skills 是该架构上的新工具，沿用其鉴权 / 飞书模式 |
| `web/src/features/mcp/McpConnectWorkspace.tsx` | 已有 4 个 section；本 PR 加 Section ⑤ |

---

## 9. Out of Scope（明确不做的）

- **distribute-loop / weekly-loop skill 包**：本 PR 只覆盖 `/goal` skill 包。后续可加 `bundle_name` 参数扩展
- **install 时本地差异比对**：让 Claude Code 自己做 Read+Write+diff，工具只返最新版
- **bundle 版本上线推送通知**：未来可以加飞书播报「新版可用」，本 PR 不做
- **用户级偏好（跳过已装版本提示）**：YAGNI；POC 期人脑记
- **公网无鉴权下载**：明确不做；最低门槛仍是 user 登录

---

## 10. Smoke Test 与上线门禁

- 后端 ruff / format / mypy / pytest 全过
- 7 个新测试通过
- 6.4 节 6 步手工冒烟全通过
- 至少 1 个非作者同事按 Section ⑤ 流程独立装好 + 跑通 `/goal`（上线门禁）
