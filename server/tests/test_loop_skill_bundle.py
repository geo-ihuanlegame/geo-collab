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
