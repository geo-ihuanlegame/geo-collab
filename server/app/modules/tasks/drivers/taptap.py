"""TapTap 长帖（topic）发布驱动：cookie-session 纯 HTTP（mode='api'，无浏览器）。

设计稿 docs/plans/2026-06-23-taptap-driver.md。鉴权用登录 cookie 罐（+ XSRF + X-UA），
非 app_id/secret token，故 ``auth='cookie'``：runner_api 据此读 storage_state（解密）与
论坛配置注入 payload，本驱动不碰 ORM（守 CLAUDE.md「驱动不碰 ORM」）。

流程：正文图逐张传七牛 → key→url 映射 + image_infos → content_json 转 contents →
create-topic（草稿）→ publish-topic（公开）。登录（拿/刷 cookie）仍走浏览器一次（noVNC），
低频；本驱动只负责发布与登录态检测。
"""

from __future__ import annotations

import logging

import httpx

from server.app.modules.tasks.drivers import register
from server.app.modules.tasks.drivers.base import (
    NOOP_COMMIT_GUARD,
    ApiPublishPayload,
    PublishError,
    PublishResult,
)
from server.app.modules.tasks.drivers.taptap_client import (
    TapTapApiError,
    build_forum_bindings,
    create_topic,
    get_image_upload_token,
    make_client,
    publish_topic,
    upload_image_to_qiniu,
)
from server.app.modules.tasks.drivers.taptap_contents import tiptap_to_contents
from server.app.shared.resilience import RetryPolicy, default_is_transient, retry_call

logger = logging.getLogger(__name__)

# 登录页 / 验证码标记：命中即判未登录（与 toutiao 同思路）
_LOGIN_URL_HINTS = ("login", "passport", "sso", "accounts.taptap", "/auth")
_LOGIN_TEXT_HINTS = ("扫码登录", "登录 TapTap", "验证码", "手机号登录")


