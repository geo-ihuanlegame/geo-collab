# Toutiao In-Page Adapter (Milestone 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Toutiao's ~1000-line DOM-choreography publish with an in-page API adapter that issues a *signed* request to `POST /mp/agw/article/publish` from inside the live editor page, proving the architecture end-to-end via a draft-save round-trip.

**Architecture:** Keep the entire server-side stack (runner, worker, locks, audit, noVNC). Add a second Toutiao driver selected by env var `GEO_TOUTIAO_DRIVER=inpage`. Its `publish()` navigates to the editor (so ByteDance's acrawler/secsdk signing SDKs are loaded), serializes the article body to Toutiao's `<p>` HTML, builds the form body, and runs an in-page `XMLHttpRequest` via `page.evaluate` — riding the page's global request-signing hook (confirmed by the spike: every `/mp/agw/*` call carries `a_bogus`+`msToken`). The DOM driver stays untouched as the default and instant rollback.

**Tech Stack:** Python 3.12, FastAPI, Playwright (sync API), pytest. JS adapter executed via `page.evaluate`.

**Scope (Milestone 1 only):** title + text/heading body + **draft save** (`save=0`). Image upload (cover + body) and full `save=1` publish + manual-confirm are **Milestone 2** (separate plan), which first needs a capture of the uncaptured image-upload endpoint. See "Milestone 2 boundary" at the end.

**Reverse-engineering validation points** (inherent to the domain — confirmed by the live test in Task 6, not guessed away):
- Whether an in-page `XMLHttpRequest` inherits signing (`a_bogus`/`msToken`/`_signature`) + the `x-secsdk-csrf-token` header from the global hook. Primary path = bare XHR; documented fallback = explicit `window.byted_acrawler` sign / switch to `fetch`.
- The publish **response** JSON shape (the spike captured requests only). Result-mapping is defensive; Task 6 prints the raw response so the success predicate is locked against reality.

---

## File Structure

| File | Responsibility | Create/Modify |
|---|---|---|
| `server/app/modules/tasks/drivers/__init__.py` | add variant registry + `resolve_driver` | Modify |
| `server/app/modules/tasks/runner.py` | use `resolve_driver` at the selection seam (line ~270) | Modify |
| `server/app/modules/tasks/drivers/toutiao_html.py` | pure `body_segments_to_toutiao_html()` serializer | Create |
| `server/app/modules/tasks/drivers/adapters/toutiao_publish.js` | in-page XHR adapter (runs via `page.evaluate`) | Create |
| `server/app/modules/tasks/drivers/toutiao_inpage.py` | `ToutiaoInPageDriver`: form builder, `publish()`, result mapping, variant registration | Create |
| `server/app/main.py` | import the new driver module to trigger registration | Modify |
| `server/tests/test_driver_resolution.py` | env routing unit tests | Create |
| `server/tests/test_toutiao_html.py` | serializer unit tests | Create |
| `server/tests/test_toutiao_inpage.py` | form builder + result mapping unit tests (fake page) | Create |
| `server/tests/test_toutiao_inpage_live.py` | `@pytest.mark.live` guarded draft-save e2e | Create |
| `server/tests/conftest.py` | register `live` marker | Modify |
| `CLAUDE.md` | document `GEO_TOUTIAO_DRIVER` | Modify |

> **YAGNI note:** the design mentioned a shared `InPageDriver` base + `adapters/runtime.js` shim. With only one platform, do **not** build them yet — keep logic in `toutiao_inpage.py`. Extract the base when the 2nd platform lands.

---

## Task 1: Driver variant resolution (env routing)

**Files:**
- Modify: `server/app/modules/tasks/drivers/__init__.py`
- Modify: `server/app/modules/tasks/runner.py` (line ~270)
- Test: `server/tests/test_driver_resolution.py`

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_driver_resolution.py
from server.app.modules.tasks.drivers import (
    get_driver,
    register_variant,
    resolve_driver,
)


class _StubDriver:
    code = "toutiao"
    name = "stub-inpage"
    home_url = "https://mp.toutiao.com"
    publish_url = "https://mp.toutiao.com/x"

    def detect_logged_in(self, *, url, title, body):
        return True

    def publish(self, *, page, context, payload, stop_before_publish):
        raise NotImplementedError


def test_resolve_defaults_to_registered_driver(monkeypatch):
    monkeypatch.delenv("GEO_TOUTIAO_DRIVER", raising=False)
    assert resolve_driver("toutiao") is get_driver("toutiao")


def test_resolve_returns_variant_when_env_set(monkeypatch):
    stub = _StubDriver()
    register_variant("toutiao", "inpage", stub, replace=True)
    monkeypatch.setenv("GEO_TOUTIAO_DRIVER", "inpage")
    assert resolve_driver("toutiao") is stub


def test_resolve_unknown_variant_falls_back(monkeypatch):
    monkeypatch.setenv("GEO_TOUTIAO_DRIVER", "does-not-exist")
    assert resolve_driver("toutiao") is get_driver("toutiao")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_driver_resolution.py -v`
Expected: FAIL with `ImportError: cannot import name 'register_variant'`

- [ ] **Step 3: Add the variant registry to `drivers/__init__.py`**

Append after the existing `all_driver_codes()` function:

```python
import os

_VARIANTS: dict[tuple[str, str], PlatformDriver] = {}


def register_variant(
    platform_code: str, variant: str, driver: PlatformDriver, *, replace: bool = False
) -> None:
    key = (platform_code, variant)
    if key in _VARIANTS and not replace:
        raise ValueError(f"Driver variant already registered: {platform_code}/{variant}")
    _VARIANTS[key] = driver


def resolve_driver(platform_code: str) -> PlatformDriver:
    """Pick a driver honoring GEO_<PLATFORM>_DRIVER; fall back to the registry."""
    variant = os.environ.get(f"GEO_{platform_code.upper()}_DRIVER", "").strip()
    if variant:
        chosen = _VARIANTS.get((platform_code, variant))
        if chosen is not None:
            return chosen
    return get_driver(platform_code)
```

- [ ] **Step 4: Switch the runner to `resolve_driver`**

In `server/app/modules/tasks/runner.py`, change the import on line 30 and the call on line ~270:

```python
# line 30
from server.app.modules.tasks.drivers import resolve_driver
```
```python
# line ~270 (inside run_publish, replacing `driver = get_driver(platform_code)`)
    driver = resolve_driver(platform_code)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest server/tests/test_driver_resolution.py -v`
Expected: 3 PASS

- [ ] **Step 6: Commit**

```bash
git add server/app/modules/tasks/drivers/__init__.py server/app/modules/tasks/runner.py server/tests/test_driver_resolution.py
git commit -m "feat(drivers): add env-selected driver variant resolution"
```

---

## Task 2: Body → Toutiao content HTML serializer

Toutiao's publish body is `content=<p data-track="N">text</p>...` (confirmed by the spike). Bold runs → `<strong>`. Headings (Milestone 1) render as a bold paragraph — Toutiao's graphic editor stored plain `<p>` in the capture, so we keep it simple and avoid unsupported tags. Images are **not** handled in M1 (they need the upload API) — an image segment raises a clear error so we never silently drop content.

**Files:**
- Create: `server/app/modules/tasks/drivers/toutiao_html.py`
- Test: `server/tests/test_toutiao_html.py`

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_toutiao_html.py
import pytest

from server.app.modules.articles.parser import BodySegment
from server.app.modules.tasks.drivers.toutiao_html import (
    ToutiaoBodyError,
    body_segments_to_toutiao_html,
)


def test_plain_paragraphs():
    segs = [
        BodySegment(kind="text", text="第一段"),
        BodySegment(kind="text", text="\n"),
        BodySegment(kind="text", text="第二段"),
    ]
    assert body_segments_to_toutiao_html(segs) == (
        '<p data-track="1">第一段</p><p data-track="2">第二段</p>'
    )


def test_bold_run_wrapped_in_strong():
    segs = [
        BodySegment(kind="text", text="普通"),
        BodySegment(kind="text", text="加粗", bold=True),
    ]
    assert body_segments_to_toutiao_html(segs) == (
        '<p data-track="1">普通<strong>加粗</strong></p>'
    )


def test_heading_becomes_bold_paragraph():
    segs = [BodySegment(kind="text", text="小标题", heading_level=1)]
    assert body_segments_to_toutiao_html(segs) == (
        '<p data-track="1"><strong>小标题</strong></p>'
    )


def test_html_special_chars_escaped():
    segs = [BodySegment(kind="text", text="a<b>&c")]
    assert body_segments_to_toutiao_html(segs) == (
        '<p data-track="1">a&lt;b&gt;&amp;c</p>'
    )


def test_empty_body_raises():
    with pytest.raises(ToutiaoBodyError):
        body_segments_to_toutiao_html([])


def test_image_segment_raises_in_m1():
    with pytest.raises(ToutiaoBodyError):
        body_segments_to_toutiao_html([BodySegment(kind="image", image_asset_id="a1")])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_toutiao_html.py -v`
Expected: FAIL with `ModuleNotFoundError: ...toutiao_html`

- [ ] **Step 3: Write the serializer**

```python
# server/app/modules/tasks/drivers/toutiao_html.py
from __future__ import annotations

from html import escape

from server.app.modules.articles.parser import BodySegment


class ToutiaoBodyError(Exception):
    """Raised when the article body cannot be serialized for Toutiao."""


def _run_html(text: str, bold: bool) -> str:
    inner = escape(text, quote=False)
    return f"<strong>{inner}</strong>" if bold else inner


def body_segments_to_toutiao_html(segments: list[BodySegment]) -> str:
    """Serialize parsed body segments into Toutiao `<p data-track="N">` HTML.

    Paragraph break = a text segment whose text is exactly "\\n".
    Headings render as a bold paragraph (M1 has no dedicated heading tag).
    Image segments are rejected in M1 (require the upload API — Milestone 2).
    """
    paragraphs: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            joined = "".join(current)
            if joined.strip("​ "):  # ignore whitespace-only paragraphs
                paragraphs.append(joined)
        current.clear()

    for seg in segments:
        if seg.kind == "image":
            raise ToutiaoBodyError(
                f"正文图片暂不支持（Milestone 2）: asset_id={seg.image_asset_id}"
            )
        if seg.text == "\n":
            flush()
            continue
        if not seg.text:
            continue
        bold = seg.bold or seg.heading_level is not None
        current.append(_run_html(seg.text, bold))
    flush()

    if not paragraphs:
        raise ToutiaoBodyError("正文为空")
    return "".join(
        f'<p data-track="{i + 1}">{p}</p>' for i, p in enumerate(paragraphs)
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest server/tests/test_toutiao_html.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/tasks/drivers/toutiao_html.py server/tests/test_toutiao_html.py
git commit -m "feat(toutiao): add body-segment to Toutiao content HTML serializer"
```

---

## Task 3: Publish form-body builder

Builds the form fields for `POST /mp/agw/article/publish`. Field set + constants taken verbatim from the spike capture (`source=29`, `extra` JSON, `draft_form_data`, flags). `save=0` for drafts (M1), `save=1` reserved for M2. `pgc_feed_covers=[]` (empty in M1 — no cover yet).

**Files:**
- Create: `server/app/modules/tasks/drivers/toutiao_inpage.py` (form builder only this task)
- Test: `server/tests/test_toutiao_inpage.py`

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_toutiao_inpage.py
import json

from server.app.modules.tasks.drivers.toutiao_inpage import build_publish_form


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
    form = build_publish_form(
        title="t", content_html="<p>x</p>", pgc_id="7646670891934089737"
    )
    assert form["pgc_id"] == "7646670891934089737"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_toutiao_inpage.py -v`
Expected: FAIL with `ModuleNotFoundError: ...toutiao_inpage`

- [ ] **Step 3: Write the form builder**

```python
# server/app/modules/tasks/drivers/toutiao_inpage.py
from __future__ import annotations

import json
import re

_EXTRA_BASE = {
    "content_source": 100000000402,
    "is_multi_title": 0,
    "sub_titles": [],
    "gd_ext": {
        "entrance": "",
        "from_page": "publisher_mp",
        "enter_from": "PC",
        "device_platform": "mp",
        "is_message": 0,
    },
    "tuwen_wtt_transfer_switch": "1",
}


def _word_count(content_html: str) -> int:
    return len(re.sub(r"<[^>]+>", "", content_html))


def build_publish_form(
    *,
    title: str,
    content_html: str,
    save: int = 0,
    pgc_id: str | None = None,
) -> dict[str, str]:
    """Build the application/x-www-form-urlencoded fields for the publish call.

    Constants mirror the real editor request captured 2026-06-02 (see design doc
    §6 "Spike 结论 · phase 2"). Milestone 1 sends save=0 (draft) with no cover.
    """
    extra = dict(_EXTRA_BASE)
    extra["content_word_cnt"] = _word_count(content_html)

    form: dict[str, str] = {
        "source": "29",
        "extra": json.dumps(extra, ensure_ascii=False, separators=(",", ":")),
        "content": content_html,
        "title": title,
        "search_creation_info": json.dumps(
            {"searchTopOne": 0, "abstract": "", "clue_id": ""}, separators=(",", ":")
        ),
        "mp_editor_stat": "{}",
        "is_refute_rumor": "0",
        "save": str(save),
        "entrance": "main" if save == 1 else "",
        "draft_form_data": json.dumps({"coverType": 2}, separators=(",", ":")),
        "pgc_feed_covers": "[]",
        "article_ad_type": "3",
        "is_fans_article": "0",
        "govern_forward": "0",
        "praise": "0",
        "disable_praise": "0",
        "tree_plan_article": "0",
        "claim_exclusive": "0",
        "timer_status": "0",
    }
    if pgc_id:
        form["pgc_id"] = pgc_id
    return form
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest server/tests/test_toutiao_inpage.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/tasks/drivers/toutiao_inpage.py server/tests/test_toutiao_inpage.py
git commit -m "feat(toutiao): add publish form-body builder for in-page adapter"
```

---

## Task 4: In-page adapter JS + driver `publish()` with result mapping

**Files:**
- Create: `server/app/modules/tasks/drivers/adapters/toutiao_publish.js`
- Modify: `server/app/modules/tasks/drivers/toutiao_inpage.py` (add driver class + mapping + registration)
- Test: `server/tests/test_toutiao_inpage.py` (add mapping tests with a fake page)

- [ ] **Step 1: Write the failing test (result mapping with a fake page)**

Append to `server/tests/test_toutiao_inpage.py`:

```python
import pytest

from server.app.modules.articles.parser import BodySegment
from server.app.modules.tasks.drivers.base import PublishError, UserInputRequired
from server.app.modules.tasks.drivers.toutiao_inpage import ToutiaoInPageDriver


class _FakePage:
    """Minimal stand-in for a Playwright Page used in driver.publish()."""

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


def _payload():
    from server.app.modules.tasks.drivers.base import PublishPayload
    from pathlib import Path

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
        evaluate_result={"httpStatus": 200, "data": {"code": 0, "data": {"pgc_id": "999"}}, "raw": "{}"},
    )
    driver = ToutiaoInPageDriver()
    result = driver.publish(page=page, context=None, payload=_payload(), stop_before_publish=True)
    assert result.title == "今天是周二"
    assert "999" in (result.url or "") or "999" in result.message


