import json
from pathlib import Path

import pytest

from server.app.modules.articles.parser import BodySegment
from server.app.modules.tasks.drivers.base import (
    PublishError,
    PublishPayload,
    UserInputRequired,
)
from server.app.modules.tasks.drivers.toutiao_inpage import (
    ToutiaoInPageDriver,
    _is_logged_out,
    build_publish_form,
)


def test_build_publish_form_minimal_draft():
    form = build_publish_form(title="今天是周二", content_html='<p data-track="1">正文</p>')
    assert form["title"] == "今天是周二"
    assert form["content"] == '<p data-track="1">正文</p>'
    assert form["save"] == "0"
    assert form["source"] == "29"
    assert form["pgc_feed_covers"] == "[]"
    # extra is valid JSON carrying the word count
    extra = json.loads(form["extra"])
    assert extra["content_word_cnt"] == len("正文")
    assert "pgc_id" not in form  # omitted on first draft


def test_build_publish_form_reuses_pgc_id():
    form = build_publish_form(title="t", content_html="<p>x</p>", pgc_id="7646670891934089737")
    assert form["pgc_id"] == "7646670891934089737"


class _FakePage:
    """Minimal stand-in for a Playwright Page used in driver.publish().

    # NOTE: url is fixed at construction; does not simulate a post-goto redirect.
    """

    def __init__(self, *, url, evaluate_result):
        self._url = url
        self._evaluate_result = evaluate_result
        self.goto_calls = []

    @property
    def url(self):
        return self._url

    def goto(self, url, **kwargs):
        self.goto_calls.append(url)

    def title(self):
        return "头条号"

    def wait_for_timeout(self, *_a, **_k):
        pass

    def evaluate(self, _js, _arg=None):
        if isinstance(self._evaluate_result, Exception):
            raise self._evaluate_result
        return self._evaluate_result

    def get_by_role(self, role, name=None):
        # Default page: the editor title box is present unless we are on a
        # login wall. Subclasses override this to simulate slow-render / walls.
        page = self

        class _Loc:
            def count(self_inner):
                return 0 if _is_logged_out(page._url) else 1

        return _Loc()


def _payload():
    return PublishPayload(
        title="今天是周二",
        cover_asset_path=Path("cover.png"),
        body_segments=[BodySegment(kind="text", text="正文")],
        account_key="acc",
        state_path=Path("state.json"),
        display_name="账号",
        platform_code="toutiao",
    )


def test_publish_success_maps_to_result():
    page = _FakePage(
        url="https://mp.toutiao.com/profile_v4/graphic/publish",
        evaluate_result={
            "httpStatus": 200,
            "data": {"code": 0, "data": {"pgc_id": "999"}},
            "raw": "{}",
        },
    )
    driver = ToutiaoInPageDriver()
    result = driver.publish(page=page, context=None, payload=_payload(), stop_before_publish=True)
    assert result.title == "今天是周二"
    assert "999" in (result.url or "") or "999" in result.message
    assert page.goto_calls == ["https://mp.toutiao.com/profile_v4/graphic/publish"]


def test_publish_login_redirect_raises_user_input_required():
    page = _FakePage(url="https://mp.toutiao.com/auth/page/login?x=1", evaluate_result=None)
    driver = ToutiaoInPageDriver()
    with pytest.raises(UserInputRequired):
        driver.publish(page=page, context=None, payload=_payload(), stop_before_publish=True)


def test_publish_api_error_raises_publish_error():
    page = _FakePage(
        url="https://mp.toutiao.com/profile_v4/graphic/publish",
        evaluate_result={
            "httpStatus": 200,
            "data": {"code": 1, "message": "verify required"},
            "raw": "{...}",
        },
    )
    driver = ToutiaoInPageDriver()
    with pytest.raises(PublishError):
        driver.publish(page=page, context=None, payload=_payload(), stop_before_publish=True)


def test_publish_waits_for_editor_then_proceeds():
    """Editor title box appears after a couple polls -> no false UserInputRequired."""

    class _SlowReadyPage(_FakePage):
        def __init__(self):
            super().__init__(
                url="https://mp.toutiao.com/profile_v4/graphic/publish",
                evaluate_result={
                    "httpStatus": 200,
                    "data": {"code": 0, "data": {"pgc_id": "7"}},
                    "raw": "{}",
                },
            )
            self._title_polls = 0

        def get_by_role(self, role, name=None):
            page = self

            class _Loc:
                def count(self_inner):
                    page._title_polls += 1
                    return 1 if page._title_polls >= 3 else 0

            return _Loc()

    page = _SlowReadyPage()
    result = ToutiaoInPageDriver().publish(
        page=page, context=None, payload=_payload(), stop_before_publish=True
    )
    # The driver must actually poll for the editor (not proceed blindly),
    # then proceed once it is ready.
    assert page._title_polls >= 3
    assert "7" in result.message


def test_publish_persistent_login_wall_raises():
    class _LoginPage(_FakePage):
        def __init__(self):
            super().__init__(url="https://mp.toutiao.com/auth/page/login?x=1", evaluate_result=None)

        def get_by_role(self, role, name=None):
            class _Loc:
                def count(self_inner):
                    return 0

            return _Loc()

    page = _LoginPage()
    with pytest.raises(UserInputRequired):
        ToutiaoInPageDriver().publish(
            page=page, context=None, payload=_payload(), stop_before_publish=True
        )
