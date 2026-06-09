from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

TOUTIAO_PUBLISH_URL = "https://mp.toutiao.com/profile_v4/graphic/publish"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Open Toutiao publish page in a persistent Playwright browser, "
            "wait for a manually triggered body-image file chooser, then upload a test image."
        )
    )
    parser.add_argument("--account-key", default="spike", help="Isolated browser profile key.")
    parser.add_argument(
        "--data-dir", default=None, help="Override GEO_DATA_DIR for browser state/log output."
    )
    parser.add_argument(
        "--image", default="scripts/test_cover.png", help="Image path to upload during the spike."
    )
    parser.add_argument(
        "--channel",
        default="chrome",
        help="Playwright browser channel, e.g. chrome/msedge/chromium.",
    )
    parser.add_argument("--executable-path", default=None, help="Explicit browser executable path.")
    parser.add_argument(
        "--wait-seconds", type=int, default=300, help="Seconds to wait for a file chooser."
    )
    parser.add_argument(
        "--scan-only", action="store_true", help="Only open the page and dump upload candidates."
    )
    parser.add_argument(
        "--pause",
        action="store_true",
        help="Call page.pause() before scanning for Playwright Inspector.",
    )
    parser.add_argument(
        "--auto-click-toolbar-image",
        action="store_true",
        help="Wait for Toutiao's body editor image toolbar button and click it before falling back to manual waiting.",
    )
    return parser.parse_args()


def import_app_helpers(data_dir: str | None):
    if data_dir:
        os.environ["GEO_DATA_DIR"] = str(Path(data_dir).resolve())

    from server.app.core.config import get_settings

    get_settings.cache_clear()

    from server.app.core.paths import ensure_data_dirs, get_data_dir
    from server.app.modules.accounts import launch_options, profile_dir_for_key, state_path_for_key

    return ensure_data_dirs, get_data_dir, launch_options, profile_dir_for_key, state_path_for_key


def editor_image_state(page: Any) -> dict[str, Any]:
    return page.evaluate(
        """
        () => {
          const editables = Array.from(document.querySelectorAll("[contenteditable='true']"));
          const images = editables.flatMap((node, editableIndex) =>
            Array.from(node.querySelectorAll("img")).map((img, imageIndex) => ({
              editableIndex,
              imageIndex,
              src: String(img.currentSrc || img.src || img.getAttribute("src") || "").slice(0, 240),
              complete: Boolean(img.complete),
              naturalWidth: Number(img.naturalWidth || 0),
              naturalHeight: Number(img.naturalHeight || 0),
              rect: (() => {
                const r = img.getBoundingClientRect();
                return { x: r.x, y: r.y, width: r.width, height: r.height };
              })(),
            }))
          );
          return {
            editableCount: editables.length,
            imageCount: images.length,
            images,
          };
        }
        """
    )


def upload_candidates(page: Any) -> dict[str, Any]:
    return page.evaluate(
        """
        () => {
          const visible = (node) => {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.display !== "none" && style.visibility !== "hidden" &&
              rect.width > 0 && rect.height > 0;
          };
          const compact = (value) => String(value || "").replace(/\\s+/g, " ").trim().slice(0, 180);
          const cssPath = (node) => {
            if (!node || !node.tagName) return "";
            const parts = [];
            let current = node;
            while (current && current.nodeType === Node.ELEMENT_NODE && parts.length < 6) {
              let part = current.tagName.toLowerCase();
              if (current.id) {
                part += "#" + current.id;
                parts.unshift(part);
                break;
              }
              const classes = Array.from(current.classList || []).slice(0, 3);
              if (classes.length) part += "." + classes.join(".");
              const parent = current.parentElement;
              if (parent) {
                const siblings = Array.from(parent.children).filter((item) => item.tagName === current.tagName);
                if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(current) + 1})`;
              }
              parts.unshift(part);
              current = parent;
            }
            return parts.join(" > ");
          };
          const inputFiles = Array.from(document.querySelectorAll("input[type='file']")).map((node, index) => {
            const rect = node.getBoundingClientRect();
            const parent = node.closest("div, label, form, section, article, body");
            return {
              index,
              selector: cssPath(node),
              accept: node.getAttribute("accept"),
              multiple: Boolean(node.multiple),
              visible: visible(node),
              disabled: Boolean(node.disabled),
              rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
              nearbyText: compact(parent ? parent.innerText : ""),
            };
          });
          const re = /图片|图像|上传|插入|素材|媒体|本地|image|upload|media/i;
          const clickableNodes = Array.from(document.querySelectorAll(
            "button, [role='button'], label, a, span, div, i, svg"
          ))
            .filter((node) => {
              const haystack = [
                node.innerText,
                node.getAttribute("aria-label"),
                node.getAttribute("title"),
                node.getAttribute("class"),
                node.getAttribute("data-testid"),
              ].join(" ");
              return re.test(haystack) && visible(node);
            })
            .slice(0, 80)
            .map((node, index) => {
              const rect = node.getBoundingClientRect();
              return {
                index,
                selector: cssPath(node),
                tag: node.tagName.toLowerCase(),
                text: compact(node.innerText),
                ariaLabel: compact(node.getAttribute("aria-label")),
                title: compact(node.getAttribute("title")),
                className: compact(node.getAttribute("class")),
                rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
              };
            });
          return {
            url: location.href,
            title: document.title,
            inputFiles,
            clickableNodes,
            bodyTextHint: compact(document.body ? document.body.innerText : ""),
          };
        }
        """
    )


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def click_and_capture_file_chooser(page: Any, locator: Any, timeout_ms: int) -> Any | None:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    try:
        with page.expect_file_chooser(timeout=timeout_ms) as chooser_info:
            locator.click(timeout=timeout_ms)
        return chooser_info.value
    except PlaywrightTimeoutError:
        return None


