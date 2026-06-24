# `/goal` Loop Skill 分发通道 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把已合并的 `/goal` Loop Engineering 那 5 个 `.claude/` 模板（README + slash command + 3 SKILL.md）搬进 git 服务端模板目录，并新增两条分发通道：(a) `McpConnectWorkspace` Section ⑤ 提供 ZIP 下载，(b) `install_loop_skills` MCP 工具供 Claude Code 一键自助安装。

**Architecture:** 新建 `server/app/modules/loop_skills/` 模块，源文件在 `templates/` 目录入 git。`service.build_bundle()` 是公用纯函数，被 user JWT 鉴权的 `/info` + `/download.zip` 端点和 MCP token 鉴权的 `/install-payload` 端点共用。前端在 `McpConnectWorkspace.tsx` 加 Section ⑤。带 `LOOP_SKILL_BUNDLE_VERSION` + `bundle_sha256`，CI 测试强制「改 templates 必同步 bump 版本」。

**Tech Stack:** FastAPI + Pydantic v2 / FastMCP (Python) / pathlib + zipfile / React 19 + lucide-react / pytest `@pytest.mark.mysql`

**Spec:** [`docs/superpowers/specs/2026-06-24-loop-skill-distribution-design.md`](../specs/2026-06-24-loop-skill-distribution-design.md)

**Branch:** `docs/loop-skill-distribution`（已从 `origin/main` 拉出，spec 已 commit 在 `3a64151`）

---

## Files to Touch

| 文件 | 操作 | 责任 |
|---|---|---|
| `server/app/modules/loop_skills/__init__.py` | 新建 | 模块标识，空文件 |
| `server/app/modules/loop_skills/version.py` | 新建 | `LOOP_SKILL_BUNDLE_VERSION` 字符串 + `KNOWN_BUNDLE_SHAS` 集合 |
| `server/app/modules/loop_skills/service.py` | 新建 | 纯函数：`build_bundle()` 扫描 templates → `SkillBundle`；`build_zip()` 打包成 ZIP bytes |
| `server/app/modules/loop_skills/router.py` | 新建 | 两组路由：`router`（user JWT）+ `mcp_router`（MCP token） |
| `server/app/modules/loop_skills/templates/README.md` | 新建（从本机 .claude/README.md 搬+轻度修订） | 模板：onboarding 文档 |
| `server/app/modules/loop_skills/templates/commands/goal.md` | 新建（从本机搬） | 模板：slash command |
| `server/app/modules/loop_skills/templates/skills/geo-goal-orchestrator/SKILL.md` | 新建（从本机搬） | 模板：主对话调度 |
| `server/app/modules/loop_skills/templates/skills/geo-article-writer/SKILL.md` | 新建（从本机搬） | 模板：writer subagent |
| `server/app/modules/loop_skills/templates/skills/geo-article-verifier/SKILL.md` | 新建（从本机搬） | 模板：verifier subagent |
| `server/app/main.py` | 修改 | 挂载 `loop_skills_user_router` + `loop_skills_mcp_router` |
| `server/mcp/tools/action.py` | 修改 | 加 `_aget` helper + `install_loop_skills()` MCP 工具 |
| `server/tests/test_loop_skill_bundle.py` | 新建 | 7 个测试：5 个 service + 2 个 endpoint 鉴权 |
| `web/src/api/mcp.ts` | 修改 | 加 `LoopSkillBundleInfo` 类型 + `getLoopSkillBundleInfo()` |
| `web/src/features/mcp/McpConnectWorkspace.tsx` | 修改 | Section ⑤ UI（版本信息卡 + 方式 A 复制提示 + 方式 B 下载按钮 + 文件清单展开） |
| `docs/superpowers/specs/2026-06-24-goal-loop-engineering-design.md` | 修改 | 加一段「§5 内容已迁移到 templates/，仅作历史快照」 |
| `docs/superpowers/plans/2026-06-24-goal-loop-engineering.md` | 修改 | 加一段「Tasks 5-9 内容已迁移到 templates/，仅作历史快照」 |

**关键边界**：

- `service.py` 纯函数无 IO 副作用（除了 read templates/）—— 不调 DB、不抛业务异常
- `router.py` 两组路由必须分开 —— 不同鉴权边界（user JWT vs MCP token）不能在同一 router 同时挂依赖
- 5 个 templates 文件**搬进 git** 后是唯一可信源；上游 spec / plan 里的 §5 / Tasks 5-9 内容降级为历史快照
- 前端下载走原生 `<a download>`，**不要** fetch + blob（避免大文件全量加载到内存）

---

## Task 1: 迁移 5 个 templates 到服务端模板目录

**Files:**
- Create: `server/app/modules/loop_skills/templates/README.md`
- Create: `server/app/modules/loop_skills/templates/commands/goal.md`
- Create: `server/app/modules/loop_skills/templates/skills/geo-goal-orchestrator/SKILL.md`
- Create: `server/app/modules/loop_skills/templates/skills/geo-article-writer/SKILL.md`
- Create: `server/app/modules/loop_skills/templates/skills/geo-article-verifier/SKILL.md`

源在本机 `.claude/`（gitignored）；目标在新模块目录。**轻度修订**：把 README 首段从「本目录里的 skill / command / README **不进 git**」改为「本目录是 `geo-collab` 服务端分发的 `/goal` Loop skill 模板」。

- [ ] **Step 1: 在 dev 容器 / 本地确认 5 个源文件存在**

```bash
ls .claude/README.md .claude/commands/goal.md .claude/skills/geo-goal-orchestrator/SKILL.md .claude/skills/geo-article-writer/SKILL.md .claude/skills/geo-article-verifier/SKILL.md
```

如果任一文件缺失，到 `docs/superpowers/specs/2026-06-24-goal-loop-engineering-design.md` §5 + `docs/superpowers/plans/2026-06-24-goal-loop-engineering.md` Tasks 5-9 复制内容补齐，再继续。

- [ ] **Step 2: 复制 5 个文件到 templates/，保留目录结构**

用 PowerShell（Windows host）或 cp（容器内）：

```bash
# Windows PowerShell (host)
New-Item -ItemType Directory -Force server/app/modules/loop_skills/templates/commands
New-Item -ItemType Directory -Force server/app/modules/loop_skills/templates/skills/geo-goal-orchestrator
New-Item -ItemType Directory -Force server/app/modules/loop_skills/templates/skills/geo-article-writer
New-Item -ItemType Directory -Force server/app/modules/loop_skills/templates/skills/geo-article-verifier
Copy-Item .claude/README.md server/app/modules/loop_skills/templates/README.md
Copy-Item .claude/commands/goal.md server/app/modules/loop_skills/templates/commands/goal.md
Copy-Item .claude/skills/geo-goal-orchestrator/SKILL.md server/app/modules/loop_skills/templates/skills/geo-goal-orchestrator/SKILL.md
Copy-Item .claude/skills/geo-article-writer/SKILL.md server/app/modules/loop_skills/templates/skills/geo-article-writer/SKILL.md
Copy-Item .claude/skills/geo-article-verifier/SKILL.md server/app/modules/loop_skills/templates/skills/geo-article-verifier/SKILL.md
```

