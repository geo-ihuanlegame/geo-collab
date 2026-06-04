# Toutiao In-Page Adapter — Milestone 2 Implementation Plan (DRAFT)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Take the M1 in-page adapter from a validated *draft-save* to a fully working *publish* — cover + body image upload, real `save=1` publish, and `stop_before_publish`/manual-confirm — and validate it on a clean (production) network where secsdk is healthy.

**Architecture:** Unchanged in-page model (`page.evaluate` → `XMLHttpRequest`, signing inherited from the page's global acrawler/secsdk hook). Extends the existing `ToutiaoInPageDriver` in `server/app/modules/tasks/drivers/toutiao_inpage.py`; no new architecture.

**Tech Stack:** Python 3.12, Playwright (sync), pytest; JS via `page.evaluate`.

---

## ⚠️ DRAFT status — read first

1. **Environment gate (Phase 0).** M1 proved the architecture, but local saves return `code=7050` — a **dev-box secsdk `DOWNGRADE`** (the security handshake can't complete on the proxied dev machine; the *editor's own* save fails identically). Root-cause detail: design doc `§6 → M2 调查记录`. Every M2 task below assumes saves succeed, so **Phase 0 must pass on a clean (production) network before any implementation effort.**
2. **One capture-gated boundary (Phase 2).** Cover/body image upload uses a Toutiao upload API we have **not captured** (the M2 capture saw `/article_material/photo/info` *resolve* but not the *upload* itself). Phase 2's implementation tasks (2.1+) are deliberately left as a **capture-then-author** boundary — writing concrete code for an uncaptured API would be a guess. Everything else is concrete.

---

## Phase 0 — Production save-validation (the gate; no code)

### Task 0.1: Confirm `save` works on a clean network
- [ ] Run on the production/Docker environment (or any clean, non-flagged network) with a logged-in Toutiao account, `GEO_TOUTIAO_DRIVER=inpage`.
- [ ] Drive the editor's own save (`spike_toutiao_editor_save.py`) **and** our adapter (`spike_toutiao_probe_outgoing.py` / the `@pytest.mark.live` test).
- [ ] **PASS criterion:** save returns `code:0` (not `7050`) and `x-secsdk-csrf-token` is a **real token**, not `DOWNGRADE`.
- [ ] **If still `7050`/`DOWNGRADE` on production → STOP and escalate.** The secsdk handshake is failing in production too; investigate which secsdk dependency is blocked (`mssdk.bytedance.com`, `security.zijieapi.com`, `bdms.js`, `acrawler.js`) before any further work. Do not build on a broken save.

---

## Phase 1 — Adapter robustness (concrete, do now)

### Task 1.1: Harden post-`goto` login detection (fix the timing false-positive)

**Files:**
- Modify: `server/app/modules/tasks/drivers/toutiao_inpage.py`
- Test: `server/tests/test_toutiao_inpage.py`

**Problem:** `publish()` does `if _is_logged_out(page.url)` immediately after `goto` + 2.5 s, which transiently catches a redirect and wrongly raises `UserInputRequired` (observed twice in the fresh-login test). Conclude "logged out" only if a login wall *persists*.

- [ ] **Step 1: Write the failing test** — append to `server/tests/test_toutiao_inpage.py`:

```python
def test_publish_waits_for_editor_then_proceeds():
    """Editor title box appears after a couple polls -> no false UserInputRequired."""

    class _SlowReadyPage(_FakePage):
        def __init__(self):
            super().__init__(
                url="https://mp.toutiao.com/profile_v4/graphic/publish",
                evaluate_result={"httpStatus": 200, "data": {"code": 0, "data": {"pgc_id": "7"}}, "raw": "{}"},
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
```

- [ ] **Step 2: Run, verify it fails** (`_FakePage` has no `get_by_role`, or the one-shot check raises early):
`& "C:\Users\Administrator\miniconda3\envs\geo_xzpt\python.exe" -m pytest server/tests/test_toutiao_inpage.py -v`

- [ ] **Step 3: Implement `_wait_editor_ready` and use it** — in `toutiao_inpage.py`:

```python
def _wait_editor_ready(page: Any, timeout_ms: int = 15000) -> bool:
    """True once the editor (title box) is present; False if a login wall persists.

    Tolerates a transient post-goto redirect: only concludes logged-out if the
    login URL is still showing after the timeout.
    """
    waited, step = 0, 500
    while waited < timeout_ms:
        try:
            if page.get_by_role("textbox", name="请输入文章标题").count() > 0:
                return True
        except Exception:
            pass
        page.wait_for_timeout(step)
        waited += step
    return not _is_logged_out(page.url)
```

Replace the check in `publish()`:

```python
        page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=60000)
        if not _wait_editor_ready(page):
            raise UserInputRequired(
                "头条账号未登录或登录态失效，需要人工接管",
                error_type="login_required",
            )
```
(Remove the now-redundant `page.wait_for_timeout(_SECSDK_SETTLE_MS)` + one-shot `_is_logged_out` lines; `_wait_editor_ready` subsumes the settle wait. Update the existing `_FakePage` to grow a default `get_by_role` returning a locator whose `count()` is 1, so the M1 tests still pass.)

- [ ] **Step 4: Run tests** (all M1 + 2 new pass). **Step 5:** ruff check + format the changed files. **Step 6:** commit `fix(toutiao): wait for editor-ready before declaring logout`.

---

## Phase 2 — Cover + body image upload (CAPTURE-GATED)

### Task 2.0: Capture the image-upload API (concrete; do on a clean network)
- [x] **Capture script ready: `spike_toutiao_image_capture.py`** (new sibling, not an edit to `spike_toutiao_m2_capture.py`, per repo convention "each spike is its own file / 别删 spike_*.py"). Fixes the gap the recon found: m2_capture's matcher requires a `toutiao.com`/`bytedance.com` host and would **miss** the ByteDance ImageX upload CDN hosts. New script broadens the host matcher (imagex/byteimg/vcloud/volces…) + sniffs any JSON/text response containing `tos-cn-i-`, summarizes multipart request bodies, keeps the `/article_material/photo/info` capture. Output → `E:/geo/spike_image_capture.json`.
- [ ] **(clean network)** Run it with a logged-in profile, then in the editor upload a **cover** + one **body image**; let autosave/preview fire.
- [ ] Distill into design doc `§M2`: the get-upload-token call (if any), the upload endpoint + method, the returned `tos-cn-i-…` uri shape, and how it is referenced in `pgc_feed_covers` (cover) and body `<img>` (body image).

### Tasks 2.1+ — authored AFTER 2.0 (capture-gated, NOT pre-written)
> Deliberately not written here: implementing cover/body upload requires Task 2.0's captured contract. Pre-writing concrete code for an uncaptured API would be a guess and violates plan discipline. Once 2.0 lands, author as TDD tasks: (a) in-page image-upload helper `base64 bytes → upload → tos-uri`; (b) `pgc_feed_covers` assembly from the cover tos-uri; (c) body `<img src="tos-uri">` substitution — extend `toutiao_html.py` to accept resolved body-image uris (removes the M1 `ToutiaoBodyError` on image segments); (d) the `/photo/info` resolve call.

---

## Phase 3 — Real publish + manual-confirm (contract known; needs Phase 2 cover)

### Task 3.1: `save=1` publish flip
`build_publish_form` already supports `save=1` (→ `entrance="main"`). When NOT `stop_before_publish` **and** a cover is set (Phase 2), send `save=1`; the global hook adds `_signature` for the publish action (confirmed by the phase-2 capture's final publish request). Map the success response to a real article URL. TDD with the fake page.

### Task 3.2: `stop_before_publish` / manual-confirm wiring
**Mechanics VERIFIED (2026-06-02, see design doc "M2 调查记录 · Phase 1 窗口").** Design `§10`'s "re-issue `save=1` to re-publish the saved draft" is **NOT how manual-confirm currently works**: `manual_confirm_record()` (`server/app/modules/tasks/service.py:190-231`) only stops the browser session, releases the lock, and writes the operator-supplied `outcome` (`succeeded`+`publish_url` / `failed`+`error_message`) — it does **not** call any driver. The DOM driver's `stop_before_publish=True` stops at the live preview (`drivers/toutiao.py:836-897`) and the operator finishes **manually** via noVNC, then reports the outcome.

→ **DECISION REQUIRED before implementing** (in-page driver is pure XHR, no live "确认发布" UI):
- **A (operator-manual, mirrors DOM, ~zero backend change):** `stop_before_publish=True` → `save=0` draft (with cover); leave the page on that draft's edit/preview URL so the operator can publish in noVNC and call manual-confirm. Relies on the live session staying up.
- **B (code re-publish, §10 vision):** extend `manual_confirm_record` to re-invoke the in-page driver and fire `save=1` on the saved `pgc_id`. More robust; touches backend orchestration beyond the driver layer.

Both are gated on Phase 2 (cover) + Phase 0 (clean network); pick A vs B when entering Phase 3.

---

## Phase 4 — Integration + live validation

### Task 4.1: Thread image bytes through the payload
Pass the cover (`payload.cover_asset_path`) and resolved body-image paths into the driver as base64 (per design `§5`), so the in-page upload helper (Phase 2) has the bytes. Reuse `_maybe_resize_for_upload` sizing from `toutiao.py`.

### Task 4.2: Full publish live test
Extend `test_toutiao_inpage_live.py` to a `save=1` round-trip on a clean env (cover + body), asserting a real article URL/`pgc_id`. `@pytest.mark.live`, skipped in CI.

---

## Self-Review

- **Spec coverage:** design `§4/§5` (in-page contract, base64 images) → Phases 2/4; `§6 + M2调查记录` (signing proven, 7050 environmental) → Phase 0 gate; `§10` (manual-confirm) → Phase 3.2; M2-polish login-check note → Phase 1. Cover/body upload → Phase 2.
- **Placeholders:** Phases 0/1/3/4 are concrete. Phase 2.1+ is an explicit, flagged **capture-then-author** boundary (not a hidden TODO) — it is dishonest to pre-write code against an uncaptured API.
- **Sequencing/risk:** Phase 0 gates everything (no point building if production also `7050`s). Phase 3 depends on Phase 2's cover. Phase 1 is independent and can land immediately.
- **DRAFT note:** finalize Phases 2/3 after the Phase 0 gate passes and Task 2.0's capture lands.