def test_publish_login_redirect_raises_user_input_required():
    page = _FakePage(url="https://mp.toutiao.com/auth/page/login?x=1", evaluate_result=None)
    driver = ToutiaoInPageDriver()
    with pytest.raises(UserInputRequired):
        driver.publish(page=page, context=None, payload=_payload(), stop_before_publish=True)


def test_publish_api_error_raises_publish_error():
    page = _FakePage(
        url="https://mp.toutiao.com/profile_v4/graphic/publish",
        evaluate_result={"httpStatus": 200, "data": {"code": 1, "message": "verify required"}, "raw": "{...}"},
    )
    driver = ToutiaoInPageDriver()
    with pytest.raises(PublishError):
        driver.publish(page=page, context=None, payload=_payload(), stop_before_publish=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_toutiao_inpage.py -v`
Expected: FAIL with `ImportError: cannot import name 'ToutiaoInPageDriver'`

- [ ] **Step 3: Write the JS adapter**

```javascript
// server/app/modules/tasks/drivers/adapters/toutiao_publish.js
// Runs inside the live Toutiao editor page via page.evaluate(js, arg).
// arg = { form: { <field>: <value>, ... } }
// Uses XMLHttpRequest so the page's global request hook (acrawler/secsdk)
// auto-appends a_bogus / msToken / _signature / x-secsdk-csrf-token.
// Returns { httpStatus, data, raw }.
async (arg) => {
  const url =
    "https://mp.toutiao.com/mp/agw/article/publish" +
    "?source=mp&type=article&aid=1231&mp_publish_ab_val=0";
  const body = new URLSearchParams(arg.form).toString();
  const res = await new Promise((resolve) => {
    try {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", url, true);
      xhr.setRequestHeader(
        "content-type",
        "application/x-www-form-urlencoded;charset=UTF-8"
      );
      xhr.onload = () => resolve({ status: xhr.status, text: xhr.responseText });
      xhr.onerror = () => resolve({ status: -1, text: "xhr network error" });
      xhr.send(body);
    } catch (e) {
      resolve({ status: -2, text: String(e) });
    }
  });
  let data = null;
  try {
    data = JSON.parse(res.text);
  } catch (_) {}
  return { httpStatus: res.status, data: data, raw: (res.text || "").slice(0, 1200) };
};
```

- [ ] **Step 4: Add the driver class + mapping + registration to `toutiao_inpage.py`**

Append to `server/app/modules/tasks/drivers/toutiao_inpage.py`:

```python
import logging
from pathlib import Path
from typing import Any

from server.app.modules.tasks.drivers import register_variant
from server.app.modules.tasks.drivers.base import (
    PublishError,
    PublishPayload,
    PublishResult,
    UserInputRequired,
)
from server.app.modules.tasks.drivers.toutiao_html import body_segments_to_toutiao_html

logger = logging.getLogger(__name__)

PUBLISH_URL = "https://mp.toutiao.com/profile_v4/graphic/publish"
_ADAPTER_JS = (Path(__file__).parent / "adapters" / "toutiao_publish.js").read_text(
    encoding="utf-8"
)
_LOGIN_HINTS = ("/auth/page/login", "passport", "/sso", "login")


def _is_logged_out(url: str) -> bool:
    return any(hint in url for hint in _LOGIN_HINTS)


def _map_publish_response(result: dict[str, Any], title: str) -> PublishResult:
    """Map the in-page XHR result into a PublishResult, or raise.

    Defensive against the exact success shape (spike captured requests only;
    the live test in Task 6 confirms it). Success predicate: HTTP 200 AND a
    truthy/zero `code` with no error message.
    """
    http_status = result.get("httpStatus")
    data = result.get("data")
    if http_status != 200 or not isinstance(data, dict):
        raise PublishError(
            f"头条发布请求失败: httpStatus={http_status}; raw={result.get('raw')}"
        )
    code = data.get("code")
    if code not in (0, None):
        message = data.get("message") or data.get("msg") or result.get("raw")
        raise PublishError(f"头条发布被拒: code={code}; message={message}")

    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    pgc_id = str(inner.get("pgc_id") or inner.get("id") or "") or None
    url = inner.get("article_url") or inner.get("url")
    return PublishResult(
        url=url or (f"pgc_id={pgc_id}" if pgc_id else None),
        title=title,
        message=f"头条草稿/发布成功: pgc_id={pgc_id}",
    )


class ToutiaoInPageDriver:
    code = "toutiao"
    name = "头条号(页内)"
    home_url = "https://mp.toutiao.com"
    publish_url = PUBLISH_URL

    def detect_logged_in(self, *, url: str, title: str, body: str) -> bool:
        if _is_logged_out(url):
            return False
        return "mp.toutiao.com" in url

    def publish(
        self,
        *,
        page: Any,
        context: Any,
        payload: PublishPayload,
        stop_before_publish: bool,
    ) -> PublishResult:
        content_html = body_segments_to_toutiao_html(payload.body_segments)
        page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)  # let acrawler/secsdk load + hook the request layer
        if _is_logged_out(page.url):
            raise UserInputRequired(
                "头条账号未登录或登录态失效，需要人工接管",
                error_type="login_required",
            )
        # M1: draft save (save=0); M2 flips to save=1 after cover upload.
        form = build_publish_form(
            title=payload.title, content_html=content_html, save=0
        )
        result = page.evaluate(_ADAPTER_JS, {"form": form})
        logger.info("toutiao in-page publish raw response: %s", result.get("raw"))
        return _map_publish_response(result, payload.title)


