"""TapTap cookie 体检核心 check_account_cookie 单测（MockTransport，无 DB / 无网络）。"""

from __future__ import annotations

import httpx

from server.app.modules.tasks.taptap_health import check_account_cookie

_STATE = {"cookies": [{"name": "XSRF-TOKEN", "value": "x", "domain": ".taptap.cn"}]}
_FORUM = {"app_id": 43639, "group_id": 4444, "x_ua": "V=1&VID=780586114"}


def _transport(status, body):
    return httpx.MockTransport(lambda req: httpx.Response(status, json=body))


def test_valid_cookie_returns_ok_with_vid():
    transport = _transport(200, {"success": True, "data": {"id": 780586114, "name": "u"}})
    r = check_account_cookie(_STATE, _FORUM, transport=transport)
    assert r.ok is True and r.expired is False
    assert r.vid == "780586114"


def test_401_marks_expired():
    transport = _transport(401, {"success": False})
    r = check_account_cookie(_STATE, _FORUM, transport=transport)
    assert r.ok is False and r.expired is True


def test_success_false_marks_expired():
    transport = _transport(200, {"success": False, "data": {"msg": "未登录"}})
    r = check_account_cookie(_STATE, _FORUM, transport=transport)
    assert r.ok is False and r.expired is True


def test_missing_state_is_expired():
    r = check_account_cookie(None, _FORUM, transport=_transport(200, {"success": True, "data": {}}))
    assert r.ok is False and r.expired is True


def test_missing_xsrf_cookie_is_expired():
    bad = {"cookies": [{"name": "sess", "value": "x", "domain": ".taptap.cn"}]}
    r = check_account_cookie(bad, _FORUM, transport=_transport(200, {"success": True, "data": {}}))
    assert r.ok is False and r.expired is True


def test_missing_x_ua_not_expired_just_skipped():
    r = check_account_cookie(_STATE, {"app_id": 1, "group_id": 2}, transport=_transport(200, {}))
    assert r.ok is False and r.expired is False  # 配置不全 → 跳过、不误判失效


def test_transient_network_error_not_expired():
    def boom(req):
        raise httpx.ConnectError("boom")

    r = check_account_cookie(_STATE, _FORUM, transport=httpx.MockTransport(boom))
    assert r.ok is False and r.expired is False  # 瞬时错误不翻状态