- [ ] **Step 3: 修订 templates/README.md 首段**

用 Edit 工具修改 `server/app/modules/loop_skills/templates/README.md`，把第一段（"# `.claude/` — Geo 协作平台 Claude Code 工程目录（**本地不入库**）" 那块）替换为：

```markdown
# `.claude/` — Geo 协作平台 Claude Code 工程目录

> 本目录的所有文件是 `geo-collab` 服务端通过 `/api/mcp/loop-skill-bundle/`
> + `install_loop_skills` MCP 工具分发的「模板」（服务端正本在仓库
> `server/app/modules/loop_skills/templates/`）。你解压 / 安装到本机后
> 可以按需修改 —— 本地改动不会被服务端覆盖，下次 install 时 Claude Code
> 会询问你是否覆盖。
>
> 装好后：重启 Claude Code → 输入 `/goal 帮我产出 1 篇国风游戏文章作为冒烟`
```

保留 README 其它内容（5 步 onboarding / 主对话日志 / 复用路径 / 排障表）不动。

- [ ] **Step 4: 修订 templates/skills/geo-article-writer/SKILL.md 「加新矩阵的方法」段**

把这段：

```markdown
## 加新矩阵的方法（给团队同事）

1. 复制本目录为 `.claude/skills/geo-article-writer-<matrix-code>/`
2. **只改本文件「矩阵特例」这一节**；其它段落不动
3. 调用时 `/goal matrix=<matrix-code> ...`，orchestrator 会装载对应目录的 SKILL.md
```

替换为：

```markdown
## 加新矩阵的方法（给团队同事）

1. 在你本机 `~/.claude/skills/` 或 `<repo>/.claude/skills/`（取决于装在哪一级）
   下复制本目录为 `geo-article-writer-<matrix-code>/`
2. **只改本文件「矩阵特例」这一节**；其它段落不动
3. 调用时 `/goal matrix=<matrix-code> ...`，orchestrator 会装载对应目录的 SKILL.md

> 服务端正本（`server/app/modules/loop_skills/templates/`）默认只有
> 餐厅养成记矩阵；新增矩阵建议在本机做，避免污染共享分发包。
```

- [ ] **Step 5: 确认 5 个文件就位 + 内容合理**

```bash
git status
```

应该看到 5 个新文件（status: untracked）：

```
server/app/modules/loop_skills/templates/README.md
server/app/modules/loop_skills/templates/commands/goal.md
server/app/modules/loop_skills/templates/skills/geo-goal-orchestrator/SKILL.md
server/app/modules/loop_skills/templates/skills/geo-article-writer/SKILL.md
server/app/modules/loop_skills/templates/skills/geo-article-verifier/SKILL.md
```

- [ ] **Step 6: Commit**

```bash
git add server/app/modules/loop_skills/templates/
git commit -m "$(cat <<'EOF'
feat(loop_skills): 5 个 .claude/ 模板搬入 git templates/ 作为分发正本

README/goal command/3 个 SKILL.md 从本机 .claude/（gitignored）搬入服务端
templates/ 目录，作为后续 Web 下载 + install_loop_skills MCP 工具的唯一
可信源。README 首段 + writer skill 「加新矩阵」段做了轻度修订，反映
「服务端分发 + 本地可改」的双层模型。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: 模块骨架 + `build_bundle()` TDD

**Files:**
- Create: `server/app/modules/loop_skills/__init__.py`
- Create: `server/app/modules/loop_skills/version.py`
- Create: `server/app/modules/loop_skills/service.py`
- Create: `server/tests/test_loop_skill_bundle.py`

- [ ] **Step 1: 创建空 `__init__.py`**

```python
# server/app/modules/loop_skills/__init__.py
```

文件留空即可。

- [ ] **Step 2: 创建 `version.py` 骨架**

`server/app/modules/loop_skills/version.py`：

```python
"""手工维护的 bundle 版本号 + 已审核 sha 集合。

CI 测试 (test_loop_skill_bundle.py::test_bundle_sha_is_known) 会校验：
如果 build_bundle().bundle_sha256 没记录在 KNOWN_BUNDLE_SHAS 集合里，
fail + 提示开发者：把新 sha 加进 KNOWN_BUNDLE_SHAS 并 bump
LOOP_SKILL_BUNDLE_VERSION，强制「改模板必同步 bump 版本」纪律。
"""

LOOP_SKILL_BUNDLE_VERSION = "2026-06-24-v1"

# 在 Task 4 跑 build_bundle 拿到 sha 后填进来；Task 2 / 3 阶段保持空集
KNOWN_BUNDLE_SHAS: frozenset[str] = frozenset()
```

- [ ] **Step 3: 写失败的测试**

创建 `server/tests/test_loop_skill_bundle.py`：

```python
"""loop_skills 模块测试：service 纯函数 + 端点鉴权。

测试覆盖（spec §6.3）：
1. build_bundle 返回 5 个预期文件
2. bundle_sha256 稳定（同一份模板调两次结果一致）
3. bundle_sha256 在内容变更时必变
4. KNOWN_BUNDLE_SHAS 必须包含当前 sha（Task 4 加）
5. build_zip 完整 round-trip（Task 3 加）
6. /info 端点要 user JWT（Task 5 加）
7. /install-payload 端点要 MCP token（Task 6 加）
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest


def test_build_bundle_lists_all_template_files():
    """build_bundle 返回的 files 包含 5 个预期 path。"""
    from server.app.modules.loop_skills.service import build_bundle

    bundle = build_bundle()
    paths = {f.path for f in bundle.files}
    assert paths == {
        "README.md",
        "commands/goal.md",
        "skills/geo-goal-orchestrator/SKILL.md",
        "skills/geo-article-writer/SKILL.md",
        "skills/geo-article-verifier/SKILL.md",
    }
    # 每个文件都该有非空内容 + 正确 sha + 正数 size
    for f in bundle.files:
        assert f.content, f"{f.path} content empty"
        assert len(f.sha256) == 64, f"{f.path} sha not hex64"
        assert f.size > 0, f"{f.path} size <= 0"


def test_build_bundle_sha_stable():
    """同一份模板调两次 build_bundle，bundle_sha256 完全一致。"""
    from server.app.modules.loop_skills.service import build_bundle

    a = build_bundle()
    b = build_bundle()
    assert a.bundle_sha256 == b.bundle_sha256


def test_build_bundle_sha_changes_when_content_changes(tmp_path, monkeypatch):
    """改一个模板文件 → bundle_sha256 必变。

    用 tmp_path 复制 templates/ 到临时目录后 monkeypatch `service._TEMPLATES_DIR`
    指过去；改临时目录里的文件，不污染 git 工作树。
    """
    from server.app.modules.loop_skills import service

    # 复制现有 templates 到 tmp_path
    src = Path(__file__).parent.parent / "app" / "modules" / "loop_skills" / "templates"
    dst = tmp_path / "templates"
    shutil.copytree(src, dst)

    # 把 _TEMPLATES_DIR 指到 tmp_path/templates
    monkeypatch.setattr(service, "_TEMPLATES_DIR", dst)

    before = service.build_bundle().bundle_sha256

    # 改一个文件
    readme = dst / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "\n\n<!-- test marker -->\n", encoding="utf-8")

    after = service.build_bundle().bundle_sha256
    assert before != after, "bundle_sha256 should change when a template changes"
```

- [ ] **Step 4: 跑测试，确认 3 个全 fail**

```bash
docker compose exec app pytest server/tests/test_loop_skill_bundle.py -q
```

**预期**：3 个测试 fail，提示 `ImportError: cannot import name 'build_bundle' from 'server.app.modules.loop_skills.service'`（service.py 还没创建）。

- [ ] **Step 5: 创建 `service.py` 实现 `build_bundle`**

`server/app/modules/loop_skills/service.py`：

```python
"""loop_skills.service —— 服务端「正本」模板的扫描 + 打包逻辑。

