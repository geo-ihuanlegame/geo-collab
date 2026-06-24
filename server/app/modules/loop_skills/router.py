"""loop_skills HTTP 路由.

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
