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
    PUBLISH_API_URL,
    UPLOAD_URL,
    ToutiaoInPageDriver,
    _is_logged_out,
    _map_publish_response,
    build_publish_form,
)


def _write_png(path: Path) -> Path:
    """Write a tiny real PNG so the driver can base64-read it from disk."""
    from PIL import Image

    Image.new("RGB", (8, 8), (123, 200, 50)).save(path)
    return path


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


def test_build_publish_form_save_flag_real_publish():
    """save=1 -> real publish (entrance=main); draft_form_data + default cover intact."""
    form = build_publish_form(title="t", content_html="<p>x</p>", save=1)
    assert form["save"] == "1"
    assert form["entrance"] == "main"
    # draft form data still carries the cover type even on real publish
    assert json.loads(form["draft_form_data"]) == {"coverType": 2}
    assert form["pgc_feed_covers"] == "[]"


class _FakePage:
    """Minimal stand-in for a Playwright Page used in driver.publish().

    # NOTE: url is fixed at construction; does not simulate a post-goto redirect.
    """

    def __init__(self, *, url, evaluate_result):
        self._url = url
        self._evaluate_result = evaluate_result
        self.goto_calls = []
        self.evaluate_arg = None

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
        self.evaluate_arg = _arg
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


def _payload(tmp_path: Path, *, with_cover: bool = True, body_image: bool = False):
    cover = _write_png(tmp_path / "cover.png") if with_cover else None
    body_segments: list[BodySegment] = [BodySegment(kind="text", text="正文")]
    if body_image:
        img = _write_png(tmp_path / "body0.png")
        body_segments.append(BodySegment(kind="image", image_path=img))
    return PublishPayload(
        title="今天是周二",
        cover_asset_path=cover,
        body_segments=body_segments,
        account_key="acc",
        state_path=Path("state.json"),
        display_name="账号",
        platform_code="toutiao",
    )


def _publish_envelope(publish_dict, uploads=None):
    """Wrap an inner publish dict in the M2 full-response envelope."""
    return {
        "ok": True,
        "step": "publish",
        "uploads": uploads or [],
        "publish": publish_dict,
    }


def test_publish_success_maps_to_result(tmp_path):
    page = _FakePage(
        url="https://mp.toutiao.com/profile_v4/graphic/publish",
        evaluate_result=_publish_envelope(
            {
                "httpStatus": 200,
                "data": {"code": 0, "data": {"pgc_id": "999"}},
                "raw": "{}",
            }
        ),
    )
    driver = ToutiaoInPageDriver()
    result = driver.publish(
        page=page, context=None, payload=_payload(tmp_path), stop_before_publish=True
    )
    assert result.title == "今天是周二"
    assert "999" in (result.url or "") or "999" in result.message
    assert page.goto_calls == ["https://mp.toutiao.com/profile_v4/graphic/publish"]


def test_publish_login_redirect_raises_user_input_required(tmp_path):
    page = _FakePage(url="https://mp.toutiao.com/auth/page/login?x=1", evaluate_result=None)
    driver = ToutiaoInPageDriver()
    with pytest.raises(UserInputRequired):
        driver.publish(
            page=page, context=None, payload=_payload(tmp_path), stop_before_publish=True
        )


def test_publish_api_error_raises_publish_error(tmp_path):
    page = _FakePage(
        url="https://mp.toutiao.com/profile_v4/graphic/publish",
        evaluate_result=_publish_envelope(
            {
                "httpStatus": 200,
                "data": {"code": 1, "message": "verify required"},
                "raw": "{...}",
            }
        ),
    )
    driver = ToutiaoInPageDriver()
    with pytest.raises(PublishError):
        driver.publish(
            page=page, context=None, payload=_payload(tmp_path), stop_before_publish=True
        )


def test_publish_waits_for_editor_then_proceeds(tmp_path):
    """Editor title box appears after a couple polls -> no false UserInputRequired."""

    class _SlowReadyPage(_FakePage):
        def __init__(self):
            super().__init__(
                url="https://mp.toutiao.com/profile_v4/graphic/publish",
                evaluate_result=_publish_envelope(
                    {
                        "httpStatus": 200,
                        "data": {"code": 0, "data": {"pgc_id": "7"}},
                        "raw": "{}",
                    }
                ),
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
        page=page, context=None, payload=_payload(tmp_path), stop_before_publish=True
    )
    # The driver must actually poll for the editor (not proceed blindly),
    # then proceed once it is ready.
    assert page._title_polls >= 3
    assert "7" in result.message