无 IO 副作用、无 DB 访问；纯文件读 + 内存 zip。Web 端 + MCP 端共用。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from server.app.modules.loop_skills.version import LOOP_SKILL_BUNDLE_VERSION

_TEMPLATES_DIR = Path(__file__).parent / "templates"


@dataclass(frozen=True)
class SkillFile:
    """单个模板文件的元信息 + 内容。"""

    path: str  # 相对 templates/ 的 posix 路径，如 "skills/geo-article-writer/SKILL.md"
    size: int  # bytes
    sha256: str  # hex digest
    content: str  # utf-8 文本


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
```

- [ ] **Step 6: 跑测试，确认 3 个全 pass**

```bash
docker compose exec app pytest server/tests/test_loop_skill_bundle.py -q
```

**预期**：`3 passed`。

- [ ] **Step 7: ruff + format clean**

```bash
docker compose exec app ruff check server/app/modules/loop_skills/ server/tests/test_loop_skill_bundle.py
docker compose exec app ruff format --check server/app/modules/loop_skills/ server/tests/test_loop_skill_bundle.py
```

如果 format 报差异，去掉 `--check` 直接改写。

- [ ] **Step 8: Commit**

```bash
git add server/app/modules/loop_skills/__init__.py server/app/modules/loop_skills/version.py server/app/modules/loop_skills/service.py server/tests/test_loop_skill_bundle.py
git commit -m "$(cat <<'EOF'
feat(loop_skills): service.build_bundle() + 3 个 TDD 测试

纯函数扫描 templates/ 下所有 utf-8 文件，按 posix 相对路径排序后产出
SkillBundle (version + files[] + bundle_sha256)。bundle_sha256 = sha256 of
sorted (path, file_sha) 串接 —— 任一文件内容或路径变更必导致 sha 变更。

KNOWN_BUNDLE_SHAS 在 Task 4 填，Task 2/3 阶段先留空集 + 跳过 sha 校验测试。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `build_zip()` TDD

**Files:**
- Modify: `server/app/modules/loop_skills/service.py`（加 `build_zip` 函数 + 顶部 import）
- Modify: `server/tests/test_loop_skill_bundle.py`（追加 1 个测试）

- [ ] **Step 1: 追加失败测试**

在 `server/tests/test_loop_skill_bundle.py` 末尾追加：

```python
def test_build_zip_round_trip():
    """build_zip 解压出来的文件名 + 内容跟 bundle.files 一对一吻合。"""
    import io
    import zipfile

    from server.app.modules.loop_skills.service import build_bundle, build_zip

    bundle = build_bundle()
    data = build_zip(bundle)

    # 解压验证
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zip_names = set(zf.namelist())
        bundle_paths = {f.path for f in bundle.files}
        assert zip_names == bundle_paths, "zip entries should match bundle paths"

        for f in bundle.files:
            with zf.open(f.path) as fp:
                content = fp.read().decode("utf-8")
            assert content == f.content, f"{f.path} content mismatch after round-trip"
```

- [ ] **Step 2: 跑测试，确认新测试 fail**

```bash
docker compose exec app pytest server/tests/test_loop_skill_bundle.py::test_build_zip_round_trip -v
```

**预期**：fail，提示 `ImportError: cannot import name 'build_zip'`。

- [ ] **Step 3: 在 service.py 加 import + build_zip 函数**

修改 `server/app/modules/loop_skills/service.py`，顶部 import 区追加：

```python
import io
import zipfile
```

文件末尾追加：

```python
def build_zip(bundle: SkillBundle) -> bytes:
    """打包成 zip bytes。文件路径保持模板的目录结构（不带顶层前缀）。

    解压到 .claude/ 后直接是 README.md / commands/ / skills/。
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in bundle.files:
            zf.writestr(f.path, f.content)
    return buf.getvalue()
```

- [ ] **Step 4: 跑测试，确认全部 pass**

```bash
docker compose exec app pytest server/tests/test_loop_skill_bundle.py -q
```

**预期**：`4 passed`。

- [ ] **Step 5: ruff + format clean**

```bash
docker compose exec app ruff check server/app/modules/loop_skills/service.py server/tests/test_loop_skill_bundle.py
docker compose exec app ruff format --check server/app/modules/loop_skills/service.py server/tests/test_loop_skill_bundle.py
```

- [ ] **Step 6: Commit**

```bash
git add server/app/modules/loop_skills/service.py server/tests/test_loop_skill_bundle.py
git commit -m "$(cat <<'EOF'
feat(loop_skills): build_zip() — bundle 转 ZIP bytes + round-trip 测试

DEFLATED 压缩、顶层不带前缀目录（解压到 .claude/ 后直接是 README.md /
commands/ / skills/）；round-trip 测试验证解压后文件路径 + utf-8 内容
与 bundle.files 一对一一致。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: 填 `KNOWN_BUNDLE_SHAS` + sha 校验测试

**Files:**
- Modify: `server/app/modules/loop_skills/version.py`（填 KNOWN_BUNDLE_SHAS）
- Modify: `server/tests/test_loop_skill_bundle.py`（追加 1 个测试）

- [ ] **Step 1: 追加测试**

在 `server/tests/test_loop_skill_bundle.py` 末尾追加：

```python
def test_bundle_sha_is_known():
    """当前 build_bundle 的 sha 必须在 KNOWN_BUNDLE_SHAS 集合里。

    失败提示：改 templates/ 后必须同步把新 sha 加进 KNOWN_BUNDLE_SHAS
    并 bump LOOP_SKILL_BUNDLE_VERSION。这是「改模板必同步 bump 版本」纪律。
    """
    from server.app.modules.loop_skills.service import build_bundle
    from server.app.modules.loop_skills.version import KNOWN_BUNDLE_SHAS

    current = build_bundle().bundle_sha256
    assert current in KNOWN_BUNDLE_SHAS, (
        f"Bundle sha256 = {current!r} not in KNOWN_BUNDLE_SHAS. "
        f"If you changed templates/, bump LOOP_SKILL_BUNDLE_VERSION + add this sha to KNOWN_BUNDLE_SHAS."
    )