class TapTapDriver:
    code = "taptap"
    name = "TapTap"
    # 登录落地页用创作者中心：已登录留在 /creator；未登录会被重定向到登录页（命中 _LOGIN_URL_HINTS）。
    home_url = "https://www.taptap.cn/creator"
    publish_url = "https://www.taptap.cn/creator/edit?type=topic"
    mode = "api"  # build_publish_runner_for_record 据此走 runner_api
    auth = (
        "cookie"  # runner_api 据此走 cookie 分支（读 storage_state + api_credentials），不拉 token
    )

    def detect_logged_in(self, *, url: str, title: str, body: str) -> bool:
        """登录浏览器路径用：判断 home_url(/creator) 是否已登录。

        信号：登录态留在 taptap.cn/creator；未登录被重定向到登录页 / 弹登录框。
        注意：登录页 DOM 未亲历，启发式以 URL 重定向 + 登录文案为准；首次真实登录后按需收紧。
        """
        lowered_url = (url or "").lower()
        if "taptap" not in lowered_url:
            return False
        if any(hint in lowered_url for hint in _LOGIN_URL_HINTS):
            return False
        text = f"{title or ''}\n{body or ''}"
        if any(hint in text for hint in _LOGIN_TEXT_HINTS):
            return False
        # 留在 creator / moment 上下文 = 已进创作者态
        return "creator" in lowered_url or "/moment" in lowered_url

    def extract_platform_user_id_sync(self, *, page) -> str | None:  # pragma: no cover
        return None  # 同步登录检测路径不强抽 VID（async 版 + cookie 体检会回填）

    async def extract_platform_user_id_async(self, *, page) -> str | None:
        """登录态活页上 best-effort 抽 VID：页内 fetch /account-profile/v1/me 取 data.id。

        失败（含需 X-UA 而被拒）返回 None，绝不抛——VID 也会被 cookie 体检后续回填。
        """
        try:
            vid = await page.evaluate(
                """async () => {
                    try {
                        const r = await fetch('/webapiv2/account-profile/v1/me',
                            {credentials: 'include', headers: {'Accept': 'application/json'}});
                        if (!r.ok) return null;
                        const j = await r.json();
                        const id = j && j.data && j.data.id;
                        return id != null ? String(id) : null;
                    } catch (e) { return null; }
                }"""
            )
            return vid or None
        except Exception:
            logger.warning("TapTap VID 抽取失败（best-effort，忽略）", exc_info=True)
            return None

    def publish(
        self, *, page, context, payload, stop_before_publish, commit_guard=None, retry_policy=None
    ):  # pragma: no cover
        raise PublishError("TapTap 为 cookie-session API 接入，不支持浏览器发布路径")

    def publish_api(
        self,
        *,
        payload: ApiPublishPayload,
        transport: httpx.BaseTransport | None = None,
        commit_guard=None,
        retry_policy=None,
    ) -> PublishResult:
        """transport 仅供测试注入 MockTransport（同一 transport 按 host 路由 taptap + 七牛）。

        commit_guard/retry_policy（#133）：传图/取 token 等幂等步走 retry_call；不可逆的
        publish-topic（公开）包进 commit_guard.committing()——网络中断后受理未知则升
        CommitUncertainError（不自动重发），业务/鉴权拒绝(带 errcode)则为干净失败可重试。
        """
        if commit_guard is None:
            commit_guard = NOOP_COMMIT_GUARD
        policy = retry_policy or RetryPolicy()
        forum = payload.forum or {}
        app_id = forum.get("app_id")
        group_id = forum.get("group_id")
        x_ua = forum.get("x_ua")
        if not app_id or not group_id or not x_ua:
            raise PublishError(
                "TapTap 账号未配置论坛绑定（需 api_credentials 含 app_id/group_id/x_ua），请先在媒体矩阵设置"
            )
        if not payload.state:
            raise PublishError("TapTap 账号缺登录态（storage_state），请先在媒体矩阵登录")

        try:
            client = make_client(
                payload.state, app_id=app_id, group_id=group_id, transport=transport
            )
        except TapTapApiError as exc:
            raise PublishError(str(exc)) from exc

        qiniu_client = (
            httpx.Client(timeout=60.0, transport=transport) if transport is not None else None
        )
        try:
            url_map, image_infos = self._upload_images(
                client,
                qiniu_client,
                x_ua=x_ua,
                image_paths=payload.image_paths or {},
                policy=policy,
            )
            contents = tiptap_to_contents(payload.content_json or {}, url_map)
            if not contents:
                raise PublishError("正文为空，无法发布 TapTap 长帖")
            forum_bindings = build_forum_bindings(group_id)
            # 草稿（私有、可重复）走 retry_call；publish-topic（公开、不可逆）只进 commit_guard。
            draft_id = retry_call(
                lambda: create_topic(
                    client,
                    x_ua=x_ua,
                    title=payload.title,
                    contents=contents,
                    forum_bindings=forum_bindings,
                    image_infos=image_infos,
                ),
                policy=policy,
                is_transient=default_is_transient,
            )
            with commit_guard.committing():
                moment_id = publish_topic(
                    client,
                    x_ua=x_ua,
                    draft_id=draft_id,
                    title=payload.title,
                    contents=contents,
                    forum_bindings=forum_bindings,
                    image_infos=image_infos,
                )
        except TapTapApiError as exc:
            raise PublishError(str(exc)) from exc
        finally:
            client.close()
            if qiniu_client is not None:
                qiniu_client.close()

        url = f"https://www.taptap.cn/moment/{moment_id}"
        return PublishResult(url=url, title=payload.title, message=f"TapTap 长帖已发布 {url}")

    @staticmethod
    def _upload_images(client, qiniu_client, *, x_ua, image_paths, policy=None):
        """逐张传七牛，返回 (key→url 映射, image_infos 列表)。取 token / 上传均幂等，走 retry_call。

        key 缺图（删了）的不在 paths 里、自然跳过（上游 runner 已过滤）。
        """
        policy = policy or RetryPolicy()
        url_map: dict[str, str] = {}
        image_infos: list[dict] = []
        for key, path in image_paths.items():
            data = path.read_bytes()
            token = retry_call(
                lambda: get_image_upload_token(client, x_ua=x_ua),
                policy=policy,
                is_transient=default_is_transient,
            )
            result = retry_call(
                lambda t=token, d=data, p=path: upload_image_to_qiniu(
                    t, d, p.name, client=qiniu_client
                ),
                policy=policy,
                is_transient=default_is_transient,
            )
            url_map[key] = result["url"]
            image_infos.append({"url": result["url"], "info": result["info"]})
        return url_map, image_infos


register(TapTapDriver())