def test_publish_persistent_login_wall_raises(tmp_path):
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
            page=page, context=None, payload=_payload(tmp_path), stop_before_publish=True
        )


def test_publish_uploads_cover_and_body_then_maps_url(tmp_path):
    """Full M2 path: cover + one body image upload then publish -> article URL."""
    page = _FakePage(
        url="https://mp.toutiao.com/profile_v4/graphic/publish",
        evaluate_result=_publish_envelope(
            {
                "httpStatus": 200,
                "data": {
                    "code": 0,
                    "data": {
                        "pgc_id": "123",
                        "article_url": "https://www.toutiao.com/article/123/",
                    },
                },
                "raw": "{}",
            },
            uploads=[{"uri": "tos-cn-i-abc/cover"}, {"uri": "tos-cn-i-abc/body0"}],
        ),
    )
    result = ToutiaoInPageDriver().publish(
        page=page,
        context=None,
        payload=_payload(tmp_path, body_image=True),
        stop_before_publish=False,
    )
    assert "123" in (result.url or "") or "123" in result.message
    assert result.url == "https://www.toutiao.com/article/123/"


def test_publish_upload_failure_raises_publish_error(tmp_path):
    page = _FakePage(
        url="https://mp.toutiao.com/profile_v4/graphic/publish",
        evaluate_result={
            "ok": False,
            "step": "upload",
            "index": 0,
            "httpStatus": 500,
            "raw": "oops",
        },
    )
    with pytest.raises(PublishError) as exc:
        ToutiaoInPageDriver().publish(
            page=page,
            context=None,
            payload=_payload(tmp_path, body_image=True),
            stop_before_publish=False,
        )
    msg = str(exc.value)
    assert "上传" in msg or "upload" in msg.lower()
    assert "oops" in msg


def test_publish_evaluate_arg_shape(tmp_path):
    page = _FakePage(
        url="https://mp.toutiao.com/profile_v4/graphic/publish",
        evaluate_result=_publish_envelope(
            {"httpStatus": 200, "data": {"code": 0, "data": {"pgc_id": "1"}}, "raw": "{}"}
        ),
    )
    ToutiaoInPageDriver().publish(
        page=page,
        context=None,
        payload=_payload(tmp_path, body_image=True),
        stop_before_publish=False,
    )
    arg = page.evaluate_arg
    assert arg["uploadUrl"] == UPLOAD_URL
    assert arg["publishUrl"] == PUBLISH_API_URL
    assert arg["cover"]["b64"]  # non-empty base64
    assert arg["cover"]["mime"] == "image/png"
    assert arg["bodyImages"][0]["token"] == "__GEO_IMG_0__"
    assert arg["bodyImages"][0]["b64"]
    assert "__GEO_IMG_0__" in arg["form"]["content"]


def test_publish_save_flag_reflects_stop_before_publish(tmp_path):
    # stop_before_publish=True -> draft (save=0)
    page = _FakePage(
        url="https://mp.toutiao.com/profile_v4/graphic/publish",
        evaluate_result=_publish_envelope(
            {"httpStatus": 200, "data": {"code": 0, "data": {"pgc_id": "1"}}, "raw": "{}"}
        ),
    )
    ToutiaoInPageDriver().publish(
        page=page, context=None, payload=_payload(tmp_path), stop_before_publish=True
    )
    assert page.evaluate_arg["form"]["save"] == "0"

    # stop_before_publish=False -> real publish (save=1)
    page2 = _FakePage(
        url="https://mp.toutiao.com/profile_v4/graphic/publish",
        evaluate_result=_publish_envelope(
            {"httpStatus": 200, "data": {"code": 0, "data": {"pgc_id": "1"}}, "raw": "{}"}
        ),
    )
    ToutiaoInPageDriver().publish(
        page=page2, context=None, payload=_payload(tmp_path), stop_before_publish=False
    )
    assert page2.evaluate_arg["form"]["save"] == "1"


def test_publish_real_publish_requires_cover(tmp_path):
    """save=1 without a cover must raise before evaluate."""
    page = _FakePage(
        url="https://mp.toutiao.com/profile_v4/graphic/publish",
        evaluate_result=_publish_envelope(
            {"httpStatus": 200, "data": {"code": 0, "data": {"pgc_id": "1"}}, "raw": "{}"}
        ),
    )
    with pytest.raises(PublishError):
        ToutiaoInPageDriver().publish(
            page=page,
            context=None,
            payload=_payload(tmp_path, with_cover=False),
            stop_before_publish=False,
        )


# --- TASK 5: harden publish-response URL/pgc_id extraction ---------------------