def auto_capture_body_image_chooser(page: Any, wait_seconds: int) -> Any | None:
    selectors = [
        "div.syl-toolbar-tool.image.static",
        ".syl-toolbar-tool.image",
        "[class*='syl-toolbar-tool'][class*='image']",
    ]
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                locator.wait_for(state="visible", timeout=1500)
            except Exception:
                continue

            print(f"Auto-clicking body image toolbar candidate: {selector}")
            chooser = click_and_capture_file_chooser(page, locator, timeout_ms=5000)
            if chooser is not None:
                return chooser

            # 有些编辑器会先打开弹层，继续尝试可能的本地上传入口。
            page.wait_for_timeout(800)
            menu_candidates = [
                page.get_by_text(re.compile("本地上传|上传图片|选择图片|图片上传")).first,
                page.locator("text=/本地上传|上传图片|选择图片|图片上传/").first,
                page.locator("input[type='file']").first,
            ]
            for candidate in menu_candidates:
                try:
                    candidate.wait_for(state="attached", timeout=1500)
                except Exception:
                    continue
                print("Trying upload menu candidate after toolbar click.")
                chooser = click_and_capture_file_chooser(page, candidate, timeout_ms=5000)
                if chooser is not None:
                    return chooser

                try:
                    if candidate.evaluate(
                        "node => node.tagName && node.tagName.toLowerCase() === 'input'"
                    ):
                        return candidate
                except Exception:
                    pass
        page.wait_for_timeout(1000)
    return None


def confirm_image_drawer(page: Any) -> bool:
    """上传后点击头条正文图片抽屉中的确认按钮。"""
    candidates = [
        ".mp-ic-img-drawer button:has-text('确定')",
        ".byte-drawer button:has-text('确定')",
        "button:has-text('确定')",
    ]
    for selector in candidates:
        locator = page.locator(selector).last
        try:
            locator.wait_for(state="visible", timeout=10000)
            locator.click(timeout=5000)
            return True
        except Exception:
            continue
    return False