register_variant("toutiao", "inpage", ToutiaoInPageDriver())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest server/tests/test_toutiao_inpage.py -v`
Expected: 5 PASS (2 from Task 3 + 3 new)

- [ ] **Step 6: Commit**

```bash
git add server/app/modules/tasks/drivers/adapters/toutiao_publish.js server/app/modules/tasks/drivers/toutiao_inpage.py server/tests/test_toutiao_inpage.py
git commit -m "feat(toutiao): in-page XHR publish adapter + result mapping"
```

---

## Task 5: Register the driver at app startup + document the env var

**Files:**
- Modify: `server/app/main.py`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Import the driver module in `create_app()`**

In `server/app/main.py`, next to the existing `import server.app.modules.tasks.drivers.toutiao  # noqa: F401` line, add:

```python
import server.app.modules.tasks.drivers.toutiao_inpage  # noqa: F401
```

- [ ] **Step 2: Verify registration works (no test file; quick import check)**

Run:
```bash
python -c "import server.app.modules.tasks.drivers.toutiao_inpage; import os; os.environ['GEO_TOUTIAO_DRIVER']='inpage'; from server.app.modules.tasks.drivers import resolve_driver; print(type(resolve_driver('toutiao')).__name__)"
```
Expected: `ToutiaoInPageDriver`

