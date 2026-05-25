"""ORM → Pydantic 序列化转换函数。"""
from server.app.modules.articles import loads_content_json
from server.app.modules.accounts import get_session_for_record
from server.app.models import (
    Account,
    Article,
    ArticleGroup,
    PublishRecord,
    PublishTask,
    TaskLog,
)
from server.app.schemas.account import AccountRead
from server.app.schemas.article import ArticleBodyAssetRead, ArticleRead
from server.app.schemas.article_group import ArticleGroupItemRead, ArticleGroupRead
from server.app.schemas.task import (
    PublishRecordRead,
    TaskAccountRead,
    TaskLogRead,
    TaskRead,
)


# 将 ORM Article 转为响应体
def to_article_read(article: Article, published_count: int = 0) -> ArticleRead:
    body_assets = sorted(article.body_assets, key=lambda item: item.position)
    return ArticleRead(
        id=article.id,
        title=article.title,
        author=article.author,
        cover_asset_id=article.cover_asset_id,
        content_json=loads_content_json(article.content_json),
        content_html=article.content_html,
        plain_text=article.plain_text,
        word_count=article.word_count,
        status=article.status,
        version=article.version,
        published_count=published_count,
        body_assets=[
            ArticleBodyAssetRead(
                asset_id=item.asset_id,
                position=item.position,
                editor_node_id=item.editor_node_id,
            )
            for item in body_assets
        ],
        stock_category_id=article.stock_category_id,
        created_at=article.created_at,
        updated_at=article.updated_at,
        ai_checking=article.ai_checking,
        ai_format_error=article.ai_format_error,
    )


# 将 ORM PublishTask 转为响应体
def to_task_read(task: PublishTask) -> TaskRead:
    accounts = sorted(task.accounts, key=lambda item: item.sort_order)
    return TaskRead(
        id=task.id,
        name=task.name,
        task_type=task.task_type,
        status=task.status,
        platform_id=task.platform_id,
        platform_code=task.platform.code,
        article_id=task.article_id,
        group_id=task.group_id,
        stop_before_publish=task.stop_before_publish,
        cancel_requested=bool(task.cancel_requested),
        accounts=[
            TaskAccountRead(
                account_id=item.account_id,
                sort_order=item.sort_order,
                display_name=item.account.display_name,
                status=item.account.status,
            )
            for item in accounts
        ],
        record_count=len(task.records),
        worker_id=task.worker_id,
        worker_heartbeat_at=task.worker_heartbeat_at,
        created_at=task.created_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
    )


# 将 ORM PublishRecord 转为响应体
def to_record_read(record: PublishRecord) -> PublishRecordRead:
    session = get_session_for_record(record.id)
    return PublishRecordRead(
        id=record.id,
        task_id=record.task_id,
        article_id=record.article_id,
        platform_id=record.platform_id,
        account_id=record.account_id,
        status=record.status,
        publish_url=record.publish_url,
        error_message=record.error_message,
        retry_of_record_id=record.retry_of_record_id,
        started_at=record.started_at,
        finished_at=record.finished_at,
        lease_until=record.lease_until,
        remote_browser_session_id=session.id if session else None,
        novnc_url=session.novnc_url if session else None,
    )


# 将 ORM TaskLog 转为响应体
def to_log_read(log: TaskLog) -> TaskLogRead:
    return TaskLogRead(
        id=log.id,
        task_id=log.task_id,
        record_id=log.record_id,
        level=log.level,
        message=log.message,
        screenshot_asset_id=log.screenshot_asset_id,
        created_at=log.created_at,
    )


# 将 ORM Account 转为响应体
def to_account_read(account: Account) -> AccountRead:
    return AccountRead(
        id=account.id,
        platform_code=account.platform.code,
        platform_name=account.platform.name,
        display_name=account.display_name,
        platform_user_id=account.platform_user_id,
        status=account.status,
        last_checked_at=account.last_checked_at,
        last_login_at=account.last_login_at,
        state_path=account.state_path,
        note=account.note,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


# 将 ORM ArticleGroup 转为响应体
def to_group_read(group: ArticleGroup) -> ArticleGroupRead:
    items = sorted(group.items, key=lambda item: item.sort_order)
    return ArticleGroupRead(
        id=group.id,
        name=group.name,
        description=group.description,
        version=group.version,
        items=[
            ArticleGroupItemRead(
                article_id=item.article_id,
                sort_order=item.sort_order,
            )
            for item in items
        ],
        created_at=group.created_at,
        updated_at=group.updated_at,
    )