def main() -> int:
    args = parse_args()
    image_path = Path(args.image).resolve()
    if not image_path.is_file():
        print(f"Image not found: {image_path}", file=sys.stderr)
        return 2

    ensure_data_dirs, get_data_dir, launch_options, profile_dir_for_key, state_path_for_key = (
        import_app_helpers(args.data_dir)
    )
    data_dir = ensure_data_dirs()
    logs_dir = data_dir / "logs"
    run_id = time.strftime("%Y%m%d-%H%M%S")

    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    print(f"Data dir: {data_dir}")
    print(f"Profile: {profile_dir_for_key(args.account_key)}")
    print(f"Test image: {image_path}")
    print("Opening Toutiao publish page. Log in manually if needed.")

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir_for_key(args.account_key)),
            **launch_options(args.channel, args.executable_path),
        )
        context.set_default_timeout(8000)
        page = context.new_page()
        page.goto(TOUTIAO_PUBLISH_URL, wait_until="domcontentloaded", timeout=60000)

        if args.pause:
            page.pause()

        time.sleep(2)
        before = editor_image_state(page)
        candidates = upload_candidates(page)
        write_json(logs_dir / f"toutiao-body-image-candidates-{run_id}.json", candidates)
        page.screenshot(
            path=str(logs_dir / f"toutiao-body-image-before-{run_id}.png"), full_page=True
        )

        print(f"Current URL: {page.url}")
        print(
            f"Editable count: {before['editableCount']}; body image count: {before['imageCount']}"
        )
        print(
            f"Found {len(candidates['inputFiles'])} file inputs and {len(candidates['clickableNodes'])} clickable candidates."
        )
        for item in candidates["inputFiles"][:20]:
            print(
                "FILE",
                item["index"],
                "visible=",
                item["visible"],
                "accept=",
                item["accept"],
                "text=",
                item["nearbyText"][:80],
            )
        for item in candidates["clickableNodes"][:20]:
            label = item["text"] or item["ariaLabel"] or item["title"] or item["className"]
            print("CLICK", item["index"], item["tag"], label[:100])

        if args.scan_only:
            print("Scan-only mode complete. Browser remains open for 30 seconds.")
            page.wait_for_timeout(30000)
            context.storage_state(path=str(state_path_for_key(args.account_key)))
            context.close()
            return 0

        print("")
        print("Manual step:")
        print("1. In the opened browser, finish login if needed.")
        print("2. Navigate to the article body editor.")
        if args.auto_click_toolbar_image:
            print("3. Script will auto-click the body editor image toolbar button when it appears.")
            chooser = auto_capture_body_image_chooser(page, args.wait_seconds)
        else:
            print("3. Click the BODY image upload entry yourself.")
            print(
                "4. Do not choose a local file manually; this script is waiting for the file chooser."
            )
            chooser = None

        if chooser is None:
            try:
                with page.expect_file_chooser(timeout=args.wait_seconds * 1000) as chooser_info:
                    pass
                chooser = chooser_info.value
            except PlaywrightTimeoutError:
                chooser = None

        if chooser is None:
            candidates = upload_candidates(page)
            write_json(
                logs_dir / f"toutiao-body-image-timeout-candidates-{run_id}.json", candidates
            )
            page.screenshot(
                path=str(logs_dir / f"toutiao-body-image-timeout-{run_id}.png"), full_page=True
            )
            print(f"Timed out waiting for file chooser after {args.wait_seconds}s.")
            print(f"Wrote candidate dump to: {logs_dir}")
            context.storage_state(path=str(state_path_for_key(args.account_key)))
            context.close()
            return 1

        if hasattr(chooser, "is_multiple"):
            print(f"File chooser captured. multiple={chooser.is_multiple()}; setting test image.")
            chooser.set_files(str(image_path))
        else:
            print("File input captured directly; setting test image.")
            chooser.set_input_files(str(image_path))
        page.wait_for_timeout(3000)

        confirmed = confirm_image_drawer(page)
        if confirmed:
            print("Clicked body image drawer confirm button.")
        else:
            print("Could not find body image drawer confirm button; leaving drawer open.")

        deadline = time.monotonic() + 60
        after = editor_image_state(page)
        while time.monotonic() < deadline:
            after = editor_image_state(page)
            if after["imageCount"] > before["imageCount"]:
                break
            page.wait_for_timeout(1500)

        candidates_after = upload_candidates(page)
        write_json(
            logs_dir / f"toutiao-body-image-result-{run_id}.json",
            {
                "before": before,
                "after": after,
                "candidatesAfter": candidates_after,
            },
        )
        page.screenshot(
            path=str(logs_dir / f"toutiao-body-image-after-{run_id}.png"), full_page=True
        )
        context.storage_state(path=str(state_path_for_key(args.account_key)))

        print(
            f"After upload: editable count={after['editableCount']}; body image count={after['imageCount']}"
        )
        if after["imageCount"] > before["imageCount"]:
            print("SUCCESS: body editor image count increased.")
            result = 0
        else:
            print(
                "UNKNOWN: body editor image count did not increase. Check screenshots/result JSON."
            )
            result = 1
        print(f"Artifacts written to: {logs_dir}")
        print("Browser remains open for 20 seconds for visual inspection.")
        page.wait_for_timeout(20000)
        context.close()
        return result


if __name__ == "__main__":
    raise SystemExit(main())