- [ ] **Step 3: Document the env var in `CLAUDE.md`**

Under the Task Execution / drivers section, add a line:

```markdown
- 头条发布驱动可切换：`GEO_TOUTIAO_DRIVER=inpage` 走页内 API 适配器（`toutiao_inpage.py`），未设或 `dom` 走原 Playwright DOM 驱动（`toutiao.py`，默认）。两者都注册，便于灰度与回滚。
```

- [ ] **Step 4: Run the full backend suite to confirm no regressions**

Run: `pytest server/tests/ -q` (MySQL-less subset is fine locally)
Expected: PASS / skips for `@pytest.mark.mysql`; no new failures.

- [ ] **Step 5: Commit**

```bash
git add server/app/main.py CLAUDE.md
git commit -m "feat(toutiao): register in-page driver variant + document GEO_TOUTIAO_DRIVER"
```

---

## Task 6: Guarded live draft-save e2e (the architecture proof)

This is the **reverse-engineering validation gate**: it confirms that our in-page XHR is auto-signed and the draft is accepted. It is `@pytest.mark.live`, skipped unless `GEO_LIVE_TOUTIAO_PROFILE` points at a logged-in Chromium user-data-dir (e.g. the spike's `geo_spike_userdata` in your temp dir). Runs headed on Windows — no Xvfb needed because it drives the driver directly, bypassing the remote-session machinery.

**Files:**
- Modify: `server/tests/conftest.py` (register `live` marker)
- Create: `server/tests/test_toutiao_inpage_live.py`

- [ ] **Step 1: Register the `live` marker in `conftest.py`**

In `server/tests/conftest.py`, where `mysql` is registered (look for `config.addinivalue_line("markers", ...)`), add:

```python
    config.addinivalue_line(
        "markers", "live: hits a real external site; skipped unless explicitly enabled"
    )
```

- [ ] **Step 2: Write the live test**

```python
# server/tests/test_toutiao_inpage_live.py
import os
from pathlib import Path

import pytest

from server.app.modules.articles.parser import BodySegment
from server.app.modules.tasks.drivers.base import PublishPayload
from server.app.modules.tasks.drivers.toutiao_inpage import ToutiaoInPageDriver

pytestmark = pytest.mark.live

PROFILE = os.environ.get("GEO_LIVE_TOUTIAO_PROFILE")


@pytest.mark.skipif(not PROFILE, reason="set GEO_LIVE_TOUTIAO_PROFILE to a logged-in user-data-dir")
def test_live_draft_save_round_trip():
    from playwright.sync_api import sync_playwright

    payload = PublishPayload(
        title="架构验证草稿-请忽略",
        cover_asset_path=Path("unused.png"),
        body_segments=[
            BodySegment(kind="text", text="这是页内适配器架构验证草稿。"),
            BodySegment(kind="text", text="\n"),
            BodySegment(kind="text", text="第二段，含", bold=False),
            BodySegment(kind="text", text="加粗", bold=True),
        ],
        account_key="live",
        state_path=Path("unused.json"),
        display_name="live",
        platform_code="toutiao",
    )
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE, headless=False, locale="zh-CN")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            # save=0 draft — no cover required (confirmed by spike capture)
            result = ToutiaoInPageDriver().publish(
                page=page, context=ctx, payload=payload, stop_before_publish=True
            )
        finally:
            ctx.close()
    # The driver logs the raw response; result.message carries the pgc_id on success.
    assert result.title == "架构验证草稿-请忽略"
    assert "成功" in result.message
```

- [ ] **Step 3: Run the live test against your logged-in profile**

Run (point at the spike's logged-in profile from earlier):
```powershell
$env:GEO_LIVE_TOUTIAO_PROFILE = "$env:TEMP\geo_spike_userdata"
pytest server/tests/test_toutiao_inpage_live.py -v -s -m live
```
Expected: PASS. **Watch the `-s` output for `toutiao in-page publish raw response:`** — that is the real success JSON.

- [ ] **Step 4: Lock the success predicate against the real response**

If the raw response shows different field names than the defensive mapping assumes (e.g. success is `{"message":"success","data":{"pgc_id":...}}` rather than `code==0`), adjust `_map_publish_response` in `toutiao_inpage.py` to match, and re-run Step 3 until green.

If instead the draft is **rejected with a signature error** (e.g. `code` indicates verify/sign failure), the global hook did not sign our XHR — apply the documented fallback: in `toutiao_publish.js`, before `xhr.send`, call the page's signer — `window.byted_acrawler && window.byted_acrawler.frontierSign && window.byted_acrawler.frontierSign({url})` — or switch the transport to `fetch`. Re-run until green. Record which path worked in the design doc §6.

- [ ] **Step 5: Commit**

```bash
git add server/tests/conftest.py server/tests/test_toutiao_inpage_live.py server/app/modules/tasks/drivers/toutiao_inpage.py
git commit -m "test(toutiao): guarded live draft-save e2e for in-page adapter"
```

---

## Milestone 2 boundary (NOT in this plan)

Do **not** write M2 tasks until the image-upload endpoint is captured — guessing its API would be a placeholder. M2 covers: cover + body image upload via Toutiao's image API, `pgc_feed_covers` assembly, the `photo/info` resolve call, full `save=1` publish, and `stop_before_publish`/manual-confirm (re-publish-saved-draft on confirm, per design doc §10).

**M2 Task 0 (capture, do first):** extend `spike_toutiao_publish_capture.py` to also log requests to image-upload hosts (broaden `_looks_publishy` to match `imagex`, `/upload/`, `vcloud`, and any request whose response body contains `tos-cn-i-`) **and capture response bodies**. Run it once (upload a cover + one body image, then publish). Distill the upload contract into design doc §6, then author M2 against it.

---

## Self-Review

- **Spec coverage:** design doc §4 two-layer split → Tasks 2/3/4; §5 contract (form fields) → Task 3; §6 signing (global hook / in-page XHR) → Task 4 + validated Task 6; §9 env coexistence → Tasks 1/5; §6.1 body=`<p>` HTML → Task 2. §10 manual-confirm, image transport (base64), and full publish are explicitly deferred to M2 (design §15 scopes Toutiao-first as architecture validation — M1 satisfies it).
- **Placeholder scan:** none. The two reverse-engineering unknowns (XHR auto-sign, response shape) are explicit, with a primary path + documented fallback resolved in Task 6 — not vague TODOs.
- **Type consistency:** `body_segments_to_toutiao_html(list[BodySegment]) -> str`, `build_publish_form(*, title, content_html, save=0, pgc_id=None) -> dict[str,str]`, `ToutiaoInPageDriver.publish(...) -> PublishResult`, `resolve_driver(str) -> PlatformDriver`, `register_variant(str, str, PlatformDriver, *, replace=False)` — names match across tasks 1–6.
