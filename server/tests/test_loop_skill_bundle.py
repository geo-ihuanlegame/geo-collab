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
    readme.write_text(
        readme.read_text(encoding="utf-8") + "\n\n<!-- test marker -->\n",
        encoding="utf-8",
    )

    after = service.build_bundle().bundle_sha256
    assert before != after, "bundle_sha256 should change when a template changes"


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


@pytest.mark.mysql
def test_user_info_endpoint_requires_jwt(monkeypatch):
    """/api/mcp/loop-skill-bundle/info 不带 cookie → 401."""
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        # 清掉 client 上自带的 admin JWT cookie，验证未鉴权请求被拒
        test_app.client.cookies.clear()
        r = test_app.client.get("/api/mcp/loop-skill-bundle/info")
        assert r.status_code == 401
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_user_info_endpoint_returns_bundle_when_authed(monkeypatch):
    """带 JWT 请求 → 200，返回 {version, bundle_sha256, files, install_hint}."""
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


@pytest.mark.mysql
def test_mcp_install_payload_endpoint_requires_mcp_token(monkeypatch):
    """/api/mcp/loop-skill-bundle/install-payload 不带 X-MCP-Token → 401."""
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
    """带 MCP token → 200，返回 {ok, data:{files[{path,content,sha256,size}], ...}, error=None}."""
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


def test_orchestrator_skill_progress_log_no_english_jargon():
    """orchestrator skill 用户可见的进度日志 + 伪码 echo / notify_feishu 字符串
    不应再含英文 / 术语黑名单（pool= / qid= / matrix=< / goal 评审 / netto /
    attempts ceiling / candidates 用尽 / token 预算 / MCP 连续 / [interrupted] /
    生文 Loop / article_id= / decision= / score=）。

    严格 grep 模板全文，宁错杀也要催 i18n 改完。
    """
    from pathlib import Path

    skill = (
        Path(__file__).resolve().parent.parent
        / "app/modules/loop_skills/templates/skills/geo-goal-orchestrator/SKILL.md"
    )
    text = skill.read_text(encoding="utf-8")

    # 这些 token 在面向用户的位置出现就算回退；伪码变量名（netto / qid / pool_id
    # 等）出现在 Python 代码缩进里不在此列（黑名单针对的是字符串字面量 + 注释 /
    # 表格 / 文档行）。判定方式：白名单允许 `target.pool_id` / `netto.count` /
    # `qid = pick_next_qid` 这种代码读取；其它出现都视为日志/叙述用词漂移。
    blacklist = [
        "[快检]",  # 旧 tag，应该换成 [启动检查]
        "pool=",  # echo 字段名应该是「问题池：」
        "matrix=<code|默认>",  # echo 字段名应该是「矩阵：」
        "matrix=<code|default>",  # 旧版残留
        "goal 评审",  # 应该是「评审员」
        "[净产出]",  # 应该是「累计通过」
        "attempts ceiling",  # 飞书消息应该是「已达尝试轮数上限」
        "token 预算",  # 飞书消息应该是「主对话内存预算」
        "MCP 连续失败",  # 飞书消息应该是「接口连续失败」
        "[interrupted]",  # 应该是「[已中断]」
        "生文 Loop ",  # 应该是「生文流程」
        'description=f"写一篇文章 qid=',  # subagent description 应换成「改写文章（问题 #...）」
        'description=f"评分 article_id=',  # subagent description 应换成「评审文章 #...」
    ]
    hits = [token for token in blacklist if token in text]
    assert not hits, (
        f"orchestrator SKILL.md 仍有 i18n 漂移 token：{hits}. "
        "改完 echo / notify_feishu / subagent description 后再跑."
    )


def test_orchestrator_skill_has_narration_rules_section():
    """orchestrator skill 必须含「主对话叙述规范（强制）」段 + 关键黑名单关键词,
    才能让 Claude 在自由叙述时不漏术语."""
    from pathlib import Path

    skill = (
        Path(__file__).resolve().parent.parent
        / "app/modules/loop_skills/templates/skills/geo-goal-orchestrator/SKILL.md"
    )
    text = skill.read_text(encoding="utf-8")

    assert "# 主对话叙述规范（强制）" in text, (
        "orchestrator skill 缺「主对话叙述规范（强制）」段；该段用于约束 Claude "
        "在主对话自由叙述时不漏 orchestrator/netto/qid 等英文术语."
    )
    # 段内必须含黑名单关键词 + 反例 / 正例标签
    for keyword in ["❌ 不要说", "✅ 改成", "**反例**", "**正例**"]:
        assert keyword in text, f"叙述规范段缺关键标签：{keyword}"
    # 段内必须列出关键术语黑名单
    for term in ["orchestrator", "netto", "qid", "pool", "matrix", "goal-verifier"]:
        assert term in text, f"叙述规范段未列入黑名单术语：{term}"
