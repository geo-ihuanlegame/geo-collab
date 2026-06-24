"""任务收口竞态遗留的孤儿 pending 记录 —— 兜底恢复回归。

观测到的生产 bug（task 94 / record 383）：多账号轮询任务收尾的瞬间，对其中一条 failed
记录发起重试，retry_record 会新建一条 pending 记录；但正在跑的 worker 在 `_run_pending_records`
末轮拿的是「插入之前」的记录快照，收口判定（无 pending）成立 → 把任务落到终态。那条刚插入的
pending 记录于是被孤儿化：worker 认领只认 status∈(pending,running) 的任务，终态任务里的 pending
记录永远没人捡。

修复：`reopen_orphaned_terminal_tasks` 在启动 / 周期恢复时兜底——把「已终态、却仍挂着未软删
pending 记录」的任务拨回 running，让 worker 重新认领、发完那条 pending。健康的在途任务（running）
与已正常收口（无 pending）的终态任务都不受影响。
"""

import pytest

from server.tests.utils import build_test_app

pytestmark = pytest.mark.mysql


def _seed_task_with_records(db, *, task_status, record_specs, username="op_orphan"):
    """造一个任务 + 一批记录；record_specs 是 [(status, is_retry), ...]。返回 (task, records)。"""
    from server.app.modules.accounts.models import Account
    from server.app.modules.articles.models import Article
    from server.app.modules.system.models import Platform, User
    from server.app.modules.tasks.models import PublishRecord, PublishTask

    user = User(username=username, role="operator", is_active=True, must_change_password=False)
    user.set_password("pw-123456")
    db.add(user)
    db.flush()
    platform = Platform(
        code="toutiao", name="头条号", base_url="https://mp.toutiao.com", enabled=True
    )
    db.add(platform)
    db.flush()
    account = Account(
        user_id=user.id,
        platform_id=platform.id,
        display_name="acc",
        platform_user_id=None,
        status="valid",
        state_path="browser_states/toutiao/acc/storage_state.json",
    )
    db.add(account)
    db.flush()
    article = Article(user_id=user.id, title="t", status="ready")
    db.add(article)
    db.flush()
    task = PublishTask(
        user_id=user.id,
        name="task",
        task_type="group_round_robin",
        platform_id=platform.id,
        article_id=article.id,
        status=task_status,
    )
    db.add(task)
    db.flush()
    records = []
    for status, is_retry in record_specs:
        rec = PublishRecord(
            task_id=task.id,
            article_id=article.id,
            platform_id=platform.id,
            account_id=account.id,
            status=status,
            retry_of_record_id=(records[0].id if is_retry and records else None),
        )
        db.add(rec)
        db.flush()
        records.append(rec)
    return task, records


def test_reopen_orphaned_terminal_task_with_pending_record(monkeypatch):
    """终态任务里挂着孤儿 pending 记录 → 兜底拨回 running，让 worker 能重新认领。"""
    from server.app.modules.tasks.service import reopen_orphaned_terminal_tasks

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            # partial_failed 任务：一条 failed（原始）+ 一条 pending（孤儿重试记录）
            task, _records = _seed_task_with_records(
                db,
                task_status="partial_failed",
                record_specs=[("failed", False), ("pending", True)],
            )
            from server.app.core.time import utcnow

            task.finished_at = utcnow()
            db.commit()

            reopened = reopen_orphaned_terminal_tasks(db)

            assert reopened == 1
            db.refresh(task)
            assert task.status == "running"  # 拨回可认领状态
            assert task.finished_at is None
    finally:
        test_app.cleanup()


def test_reopen_ignores_terminal_task_without_pending(monkeypatch):
    """已正常收口的终态任务（无 pending 记录）不应被打扰。"""
    from server.app.core.time import utcnow
    from server.app.modules.tasks.service import reopen_orphaned_terminal_tasks

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            task, _records = _seed_task_with_records(
                db,
                task_status="succeeded",
                record_specs=[("succeeded", False), ("succeeded", False)],
            )
            finished = utcnow()
            task.finished_at = finished
            db.commit()

            reopened = reopen_orphaned_terminal_tasks(db)

            assert reopened == 0
            db.refresh(task)
            assert task.status == "succeeded"
            assert task.finished_at is not None
    finally:
        test_app.cleanup()


def test_reopen_ignores_running_task_with_pending(monkeypatch):
    """健康的在途任务（running，含 pending）不是终态，不应被恢复函数碰。"""
    from server.app.modules.tasks.service import reopen_orphaned_terminal_tasks

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            task, _records = _seed_task_with_records(
                db,
                task_status="running",
                record_specs=[("succeeded", False), ("pending", False)],
            )
            db.commit()

            reopened = reopen_orphaned_terminal_tasks(db)

            assert reopened == 0
            db.refresh(task)
            assert task.status == "running"
    finally:
        test_app.cleanup()