```

- [ ] **Step 2: 跑测试拿当前 sha**

```bash
docker compose exec app pytest server/tests/test_loop_skill_bundle.py::test_bundle_sha_is_known -v
```

**预期**：fail，错误信息会打出 `Bundle sha256 = '<some hex>' not in KNOWN_BUNDLE_SHAS`。**复制那个 sha 串**到下一步。

- [ ] **Step 3: 把 sha 填进 `version.py`**

修改 `server/app/modules/loop_skills/version.py`，把 `KNOWN_BUNDLE_SHAS` 从空集改为：

```python
KNOWN_BUNDLE_SHAS: frozenset[str] = frozenset({
    "<上一步打印的 sha 串，64 字符 hex>",
})
```

把 `<上一步打印的 sha 串>` 替换为 Step 2 输出里的真实 sha 值。

- [ ] **Step 4: 跑测试，确认 5 个全 pass**

```bash
docker compose exec app pytest server/tests/test_loop_skill_bundle.py -q
```

**预期**：`5 passed`。

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/loop_skills/version.py server/tests/test_loop_skill_bundle.py
git commit -m "$(cat <<'EOF'
test(loop_skills): test_bundle_sha_is_known + 首版 KNOWN_BUNDLE_SHAS

纪律性测试：build_bundle().bundle_sha256 必须在 version.py 的 KNOWN 集合
里。改 templates/ 后 CI 立刻 fail，提示开发者「bump 版本 + 加新 sha」，
防止模板内容默默漂移。首版 sha 已根据 Task 1-3 的 templates 计算填入。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `router.py` user JWT 端点 + 鉴权测试

**Files:**
- Create: `server/app/modules/loop_skills/router.py`
- Modify: `server/tests/test_loop_skill_bundle.py`（追加 2 个测试）

- [ ] **Step 1: 创建 `router.py` 含 user 路由**

`server/app/modules/loop_skills/router.py`：

```python
"""loop_skills HTTP 路由。

两组路由：
- router (user JWT)：/info + /download.zip，给 Web Section ⑤ 用
- mcp_router (MCP token)：/install-payload，给 install_loop_skills 工具用（Task 6 加）

两条用户群不同 + 鉴权不同，必须拆 router；service 层 build_bundle 共用。
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel

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
            "Content-Disposition": (f'attachment; filename="geo-loop-skills-{b.version}.zip"'),
            "X-Bundle-Version": b.version,
            "X-Bundle-Sha256": b.bundle_sha256,
        },
    )
```

- [ ] **Step 2: 在 main.py 挂载 user 路由**

修改 `server/app/main.py`：

(a) 顶部 import 区追加：

```python
from server.app.modules.loop_skills.router import router as loop_skills_user_router
```

(b) 在 `mcp_connect_user_router` 挂载附近（约 main.py:213 之后，跟 user JWT MCP 同组）追加：

```python
# Loop skill 包分发（Web Section ⑤ 用）—— user JWT 鉴权
app.include_router(
    loop_skills_user_router,
    prefix="/api/mcp",
    tags=["loop-skills"],
    dependencies=[Depends(get_current_user)],
)
```

- [ ] **Step 3: 追加 2 个测试**

在 `server/tests/test_loop_skill_bundle.py` 末尾追加：

```python
@pytest.mark.mysql
def test_user_info_endpoint_requires_jwt(monkeypatch):
    """/api/mcp/loop-skill-bundle/info 不带 cookie → 401。"""
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        # 用一个干净的 client（不带 auth cookie）请求
        r = test_app.client.get("/api/mcp/loop-skill-bundle/info", cookies={})
        assert r.status_code == 401
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_user_info_endpoint_returns_bundle_when_authed(monkeypatch):
    """带 JWT 请求 → 200，返回 {version, bundle_sha256, files, install_hint}。"""
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        r = test_app.client.get("/api/mcp/loop-skill-bundle/info")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["version"]
        assert len(body["bundle_sha256"]) == 64
        assert len(body["files"]) == 5
        assert body["install_hint"]
    finally:
        test_app.cleanup()
```

> 注：`build_test_app` 默认会写入 admin user JWT cookie，所以第二个测试不需要额外登录步骤；第一个测试用 `cookies={}` 显式清空。

- [ ] **Step 4: 跑测试，确认 7 个全 pass**

```bash
docker compose exec -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/test_loop_skill_bundle.py -q
```

**预期**：`7 passed`（5 个原有 + 2 个新增）。

- [ ] **Step 5: ruff + format clean**

```bash
docker compose exec app ruff check server/app/modules/loop_skills/router.py server/app/main.py server/tests/test_loop_skill_bundle.py
docker compose exec app ruff format --check server/app/modules/loop_skills/router.py server/app/main.py server/tests/test_loop_skill_bundle.py
```

- [ ] **Step 6: Commit**

```bash
git add server/app/modules/loop_skills/router.py server/app/main.py server/tests/test_loop_skill_bundle.py
git commit -m "$(cat <<'EOF'
feat(loop_skills): user JWT 端点 /info + /download.zip + 2 个集成测试

Web Section ⑤ 用：/info 返回 {version, sha256, files[], install_hint}，
/download.zip 流式 ZIP + Content-Disposition + X-Bundle-* 响应头。两个
端点都通过 main.py include 时挂 get_current_user 依赖；测试验证 401 +
正常 200 返回结构。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: `router.py` MCP token 端点 + 鉴权测试

**Files:**
- Modify: `server/app/modules/loop_skills/router.py`（追加 mcp_router）
- Modify: `server/app/main.py`（挂载 mcp_router）
- Modify: `server/tests/test_loop_skill_bundle.py`（追加 1 个测试）

- [ ] **Step 1: 追加 `mcp_router` 到 router.py**

修改 `server/app/modules/loop_skills/router.py`：

(a) 顶部 import 区追加：

```python
from fastapi import Depends

from server.app.core.mcp_auth import require_mcp_token
```

(b) 在 user `router` 定义之后，文件末尾追加：

```python
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

- [ ] **Step 2: 在 main.py 挂载 mcp_router**

修改 `server/app/main.py`：

(a) 修改顶部 import 行（Task 5 加的那行）为：

```python
from server.app.modules.loop_skills.router import (
    mcp_router as loop_skills_mcp_router,
    router as loop_skills_user_router,
)
```

(b) 在 Task 5 加的 `loop_skills_user_router` include 之后追加：

```python
# MCP token 鉴权 (router 自带 dependency)
app.include_router(
    loop_skills_mcp_router,
    prefix="/api/mcp",
    tags=["loop-skills-mcp"],
)
```

- [ ] **Step 3: 追加测试**

在 `server/tests/test_loop_skill_bundle.py` 末尾追加：

```python
@pytest.mark.mysql
def test_mcp_install_payload_endpoint_requires_mcp_token(monkeypatch):
    """/api/mcp/loop-skill-bundle/install-payload 不带 X-MCP-Token → 401。"""
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        r = test_app.client.get("/api/mcp/loop-skill-bundle/install-payload")
        assert r.status_code == 401
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_mcp_install_payload_returns_full_files_when_authed(monkeypatch):
    """带 MCP token → 200，返回 {ok, data:{files[{path,content,sha256,size}], ...}, error=None}。"""
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        r = test_app.client.get(
            "/api/mcp/loop-skill-bundle/install-payload",
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["error"] is None
        data = body["data"]
        assert data["version"]
        assert len(data["bundle_sha256"]) == 64
        assert data["install_hint"]
        assert len(data["files"]) == 5
        # 每个文件 dict 必须含 4 个字段 + content 非空
        for f in data["files"]:
            assert {"path", "content", "sha256", "size"} <= set(f.keys())
            assert f["content"]
    finally:
        test_app.cleanup()
```

- [ ] **Step 4: 跑测试，确认 9 个全 pass**

```bash
docker compose exec -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/test_loop_skill_bundle.py -q
```

**预期**：`9 passed`（7 + 2）。

- [ ] **Step 5: ruff + format clean**

```bash
docker compose exec app ruff check server/app/modules/loop_skills/router.py server/app/main.py server/tests/test_loop_skill_bundle.py
docker compose exec app ruff format --check server/app/modules/loop_skills/router.py server/app/main.py server/tests/test_loop_skill_bundle.py
```

- [ ] **Step 6: Commit**

```bash
git add server/app/modules/loop_skills/router.py server/app/main.py server/tests/test_loop_skill_bundle.py
git commit -m "$(cat <<'EOF'
feat(loop_skills): MCP token 端点 /install-payload + 2 个鉴权测试

install_loop_skills MCP 工具的后端入口，返回完整 5 个文件的 dict + 给
Claude Code 用的 install_hint 英文指引（中文 install_hint 是给前端 Section ⑤
人类用户看的，这里是英文给 Claude 看）。鉴权走 router-level
require_mcp_token；测试验证 401 + 正常 200 返回 {ok, data, error} 结构。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: `install_loop_skills` MCP 工具

**Files:**
- Modify: `server/mcp/tools/action.py`（追加 `_aget` helper + `install_loop_skills` 工具）

- [ ] **Step 1: 加 `_aget` helper（如果还没有）**

读 `server/mcp/tools/action.py` 顶部约 50 行，确认当前是否已有 `_aget`。

如果**没有**（只有 `_apost`），在 `_apost` 函数后面追加：

```python
async def _aget(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """同步 GET 丢线程池跑，避免阻塞事件循环（见 catalog.py 的自调用死锁说明）。"""

    def _impl() -> dict[str, Any]:
        try:
            return _ok(_client().get(path, params=params))
        except ApiError as exc:
            return _fail(str(exc))

    return await anyio.to_thread.run_sync(_impl)
```

- [ ] **Step 2: 追加 `install_loop_skills` 工具到文件末尾**

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
    # 后端 /install-payload 已经返回了完整 {ok, data, error} 结构，这里直接透传。
    # _aget 默认会把 GeoApiClient.get 的返回值再 wrap 一层 _ok()，因此
    # 实际拿到的是 {"ok": True, "data": {"ok": True, "data": {...}, "error": None}, "error": None}。
    # 把内层剥出来，让 LLM 看到的契约干净。
    raw = await _aget("/api/mcp/loop-skill-bundle/install-payload")
    if not raw.get("ok"):
        return raw  # 透传 _fail 结构
    inner = raw.get("data") or {}
    if isinstance(inner, dict) and "ok" in inner and "data" in inner:
        return inner  # 后端已经返了 {ok, data, error}
    return raw
```

> 关于「双层 ok 剥层」逻辑：`_aget` 的实现总是 wrap 一次 `_ok`，但后端 `/install-payload` 已经返回了 `{ok, data, error}` 形状，导致双层嵌套。这段防御性剥层让 LLM-facing 契约干净，且如果未来 `_aget` 行为改变也兼容。

- [ ] **Step 3: ruff + import 自检**

```bash
docker compose exec app ruff check server/mcp/tools/action.py
docker compose exec app ruff format --check server/mcp/tools/action.py
docker compose exec app python -c "import server.mcp.tools.action; print('ok')"
```

**预期**：`ok`。

- [ ] **Step 4: Commit**

```bash
git add server/mcp/tools/action.py
git commit -m "$(cat <<'EOF'
feat(mcp): install_loop_skills 工具 — Claude Code 一键装 /goal skill 包

action 组从 6 个工具增到 7 个。async + _aget 薄壳模式（与 catalog.py 一致），
转发到后端 /api/mcp/loop-skill-bundle/install-payload；剥掉 _aget 默认 wrap
的外层 ok，让 LLM-facing 返回值是干净的 {ok, data:{version, sha256,
install_hint, files[]}, error}。

工具 docstring 明确告诉 Claude：拿到 files dict 后用 Write 写到本机
.claude/，写前确认目标位置（项目级 vs 全局）+ 询问是否覆盖已有文件。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: 前端 `api/mcp.ts` 加 loop skill bundle 客户端

**Files:**
- Modify: `web/src/api/mcp.ts`

- [ ] **Step 1: 在文件末尾追加类型 + 函数**

读 `web/src/api/mcp.ts` 看末尾位置，在最后一个 `export` 之后追加：

```typescript
// ─────────────────────────────────────────────────────────────────────────────
// Loop skill bundle distribution（Section ⑤ 用）
// ─────────────────────────────────────────────────────────────────────────────

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
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
  return res.json();
}

/** ZIP 下载 URL，直接用 <a href={url} download> 触发浏览器下载（不要 fetch+blob）。 */
export const LOOP_SKILL_BUNDLE_DOWNLOAD_URL = "/api/mcp/loop-skill-bundle/download.zip";
```

- [ ] **Step 2: typecheck**

```bash
pnpm --filter @geo/web typecheck
```

**预期**：0 error。

- [ ] **Step 3: Commit**

```bash
git add web/src/api/mcp.ts
git commit -m "$(cat <<'EOF'
feat(web/mcp): api 客户端加 getLoopSkillBundleInfo + 下载 URL 常量

Section ⑤ 用：拉 /info 拿版本 + 文件清单 + install_hint 走 fetch；ZIP 下载
直接用 <a download> 不走 fetch+blob 避免大文件全量加载到内存。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: 前端 `McpConnectWorkspace.tsx` 加 Section ⑤

**Files:**
- Modify: `web/src/features/mcp/McpConnectWorkspace.tsx`

- [ ] **Step 1: 顶部 import 区追加**

在现有 `import` 区追加：

```typescript
import { Download, FileText, Package } from "lucide-react";
import {
  getLoopSkillBundleInfo,
  LOOP_SKILL_BUNDLE_DOWNLOAD_URL,
  type LoopSkillBundleInfo,
} from "../../api/mcp";
```

- [ ] **Step 2: 组件内加状态 + 加载副作用**

在 `McpConnectWorkspace` 函数体内、现有 `useState` / `useEffect` 区域之后追加：

```typescript
  // Section ⑤ — loop skill bundle
  const [bundle, setBundle] = useState<LoopSkillBundleInfo | null>(null);
  const [bundleError, setBundleError] = useState("");
  const [bundleLoading, setBundleLoading] = useState(true);
  const [bundleFilesExpanded, setBundleFilesExpanded] = useState(false);
  const [installPromptCopied, setInstallPromptCopied] = useState(false);
  const [bundleShaCopied, setBundleShaCopied] = useState(false);

  const refreshBundle = useCallback(async () => {
    setBundleLoading(true);
    try {
      const data = await getLoopSkillBundleInfo();
      setBundle(data);
      setBundleError("");
    } catch (err) {
      setBundle(null);
      setBundleError(err instanceof Error ? err.message : "加载 skill 包元信息失败");
    } finally {
      setBundleLoading(false);
    }
  }, []);

  useEffect(() => {
    void refreshBundle();
  }, [refreshBundle]);

  const totalBundleBytes = useMemo(
    () => bundle?.files.reduce((sum, f) => sum + f.size, 0) ?? 0,
    [bundle],
  );

  const onCopyInstallPrompt = useCallback(async () => {
    const prompt = "帮我装 geo loop skills";
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(prompt);
      } else {
        const ta = document.createElement("textarea");
        ta.value = prompt;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }
      setInstallPromptCopied(true);
      toast("已复制提示语", "success");
      setTimeout(() => setInstallPromptCopied(false), 1500);
    } catch {
      toast("复制失败，请手动选择文本", "error");
    }
  }, [toast]);

  const onCopyBundleSha = useCallback(async () => {
    if (!bundle) return;
    try {
      await navigator.clipboard.writeText(bundle.bundle_sha256);
      setBundleShaCopied(true);
      toast("已复制 SHA-256", "success");
      setTimeout(() => setBundleShaCopied(false), 1500);
    } catch {
      toast("复制失败", "error");
    }
  }, [bundle, toast]);

  const mcpConnected = testResult?.ok === true;
```

- [ ] **Step 3: 在 JSX 渲染部分加 Section ⑤**

找到现有 `<div style={{ display: "grid", gap: 16, maxWidth: 860 }}>` 包裹的 4 个 section（Section ① 概览 / ② 服务端状态 / ③ 配置 / ④ 测试连接），在第 4 个 section 之后、grid 闭合 `</div>` 之前追加：

```tsx
        {/* Section ⑤ 装 /goal 自动生文 Skills ─────────────────────────── */}
        <section className="panel">
          <h2 style={{ marginBottom: 12, display: "flex", alignItems: "center", gap: 8 }}>
            <Package size={18} /> ⑤ 装 /goal 自动生文 Skills（可选）
          </h2>
          <p style={{ color: "var(--fg-2)", lineHeight: 1.7, marginBottom: 12 }}>
            想用 <code style={inlineCode}>/goal</code> 一句话让 Claude 帮你跑生文 Loop？
            需要先在本机 <code style={inlineCode}>.claude/</code> 装 5 个 skill 模板。
            两种方式任选其一。
          </p>

          {/* 版本信息卡 */}
          {bundleLoading && (
            <div style={{ color: "var(--fg-2)", fontSize: 13 }}>
              <Loader2 size={14} className="hotSpin" /> 加载 skill 包元信息中...
            </div>
          )}
          {bundleError && (
            <div
              style={{
                padding: 12,
                borderRadius: 6,
                background: "var(--bg-danger-soft, rgba(239,68,68,0.1))",
                color: "var(--fg-danger, #ef4444)",
                fontSize: 13,
                marginBottom: 12,
              }}
            >
              <AlertTriangle size={14} style={{ verticalAlign: "middle", marginRight: 6 }} />
              {bundleError}
              <button
                type="button"
                onClick={() => void refreshBundle()}
                style={{
                  marginLeft: 12,
                  padding: "2px 8px",
                  background: "transparent",
                  border: "1px solid currentColor",
                  borderRadius: 4,
                  cursor: "pointer",
                  color: "inherit",
                }}
              >
                重试
              </button>
            </div>
          )}
          {bundle && (
            <div
              style={{
                padding: 12,
                borderRadius: 6,
                background: "var(--bg-2)",
                marginBottom: 16,
                fontSize: 13,
                lineHeight: 1.8,
              }}
            >
              <div>
                版本：<strong style={{ color: "var(--fg)" }}>{bundle.version}</strong>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span>SHA-256:</span>
                <code style={{ ...inlineCode, fontSize: 12 }}>
                  {bundle.bundle_sha256.slice(0, 16)}...
                </code>
                <button
                  type="button"
                  onClick={() => void onCopyBundleSha()}
                  style={{
                    padding: "2px 8px",
                    background: "transparent",
                    border: "1px solid var(--border)",
                    borderRadius: 4,
                    cursor: "pointer",
                    fontSize: 12,
                  }}
                >
                  {bundleShaCopied ? <CheckCircle2 size={12} /> : <Copy size={12} />}
                  {bundleShaCopied ? " 已复制" : " 复制"}
                </button>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span>
                  含 <strong style={{ color: "var(--fg)" }}>{bundle.files.length}</strong> 个文件 / 总{" "}
                  {(totalBundleBytes / 1024).toFixed(1)} KB
                </span>
                <button
                  type="button"
                  onClick={() => setBundleFilesExpanded((v) => !v)}
                  style={{
                    padding: "2px 8px",
                    background: "transparent",
                    border: "1px solid var(--border)",
                    borderRadius: 4,
                    cursor: "pointer",
                    fontSize: 12,
                  }}
                >
                  <FileText size={12} /> {bundleFilesExpanded ? "收起" : "展开"}清单
                </button>
              </div>
              {bundleFilesExpanded && (
                <table style={{ marginTop: 8, fontSize: 12, width: "100%" }}>
                  <thead>
                    <tr style={{ textAlign: "left", color: "var(--fg-2)" }}>
                      <th style={{ paddingRight: 12 }}>path</th>
                      <th style={{ paddingRight: 12 }}>size</th>
                      <th>sha256[:12]</th>
                    </tr>
                  </thead>
                  <tbody>
                    {bundle.files.map((f) => (
                      <tr key={f.path}>
                        <td style={{ paddingRight: 12 }}>
                          <code style={inlineCode}>{f.path}</code>
                        </td>
                        <td style={{ paddingRight: 12 }}>{f.size}</td>
                        <td>
                          <code style={inlineCode}>{f.sha256.slice(0, 12)}</code>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}

          {/* 方式 A：让 Claude Code 自己装 */}
          <div
            style={{
              padding: 12,
              borderRadius: 6,
              background: "var(--bg-2)",
              marginBottom: 12,
              opacity: mcpConnected ? 1 : 0.6,
            }}
          >
            <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 8 }}>
              方式 A · 让 Claude Code 自己装（推荐）
            </div>
            {!mcpConnected && (
              <div style={{ fontSize: 12, color: "var(--fg-warning, #f59e0b)", marginBottom: 8 }}>
                请先到上面 ④「测试连接」完成 MCP token 验证。
              </div>
            )}
            <div style={{ fontSize: 13, color: "var(--fg-2)", marginBottom: 8 }}>
              在 Claude Code 主对话里说：
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <code
                style={{
                  flex: 1,
                  padding: "8px 12px",
                  background: "var(--bg-1)",
                  borderRadius: 4,
                  fontSize: 13,
                }}
              >
                帮我装 geo loop skills
              </code>
              <button
                type="button"
                onClick={() => void onCopyInstallPrompt()}
                disabled={!mcpConnected}
                style={{
                  padding: "6px 10px",
                  background: "transparent",
                  border: "1px solid var(--border)",
                  borderRadius: 4,
                  cursor: mcpConnected ? "pointer" : "not-allowed",
                }}
              >
                {installPromptCopied ? <CheckCircle2 size={14} /> : <Copy size={14} />}
              </button>
            </div>
            <div style={{ fontSize: 12, color: "var(--fg-2)", marginTop: 8, lineHeight: 1.6 }}>
              Claude 会调 <code style={inlineCode}>install_loop_skills</code> 工具拿到 5 个文件 +
              询问你装到全局 <code style={inlineCode}>~/.claude/</code> 还是项目根{" "}
              <code style={inlineCode}>.claude/</code>，然后用 Write 工具写到本地。
            </div>
          </div>

          {/* 方式 B：下载 ZIP */}
          <div style={{ padding: 12, borderRadius: 6, background: "var(--bg-2)" }}>
            <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 8 }}>
              方式 B · 下载 ZIP 手动解压（无需配 MCP）
            </div>
            <a
              href={LOOP_SKILL_BUNDLE_DOWNLOAD_URL}
              download
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                padding: "8px 14px",
                background: "var(--accent)",
                color: "var(--bg)",
                borderRadius: 4,
                textDecoration: "none",
                fontSize: 13,
                fontWeight: 500,
                marginBottom: 8,
              }}
            >
              <Download size={14} />
              下载 geo-loop-skills-{bundle?.version ?? "..."}.zip
              {bundle && ` (${(totalBundleBytes / 1024).toFixed(1)} KB)`}
            </a>
            <div style={{ fontSize: 12, color: "var(--fg-2)", lineHeight: 1.6 }}>
              {bundle?.install_hint ??
                "解压到 ~/.claude/（全局）或 <repo>/.claude/（项目级）；保留 zip 里的目录结构。"}
            </div>
          </div>

          <div style={{ marginTop: 12, fontSize: 12, color: "var(--fg-2)" }}>
            装好以后：重启 Claude Code → 输入{" "}
            <code style={inlineCode}>/goal 帮我产出 1 篇国风游戏文章作为冒烟</code>
          </div>
        </section>
```

- [ ] **Step 4: typecheck + build**

```bash
pnpm --filter @geo/web typecheck
pnpm --filter @geo/web build
```

**预期**：0 error。

> 如果 typecheck 报 `inlineCode` 未定义：查文件顶部，复用已有的 `inlineCode` 样式常量。如果它是函数内 const 而非 module-level，挪到 module-level（不修改语义）。

- [ ] **Step 5: Commit**

```bash
git add web/src/features/mcp/McpConnectWorkspace.tsx
git commit -m "$(cat <<'EOF'
feat(web/mcp): McpConnectWorkspace Section ⑤ — /goal Loop skill 包分发

新 section 含 3 块：(a) 版本信息卡（version + sha256 + 文件清单展开） +
(b) 方式 A 让 Claude Code 自己装（复制 "帮我装 geo loop skills"，需先过
④ 测试连接） + (c) 方式 B 直接 <a download> 拉 ZIP（不走 fetch+blob，避免
大文件 OOM）。文件清单可展开显示 path/size/sha256[:12] 表格。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: 上游 spec/plan 加 source-of-truth 指引

**Files:**
- Modify: `docs/superpowers/specs/2026-06-24-goal-loop-engineering-design.md`
- Modify: `docs/superpowers/plans/2026-06-24-goal-loop-engineering.md`

- [ ] **Step 1: 给上游 spec §5 头加 banner**

修改 `docs/superpowers/specs/2026-06-24-goal-loop-engineering-design.md`，找到 `## 5. 三个 SKILL.md 骨架` 标题这一行。在它之前插入：

```markdown
> **⚠️ Source-of-truth 已迁移**
>
> 本节的 SKILL.md 内容已经搬到 `server/app/modules/loop_skills/templates/` 入
> git，作为服务端分发正本。本节内容仅作历史快照保留——以服务端 templates/
> 为准。
>
> 分发方式见 [`2026-06-24-loop-skill-distribution-design.md`](./2026-06-24-loop-skill-distribution-design.md)。
>
```

- [ ] **Step 2: 给上游 plan Tasks 5-9 头加 banner**

修改 `docs/superpowers/plans/2026-06-24-goal-loop-engineering.md`，找到 `## Task 5: 填` 标题这一行。在它之前插入：

```markdown
> **⚠️ Tasks 5-9 内容 source-of-truth 已迁移**
>
> 这 5 个 task 创建的 .claude/ 模板内容已经搬到
> `server/app/modules/loop_skills/templates/` 入 git，作为服务端分发正本。
> 本节内容仅作历史快照保留——以服务端 templates/ 为准。
>
> 分发方式见 [`../specs/2026-06-24-loop-skill-distribution-design.md`](../specs/2026-06-24-loop-skill-distribution-design.md)。
>
```

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-06-24-goal-loop-engineering-design.md docs/superpowers/plans/2026-06-24-goal-loop-engineering.md
git commit -m "$(cat <<'EOF'
docs(goal-loop): 标注 SKILL.md 内容已迁移到 templates/ 为分发正本

PR #144 的 spec §5 + plan Tasks 5-9 的 SKILL.md 内容现在在
server/app/modules/loop_skills/templates/ 入 git；旧文档加 banner 提示
那部分内容仅作历史快照，以服务端 templates/ 为准。分发设计稿在
2026-06-24-loop-skill-distribution-design.md。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: 全项目 lint + test 通过 + push + 建 PR

不引入新文件；本任务是集成验证。

- [ ] **Step 1: 后端硬门禁**

```bash
docker compose exec app ruff check server/
docker compose exec app ruff format --check server/
docker compose exec app mypy server/app
```

**预期**：0 error。如果 mypy 在 dev 容器里不可用，跳过 + 报告里说明（CI 上会跑）。

- [ ] **Step 2: 后端全测试**

```bash
docker compose exec -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/ -q
```

**预期**：全 pass。本次新增 9 个测试。

- [ ] **Step 3: 前端 typecheck + build**

```bash
pnpm --filter @geo/web typecheck
pnpm --filter @geo/web build
```

**预期**：0 error。

- [ ] **Step 4: 推分支**

```bash
git push -u origin docs/loop-skill-distribution
```

- [ ] **Step 5: 建 PR**

```bash
gh pr create --title "feat(loop_skills): /goal Loop skill 分发通道 — 模板入 git + Web 下载 + MCP install 工具" --body "$(cat <<'EOF'
## Summary

补齐上游 [PR #144 `/goal` Loop Engineering](https://github.com/geo-ihuanlegame/geo-collab/pull/144) 在 SaaS 模式下的 gap：同事不 clone 仓库，要靠平台分发拿到 5 个 `.claude/` 模板文件。

- 源文件入 git 到 `server/app/modules/loop_skills/templates/`
- Web 端：`McpConnectWorkspace` 加 Section ⑤，user JWT 鉴权下 `.zip`
- MCP 端：`install_loop_skills` 工具一口气返回 5 个文件 dict，让 Claude Code 用 Write 写到本机
- 带 `LOOP_SKILL_BUNDLE_VERSION` + `bundle_sha256`，本地能感知是否过期
- 上游 PR #144 的 spec §5 + plan Tasks 5-9 加 banner 指向新 source-of-truth

## Test plan

- [x] 后端 ruff / format / mypy / pytest 全过（CI 门禁）
- [x] 9 个新 unit 测试通过（5 个 service + 4 个 endpoint 鉴权）
- [x] 前端 typecheck + build 通过
- [ ] 浏览器访问 → MCP 接入 tab → Section ⑤ 显示版本号 + 文件清单（user 手动验证）
- [ ] 点 「下载 ZIP」 → 浏览器拉到 `geo-loop-skills-2026-06-24-v1.zip`（user 手动验证）
- [ ] Claude Code 主对话说 "帮我装 geo loop skills" → install_loop_skills 工具被调 + Write 出 5 个文件（user 手动验证）
- [ ] 装好后跑 `/goal 1 篇国风游戏文章作为冒烟` 走通（user 手动验证）

## 设计 / 实施

- 设计稿：`docs/superpowers/specs/2026-06-24-loop-skill-distribution-design.md`
- 实施 plan：`docs/superpowers/plans/2026-06-24-loop-skill-distribution.md`
- 上游 PR：#144 (已合)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

如果 `gh` 失败（未认证 / 仓库权限），把命令 + 错误记下来交给 user 手动跑。

---

## Self-Review

**1. Spec coverage check** — 每节是否有对应 task？

| Spec 节 | Task 覆盖 |
|---|---|
| §0-§1 决策快照 | 总览部分引用 |
| §2 架构总览 / 5 个关键设计点 | 整体 task 顺序匹配 |
| §3.1 文件清单 | Files to Touch 表 |
| §3.2 源文件搬移 | Task 1 |
| §3.3 给上游 spec/plan 加指引 | Task 10 |
| §3.4 边界 / 不做的事 | 各 task 实施步骤里逐条体现（如 Web 用 <a download> 不用 fetch+blob 在 Task 8 + Task 9 都说了） |
| §4.1 service.py | Tasks 2 + 3 |
| §4.2 version.py | Tasks 2（骨架）+ 4（填 KNOWN sha） |
| §4.3 router.py user endpoints | Task 5 |
| §4.3 router.py mcp_router | Task 6 |
| §4.4 MCP install_loop_skills | Task 7 |
| §4.5 main.py 挂载 | Tasks 5（user router）+ 6（mcp router）|
| §5.1 区块结构 | Task 9 JSX 落地 |
| §5.2 行为细节 | Task 9 各 button/expand 行为 |
| §5.3 API 客户端 | Task 8 |
| §6.1 失败矩阵 | 测试覆盖鉴权层（Tasks 5, 6）；其他失败模式靠 mcp_exception_response + service ValueError 原生路径 |
| §6.2 3 个边界纪律 | Task 7 工具 docstring + Task 9 注释里都强调了 |
| §6.3 自动测 7 个 | Tasks 2 (3) + 3 (1) + 4 (1) + 5 (2) + 6 (2) = 9（spec 写 7，本 plan 加了 2 个正常 200 返回测试是合理补强） |
| §6.4 手工冒烟 6 步 | Task 11 PR description 里 Test plan checklist 留给 user |
| §6.5 有意不测 | 隐含落实（不出现在 task 列表里） |
| §7 工作量估算 + 顺序 | task 顺序匹配 §7.2 |
| §8 与已有 spec / 实现的关系 | 在 Architecture 段引用 |
| §9 Out of Scope | 不需要 task |
| §10 上线门禁 | Task 11 Step 5 PR 描述里 Test plan |

**结论：全覆盖**。

**2. Placeholder scan** — 检查 plan 里有无 TBD / TODO / "implement later" / "similar to Task N"：
- Task 4 Step 3 的 `<上一步打印的 sha 串>` 是**有意的占位符**——它依赖 Step 2 实际跑出来的 sha 值，没法在 plan 里写死。每个 task 的代码块都是完整可 copy-paste 的。✓
- Task 8 注释里提到 "如果 it is function-内 const 而非 module-level，挪到 module-level" 是条件性指令，明确告诉实施者怎么做。✓

**结论：无 placeholder 遗留**。

**3. Type consistency**
- `SkillBundle` / `SkillFile` 的字段定义在 Task 2 service.py 里，被 Task 3 (build_zip) + Task 5 (router) + Task 6 (router) 引用，字段名一致：`path` / `size` / `sha256` / `content` / `version` / `bundle_sha256` / `files`
- `LoopSkillBundleInfo` 在 Task 5 后端 Pydantic 模型 + Task 8 前端 TypeScript 接口字段完全对应（`version`, `bundle_sha256`, `files: [{path, size, sha256}]`, `install_hint`）
- `LOOP_SKILL_BUNDLE_VERSION` 在 Task 2 定义、Task 4 引用、Task 5 / 6 端点输出，名称一致
- `KNOWN_BUNDLE_SHAS` 在 Task 2 (空集) + Task 4 (填值) + Task 4 测试 (校验) 名称一致
- MCP 工具 `install_loop_skills` 在 Task 7 定义、PR description 引用，名称一致
- 后端路径 `/api/mcp/loop-skill-bundle/info` + `/download.zip` + `/install-payload` 三处一致

**结论：一致**。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-24-loop-skill-distribution.md`. Two execution options:

**1. Subagent-Driven (recommended)** — 我每个 task 起一个 fresh subagent 跑，task 之间我 review，迭代快。

**2. Inline Execution** — 我在当前会话里逐 task 顺序执行，每 2-3 个 task 检查一次。

Which approach?
