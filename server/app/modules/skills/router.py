"""技能模块路由。"""
import io
import re
import zipfile
from pathlib import Path
from typing import Any

import frontmatter
import yaml
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from server.app.core.config import MAX_ZIP_BYTES, get_settings
from server.app.core.paths import get_data_dir
from server.app.core.security import get_current_user
from server.app.db.session import get_db
from server.app.modules.system.models import User
from server.app.modules.skills.service import (
    create_skill,
    delete_skill,
    get_skill,
    list_skills,
    patch_skill,
)
from server.app.modules.skills.schemas import SkillPatch, SkillRead
from server.app.shared.errors import ClientError

router = APIRouter()


_FRONTMATTER_RE = re.compile(r"\A---\s*\r?\n(.*?\r?\n)---\s*(?:\r?\n|\Z)", re.DOTALL)
_FM_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$")


def _loose_parse_frontmatter(content: str) -> dict[str, str]:
    """YAML 严格解析失败时的兜底：逐行抓 top-level `key: value`，
    缩进或非 key 行视为上一个值的续行。仅返回字符串值，够用于 name/description。"""
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return {}
    result: dict[str, str] = {}
    current_key: str | None = None
    current_lines: list[str] = []
    for line in m.group(1).splitlines():
        is_indented = line.startswith((" ", "\t"))
        key_match = None if is_indented else _FM_KEY_RE.match(line)
        if key_match:
            if current_key is not None:
                result[current_key] = "\n".join(current_lines).strip()
            current_key = key_match.group(1)
            current_lines = [key_match.group(2)]
        elif current_key is not None:
            current_lines.append(line.strip())
    if current_key is not None:
        result[current_key] = "\n".join(current_lines).strip()
    for k, v in list(result.items()):
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            result[k] = v[1:-1]
    return result


def _parse_zip(data: bytes) -> tuple[str, str | None, dict[str, int], zipfile.ZipFile, str]:
    """解析 ZIP 内容，返回 (name, description, file_stats, zf, prefix)。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ClientError("上传文件不是有效的 ZIP 格式")

    # 找到 SKILL.md，确定 ZIP 内顶层前缀
    skill_md_path: str | None = None
    for entry in zf.namelist():
        if entry.split("/")[-1] == "SKILL.md":
            skill_md_path = entry
            break
    if skill_md_path is None:
        raise ClientError("ZIP 中未找到 SKILL.md，请确认技能文件夹结构")

    prefix = skill_md_path[: -len("SKILL.md")]  # e.g. "geo-article-v2/" or ""

    skill_md_content = zf.read(skill_md_path).decode("utf-8")
    try:
        metadata: dict[str, Any] = frontmatter.loads(skill_md_content).metadata
    except yaml.YAMLError:
        metadata = _loose_parse_frontmatter(skill_md_content)
    name: str = metadata.get("name") or Path(prefix.rstrip("/")).name or "未命名技能"
    description: str | None = metadata.get("description")

    relative_paths = [
        entry[len(prefix):]
        for entry in zf.namelist()
        if entry.startswith(prefix) and entry[len(prefix):]
    ]

    def _count(subdir: str) -> int:
        return sum(
            1 for p in relative_paths if p.startswith(f"{subdir}/") and not p.endswith("/")
        )

    file_stats = {
        "references": _count("references"),
        "skeletons": _count("skeletons"),
        "assets": _count("assets"),
    }
    return name, description, file_stats, zf, prefix


def _extract_zip(zf: zipfile.ZipFile, prefix: str, dest_dir: Path) -> None:
    """将 ZIP 中 prefix 下的文件解压到 dest_dir，剥去 prefix 前缀。"""
    for entry in zf.namelist():
        if not entry.startswith(prefix):
            continue
        relative = entry[len(prefix):]
        if not relative:
            continue
        dest = dest_dir / relative
        if entry.endswith("/"):
            dest.mkdir(parents=True, exist_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(entry))


@router.get("", response_model=list[SkillRead])
def read_skills(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Any]:
    return list_skills(db)


@router.post("", response_model=SkillRead, status_code=201)
def upload_skill(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    data = file.file.read()
    if len(data) > MAX_ZIP_BYTES:
        raise ClientError(f"ZIP 文件超过 {MAX_ZIP_BYTES // 1024 // 1024} MB 上限")

    name, description, file_stats, zf, prefix = _parse_zip(data)

    skill = create_skill(
        db,
        name=name,
        description=description,
        storage_path="",
        file_stats=file_stats,
    )

    skills_dir = get_data_dir() / "skills" / str(skill.id)
    skills_dir.mkdir(parents=True, exist_ok=True)
    _extract_zip(zf, prefix, skills_dir)

    skill.storage_path = str(skills_dir)
    db.flush()

    return SkillRead.model_validate(skill)


@router.patch("/{skill_id}", response_model=SkillRead)
def update_skill(
    skill_id: int,
    payload: SkillPatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    skill = get_skill(db, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    return patch_skill(db, skill, is_enabled=payload.is_enabled)


@router.delete("/{skill_id}", status_code=204)
def remove_skill(
    skill_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    skill = get_skill(db, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    delete_skill(db, skill)
