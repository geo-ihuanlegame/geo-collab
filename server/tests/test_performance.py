import json

from server.app.modules.performance.service import record_publish_metrics
from server.tests.utils import build_test_app


def test_record_publish_metrics_merges_into_article(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.accounts.models import Account
        from server.app.modules.articles.models import Article
        from server.app.modules.system.models import Platform
        from server.app.modules.tasks.models import PublishRecord, PublishTask

        db = test_app.session_factory()
        try:
            platform = Platform(
                code="toutiao_perf_test",
                name="Toutiao Perf Test",
                base_url="https://mp.toutiao.com",
            )
            db.add(platform)
            db.flush()

            account = Account(
                user_id=test_app.admin_id,
                platform=platform,
                display_name="perf test account",
                platform_user_id="perf-test-user",
                status="valid",
                state_path="browser_states/toutiao/perf/storage_state.json",
            )
            db.add(account)
            db.flush()

            a = Article(
                user_id=test_app.admin_id,
                title="t",
                content_json=json.dumps({"type": "doc", "content": []}),
                content_html="",
                plain_text="",
                word_count=0,
                status="ready",
                review_status="approved",
                metrics={"views": 100},  # 已有的会被合并
            )
            db.add(a)
            db.flush()

            task = PublishTask(
                user_id=test_app.admin_id,
                name="perf test task",
                task_type="single",
                status="succeeded",
                platform=platform,
                article=a,
            )
            db.add(task)
            db.flush()

            r = PublishRecord(
                task=task,
                article=a,
                platform=platform,
                account=account,
                status="succeeded",
            )
            db.add(r)
            db.commit()
            aid, rid = a.id, r.id
        finally:
            db.close()

        db = test_app.session_factory()
        try:
            record_publish_metrics(db, rid, {"likes": 50, "comments": 5})
            db.commit()
            a = db.query(Article).filter(Article.id == aid).first()
            assert a.metrics["views"] == 100  # 保留
            assert a.metrics["likes"] == 50  # 新增
            assert a.metrics["comments"] == 5
        finally:
            db.close()
    finally:
        test_app.cleanup()
