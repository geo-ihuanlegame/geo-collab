from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.app.core.security import get_current_user, require_admin
from server.app.db.session import get_db
from server.app.models import ArticleGroup, User
from server.app.shared.errors import ClientError
from server.app.schemas.article_group import (
    ArticleGroupCreate,
    ArticleGroupItemsUpdate,
    ArticleGroupRead,
    ArticleGroupUpdate,
)
from server.app.modules.articles import (
    create_group,
    delete_group,
    get_group,
    list_groups,
    replace_group_items,
    update_group,
)
from server.app.api.serializers import to_group_read

router = APIRouter()


def _verify_group_ownership(group: ArticleGroup | None, current_user: User) -> ArticleGroup:
    if group is None:
        raise HTTPException(status_code=404, detail="文章分组不存在")
    if current_user.role != "admin" and group.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="文章分组不存在")
    return group


# 获取所有文章分组列表
@router.get("", response_model=list[ArticleGroupRead])
def read_groups(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ArticleGroupRead]:
    groups = list_groups(db)
    if current_user.role != "admin":
        groups = [g for g in groups if g.user_id == current_user.id]
    return [to_group_read(group) for group in groups]


# 创建新分组
@router.post("", response_model=ArticleGroupRead)
def create_group_endpoint(
    payload: ArticleGroupCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ArticleGroupRead:
    try:
        group = create_group(db, current_user.id, payload)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="分组名称已存在") from exc
    return to_group_read(group)


# 获取分组详情
@router.get("/{group_id}", response_model=ArticleGroupRead)
def read_group(
    group_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ArticleGroupRead:
    group = _verify_group_ownership(get_group(db, group_id), current_user)
    return to_group_read(group)


# 更新分组信息（名称、描述）
@router.put("/{group_id}", response_model=ArticleGroupRead)
def update_group_endpoint(
    group_id: int,
    payload: ArticleGroupUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ArticleGroupRead:
    group = _verify_group_ownership(get_group(db, group_id), current_user)
    try:
        updated = update_group(db, group, payload)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="分组名称已存在") from exc
    return to_group_read(updated)


# 删除分组
@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_group_endpoint(
    group_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> Response:
    group = _verify_group_ownership(get_group(db, group_id), current_user)
    try:
        delete_group(db, group)
    except ClientError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 更新分组中的文章列表（全量替换）
@router.put("/{group_id}/items", response_model=ArticleGroupRead)
def update_group_items(
    group_id: int,
    payload: ArticleGroupItemsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ArticleGroupRead:
    group = _verify_group_ownership(get_group(db, group_id), current_user)
    return to_group_read(replace_group_items(db, group, payload))