def _ok_publish(inner):
    """HTTP-200 / code:0 publish dict carrying the given inner ``data`` payload."""
    return {"httpStatus": 200, "data": {"code": 0, "data": inner}, "raw": "{}"}


def test_map_publish_response_article_url_surfaces():
    result = _map_publish_response(
        _ok_publish({"pgc_id": "7", "article_url": "https://www.toutiao.com/article/7/"}),
        "标题",
    )
    assert result.url == "https://www.toutiao.com/article/7/"
    assert "7" in result.message


def test_map_publish_response_url_fallback_surfaces():
    result = _map_publish_response(
        _ok_publish({"pgc_id": "7", "url": "https://www.toutiao.com/i7/"}),
        "标题",
    )
    assert result.url == "https://www.toutiao.com/i7/"


def test_map_publish_response_display_url_fallback_surfaces():
    result = _map_publish_response(
        _ok_publish({"pgc_id": "7", "display_url": "https://www.toutiao.com/disp/7/"}),
        "标题",
    )
    assert result.url == "https://www.toutiao.com/disp/7/"


def test_map_publish_response_pgc_id_only_falls_back_to_pgc_id_url():
    result = _map_publish_response(_ok_publish({"pgc_id": "777"}), "标题")
    # No URL field at all -> never crash; url falls back to a pgc_id= form and
    # the message still carries the pgc_id.
    assert result.url == "pgc_id=777"
    assert "777" in result.message


def test_map_publish_response_id_fallback_for_pgc_id():
    result = _map_publish_response(_ok_publish({"id": "888"}), "标题")
    assert result.url == "pgc_id=888"
    assert "888" in result.message


def test_map_publish_response_no_inner_data_does_not_crash():
    # data has NO inner "data" key (i.e. {"code":0}) -> graceful, no crash.
    result = _map_publish_response({"httpStatus": 200, "data": {"code": 0}, "raw": "{}"}, "标题")
    assert result.url is None
    assert result.title == "标题"


def test_map_publish_response_inner_data_non_dict_does_not_crash():
    # data.data present but not a dict -> graceful, no crash.
    result = _map_publish_response(
        {"httpStatus": 200, "data": {"code": 0, "data": "oops"}, "raw": "{}"}, "标题"
    )
    assert result.url is None


def test_map_publish_response_code_none_is_success():
    # Success predicate keeps code in (0, None); code:None must not raise.
    result = _map_publish_response(
        {"httpStatus": 200, "data": {"data": {"pgc_id": "9"}}, "raw": "{}"}, "标题"
    )
    assert "9" in (result.url or "") or "9" in result.message


def test_map_publish_response_nonzero_code_still_raises():
    with pytest.raises(PublishError):
        _map_publish_response(
            {"httpStatus": 200, "data": {"code": 7, "message": "nope"}, "raw": "{}"}, "标题"
        )


def test_map_publish_response_non_200_still_raises():
    with pytest.raises(PublishError):
        _map_publish_response({"httpStatus": 500, "data": {"code": 0}, "raw": "boom"}, "标题")


# --- TASK 6: draft-vs-publish messaging ----------------------------------------


def test_publish_draft_message_indicates_draft(tmp_path):
    """stop_before_publish=True (save=0) -> message says DRAFT, not 发布成功."""
    page = _FakePage(
        url="https://mp.toutiao.com/profile_v4/graphic/publish",
        evaluate_result=_publish_envelope(
            {"httpStatus": 200, "data": {"code": 0, "data": {"pgc_id": "555"}}, "raw": "{}"}
        ),
    )
    result = ToutiaoInPageDriver().publish(
        page=page, context=None, payload=_payload(tmp_path), stop_before_publish=True
    )
    assert "草稿" in result.message
    assert "发布成功" not in result.message
    # pgc_id still present in the message.
    assert "555" in result.message


def test_publish_real_publish_message_indicates_publish(tmp_path):
    """stop_before_publish=False (save=1) -> message says real PUBLISH success."""
    page = _FakePage(
        url="https://mp.toutiao.com/profile_v4/graphic/publish",
        evaluate_result=_publish_envelope(
            {"httpStatus": 200, "data": {"code": 0, "data": {"pgc_id": "666"}}, "raw": "{}"}
        ),
    )
    result = ToutiaoInPageDriver().publish(
        page=page,
        context=None,
        payload=_payload(tmp_path, with_cover=True),
        stop_before_publish=False,
    )
    assert "发布成功" in result.message
    assert "草稿" not in result.message
    assert "666" in result.message
