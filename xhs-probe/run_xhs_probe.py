from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from glmv_client import GlmVisionClient, GlmVisionError, load_api_key, load_prompt


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.json"

BLOCKING_TEXT = [
    "验证码",
    "安全验证",
    "滑块",
    "风险提示",
    "账号异常",
    "请先登录",
    "扫码登录",
    "登录后",
]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_run_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Missing config: {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def project_path(value: str | None, fallback: str) -> Path:
    raw = value or fallback
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def ensure_dirs(config: dict[str, Any]) -> None:
    for key, fallback in (
        ("profile_dir", "profiles/xhs"),
        ("screenshot_dir", "screenshots"),
        ("log_dir", "logs"),
    ):
        project_path(config.get(key), fallback).mkdir(parents=True, exist_ok=True)


def redact_text(text: str) -> str:
    text = re.sub(r"Bearer\s+[A-Za-z0-9._\-]+", "Bearer [REDACTED]", text)
    text = re.sub(r"sk-[A-Za-z0-9._\-]{8,}", "sk-[REDACTED]", text)
    return text


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            lowered = key.lower()
            if ("key" in lowered and not lowered.endswith("_loaded")) or "authorization" in lowered:
                clean[key] = "[REDACTED]"
            else:
                clean[key] = sanitize(item)
        return clean
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class RunLogger:
    def __init__(self, config: dict[str, Any], run_id: str) -> None:
        self.run_id = run_id
        self.log_dir = project_path(config.get("log_dir"), "logs")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.run_log = self.log_dir / "run.jsonl"

    def event(self, event: str, **data: Any) -> None:
        record = {
            "ts": now_utc(),
            "run_id": self.run_id,
            "event": event,
            **sanitize(data),
        }
        with self.run_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def json_path(self, prefix: str, tag: str) -> Path:
        return self.log_dir / f"{prefix}_{self.run_id}_{tag}.json"


def screenshot_path(config: dict[str, Any], run_id: str, tag: str) -> Path:
    directory = project_path(config.get("screenshot_dir"), "screenshots")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{run_id}_{tag}.png"


def capture_screenshot(page: Any, config: dict[str, Any], run_id: str, tag: str) -> Path:
    path = screenshot_path(config, run_id, tag)
    page.screenshot(path=str(path), full_page=True)
    return path


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(sanitize(payload), ensure_ascii=False, indent=2), encoding="utf-8")


def default_vision_result() -> dict[str, Any]:
    return {
        "page_state": "unknown",
        "confidence": 0.0,
        "upload_area": {"found": False, "description": "", "approx_position": ""},
        "title_input": {"found": False, "description": "", "approx_position": ""},
        "body_input": {"found": False, "description": "", "approx_position": ""},
        "publish_button": {"found": False, "description": "", "approx_position": ""},
        "risk_warning": {"found": False, "text": ""},
        "next_action_suggestion": "",
    }


def normalize_vision_result(parsed: Any) -> dict[str, Any]:
    result = default_vision_result()
    if not isinstance(parsed, dict):
        return result
    for key in ("page_state", "next_action_suggestion"):
        if isinstance(parsed.get(key), str):
            result[key] = parsed[key]
    if result["page_state"] not in {"xhs_publish_page", "login_required", "captcha_or_verification", "unknown"}:
        result["page_state"] = "unknown"
    try:
        result["confidence"] = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        result["confidence"] = 0.0
    for key in ("upload_area", "title_input", "body_input", "publish_button", "risk_warning"):
        if isinstance(parsed.get(key), dict):
            result[key].update(parsed[key])
        result[key]["found"] = bool(result[key].get("found"))
    return result


def prompt_path(config: dict[str, Any]) -> Path:
    return project_path(config.get("vision_prompt_path"), "vision_prompt.txt")


def analyze_with_provider(
    config: dict[str, Any],
    logger: RunLogger,
    screenshot: Path,
    tag: str,
    provider_key: str,
    result_path: Path,
) -> tuple[dict[str, Any], bool, dict[str, Any]]:
    provider_config = config.get(provider_key, {})
    api_key, key_loaded = load_api_key(provider_config, PROJECT_ROOT)
    logger.event(f"{provider_key}_key_status", **{f"{provider_key}_key_loaded": key_loaded})

    payload: dict[str, Any] = {
        "provider": provider_key,
        f"{provider_key}_key_loaded": key_loaded,
        "screenshot_path": str(screenshot),
    }
    if not key_loaded:
        payload.update({"ok": False, "error": f"{provider_key} API key is not loaded.", "parsed": default_vision_result()})
        return payload["parsed"], False, payload

    try:
        client = GlmVisionClient(provider_config, api_key, provider=provider_key)
        response = client.analyze_image(screenshot, load_prompt(prompt_path(config)))
        parsed = normalize_vision_result(response.get("parsed"))
        payload.update({"ok": True, **response, "parsed": parsed})
        return parsed, True, payload
    except GlmVisionError as exc:
        parsed = default_vision_result()
        payload.update({"ok": False, "error": redact_text(str(exc)), "parsed": parsed})
        logger.event(f"{provider_key}_error", error=str(exc), **{f"{provider_key}_key_loaded": key_loaded})
        return parsed, False, payload


def analyze_with_glm(
    config: dict[str, Any],
    logger: RunLogger,
    screenshot: Path,
    tag: str,
) -> tuple[dict[str, Any], Path, bool]:
    result_path = logger.json_path("glm_result", tag)
    parsed, ok, payload = analyze_with_provider(config, logger, screenshot, tag, "glm", result_path)
    if ok:
        write_json(result_path, payload)
        return parsed, result_path, True

    fallback_config = config.get("agent_vision", {})
    fallback_enabled = bool(fallback_config.get("enabled"))
    fallback_ready = bool(str(fallback_config.get("api_endpoint") or "").strip()) and bool(
        str(fallback_config.get("model") or "").strip()
    )
    if fallback_enabled and fallback_ready:
        fallback_parsed, fallback_ok, fallback_payload = analyze_with_provider(
            config, logger, screenshot, tag, "agent_vision", result_path
        )
        combined_payload = {"ok": fallback_ok, "primary_error": payload, "fallback": fallback_payload}
        write_json(result_path, combined_payload)
        return fallback_parsed, result_path, fallback_ok

    if fallback_enabled and not fallback_ready:
        logger.event("agent_vision_skipped", reason="missing api_endpoint or model")
    write_json(result_path, payload)
    return parsed, result_path, False


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def start_browser(config: dict[str, Any]) -> tuple[Any, Any, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is not installed. Run: pip install -r requirements.txt") from exc

    browser_config = config.get("browser", {})
    profile_dir = project_path(config.get("profile_dir"), "profiles/xhs")
    profile_dir.mkdir(parents=True, exist_ok=True)

    playwright = sync_playwright().start()
    launch_options: dict[str, Any] = {
        "headless": False,
        "viewport": browser_config.get("viewport") or {"width": 1440, "height": 1000},
        "locale": browser_config.get("locale") or "zh-CN",
        "slow_mo": int(browser_config.get("slow_mo_ms") or 0),
        "accept_downloads": True,
    }
    channel = str(browser_config.get("channel") or "").strip()
    if channel:
        launch_options["channel"] = channel

    context = playwright.chromium.launch_persistent_context(str(profile_dir), **launch_options)
    return playwright, context, context.pages[0] if context.pages else context.new_page()


def close_browser(playwright: Any, context: Any) -> None:
    try:
        context.close()
    except Exception:
        pass
    try:
        playwright.stop()
    except Exception:
        pass


def goto_publish(page: Any, config: dict[str, Any]) -> None:
    browser_config = config.get("browser", {})
    page.goto(
        config.get("xhs_publish_url") or "https://creator.xiaohongshu.com/publish/publish",
        wait_until="domcontentloaded",
        timeout=int(browser_config.get("goto_timeout_ms") or 60000),
    )
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(int(browser_config.get("settle_wait_ms") or 5000))
    tab_text = str(config.get("xhs_publish_tab") or "").strip()
    if tab_text:
        try:
            tab = page.get_by_text(tab_text, exact=True).first
            if tab.is_visible(timeout=2000):
                tab.click(timeout=3000)
                page.wait_for_timeout(1500)
        except Exception:
            try:
                clicked = page.evaluate(
                    """
                    (text) => {
                      const wanted = text.trim();
                      const els = Array.from(document.querySelectorAll('button, a, div, span'));
                      const el = els.find((node) => (node.innerText || node.textContent || '').trim() === wanted);
                      if (!el) return false;
                      el.click();
                      return true;
                    }
                    """,
                    tab_text,
                )
                if clicked:
                    page.wait_for_timeout(1500)
            except Exception:
                pass


def wait_for_browser_close(page: Any) -> None:
    try:
        page.wait_for_event("close", timeout=0)
    except Exception:
        pass


def visible_text_found(page: Any, text: str, timeout_ms: int = 700) -> bool:
    try:
        return page.get_by_text(text, exact=False).first.is_visible(timeout=timeout_ms)
    except Exception:
        return False


def detect_blocking_state(page: Any) -> dict[str, Any]:
    for text in BLOCKING_TEXT:
        if visible_text_found(page, text):
            return {"found": True, "text": text}
    return {"found": False, "text": ""}


def dump_elements(page: Any, logger: RunLogger, tag: str) -> Path:
    path = logger.json_path("elements", tag)
    elements = page.evaluate(
        """
        () => Array.from(document.querySelectorAll('button, textarea, input, [contenteditable="true"]'))
          .slice(0, 300)
          .map((el, index) => {
            const rect = el.getBoundingClientRect();
            return {
              index,
              tag: el.tagName.toLowerCase(),
              type: el.getAttribute('type') || '',
              role: el.getAttribute('role') || '',
              aria_label: el.getAttribute('aria-label') || '',
              placeholder: el.getAttribute('placeholder') || el.getAttribute('data-placeholder') || '',
              text: (el.innerText || el.textContent || '').trim().slice(0, 120),
              contenteditable: el.getAttribute('contenteditable') || '',
              visible: rect.width > 0 && rect.height > 0,
              rect: { x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height) }
            };
          })
        """
    )
    write_json(path, {"elements": elements})
    logger.event("elements_dumped", path=str(path))
    return path


def upload_form_visible(page: Any) -> bool:
    try:
        return bool(
            page.evaluate(
                """
                () => Array.from(document.querySelectorAll('input, textarea, [contenteditable="true"]'))
                  .some((el) => /标题|正文|描述|内容|说点什么/.test(
                    [
                      el.getAttribute('placeholder') || '',
                      el.getAttribute('data-placeholder') || '',
                      el.getAttribute('aria-label') || '',
                      el.innerText || '',
                      el.textContent || ''
                    ].join(' ')
                  ) && el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0)
                """
            )
        )
    except Exception:
        return False


def wait_for_upload_progress(page: Any, wait_seconds: int) -> bool:
    deadline = time.time() + max(1, wait_seconds)
    while time.time() < deadline:
        if upload_form_visible(page):
            return True
        page.wait_for_timeout(1000)
    return upload_form_visible(page)


def try_set_file_input(page: Any, image_path: Path, wait_seconds: int) -> bool:
    selectors = [
        "input[type='file'][accept*='image']",
        "input[type='file'][accept*='png']",
        "input[type='file'][accept*='jpg']",
        "input[type='file'][accept*='jpeg']",
        "input[type='file']",
    ]
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = min(locator.count(), 5)
        except Exception:
            continue
        for index in range(count):
            try:
                locator.nth(index).set_input_files(str(image_path), timeout=5000)
                if wait_for_upload_progress(page, wait_seconds):
                    return True
            except Exception:
                continue
    return False


def upload_image(page: Any, image_path: Path, wait_seconds: int) -> bool:
    if try_set_file_input(page, image_path, wait_seconds):
        return True

    candidates = [
        "text=点击上传",
        "text=上传图片",
        "text=上传图文",
        "text=选择图片",
        "text=选择文件",
        "button:has-text('上传')",
        "button:has-text('选择')",
        "[aria-label*='上传']",
    ]
    for selector in candidates[:8]:
        try:
            locator = page.locator(selector).first
            if not locator.is_visible(timeout=1500):
                continue
            with page.expect_file_chooser(timeout=4000) as chooser_info:
                locator.click(timeout=3000)
            chooser_info.value.set_files(str(image_path))
            return wait_for_upload_progress(page, wait_seconds)
        except Exception:
            if try_set_file_input(page, image_path, wait_seconds):
                return True
            continue
    return False


def wait_for_upload(page: Any, wait_seconds: int) -> None:
    deadline = time.time() + max(1, wait_seconds)
    while time.time() < deadline:
        blocking_upload_text = any(visible_text_found(page, text, timeout_ms=250) for text in ("上传中", "处理中"))
        if not blocking_upload_text:
            page.wait_for_timeout(1500)
            return
        page.wait_for_timeout(1000)


def locator_factories(page: Any, field: str) -> list[Callable[[], Any]]:
    if field == "title":
        title_re = re.compile("标题|title", re.I)
        return [
            lambda: page.get_by_placeholder(title_re),
            lambda: page.get_by_role("textbox", name=title_re),
            lambda: page.locator("input[placeholder*='标题']"),
            lambda: page.locator("textarea[placeholder*='标题']"),
            lambda: page.locator("[contenteditable='true'][data-placeholder*='标题']"),
            lambda: page.locator("[contenteditable='true'][aria-label*='标题']"),
        ]
    body_re = re.compile("正文|描述|内容|分享|说点什么|写下|添加", re.I)
    return [
        lambda: page.get_by_placeholder(body_re),
        lambda: page.get_by_role("textbox", name=body_re),
        lambda: page.locator("textarea[placeholder*='正文']"),
        lambda: page.locator("textarea[placeholder*='描述']"),
        lambda: page.locator("[contenteditable='true'][data-placeholder*='正文']"),
        lambda: page.locator("[contenteditable='true'][data-placeholder*='描述']"),
        lambda: page.locator("[contenteditable='true'][aria-label*='正文']"),
        lambda: page.locator("[contenteditable='true'][aria-label*='描述']"),
        lambda: page.locator("[contenteditable='true']"),
    ]


def fill_text_field(page: Any, field: str, value: str) -> bool:
    for make_locator in locator_factories(page, field):
        try:
            locator = make_locator()
            count = min(locator.count(), 5)
        except Exception:
            continue
        for index in range(count):
            target = locator.nth(index)
            try:
                if not target.is_visible(timeout=1000):
                    continue
                target.scroll_into_view_if_needed(timeout=2000)
                target.fill(value, timeout=5000)
                return True
            except Exception:
                try:
                    target.click(timeout=2000)
                    page.keyboard.press("Control+A")
                    page.keyboard.type(value)
                    return True
                except Exception:
                    continue
    return False


def publish_button_visible_dom(page: Any) -> bool:
    candidates = [
        "button:has-text('发布')",
        "[role='button']:has-text('发布')",
        "text=发布",
    ]
    for selector in candidates:
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=1000):
                return True
        except Exception:
            continue
    return False


def final_publish_button_visible(page: Any) -> bool:
    try:
        return bool(
            page.evaluate(
                """
                () => Array.from(document.querySelectorAll('button, [role="button"], div, span'))
                  .some((el) => {
                    const rect = el.getBoundingClientRect();
                    const text = (el.innerText || el.textContent || '').trim();
                    return text === '发布' && rect.width > 0 && rect.height > 0 && !el.disabled;
                  })
                """
            )
        )
    except Exception:
        return False


def click_final_publish_button(page: Any) -> bool:
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception:
        pass
    for make_locator in (
        lambda: page.get_by_role("button", name="发布", exact=True),
        lambda: page.locator("button").filter(has_text=re.compile("^发布$")),
    ):
        try:
            locator = make_locator()
            count = min(locator.count(), 5)
        except Exception:
            continue
        for index in range(count - 1, -1, -1):
            target = locator.nth(index)
            try:
                if not target.is_visible(timeout=1000):
                    continue
                target.scroll_into_view_if_needed(timeout=2000)
                target.click(timeout=5000)
                return True
            except Exception:
                continue
    try:
        box = page.evaluate(
            """
            () => {
              const nodes = Array.from(document.querySelectorAll('button, [role="button"], div, span'))
                .map((node) => {
                  const rect = node.getBoundingClientRect();
                  const text = (node.innerText || node.textContent || '').trim().replace(/\\s+/g, ' ');
                  return {
                    node,
                    text,
                    x: rect.x,
                    y: rect.y,
                    width: rect.width,
                    height: rect.height,
                    disabled: !!node.disabled
                  };
                })
                .filter((item) =>
                  item.text === '发布' &&
                  item.width > 20 &&
                  item.height > 15 &&
                  item.y > window.innerHeight / 2 &&
                  !item.disabled
                )
                .sort((a, b) => b.y - a.y || b.width - a.width);
              const item = nodes[0];
              if (!item) return null;
              item.node.scrollIntoView({ block: 'center', inline: 'center' });
              const rect = item.node.getBoundingClientRect();
              return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2, text: item.text };
            }
            """
        )
        if box:
            page.mouse.click(float(box["x"]), float(box["y"]))
            return True
    except Exception:
        pass

    try:
        viewport = page.viewport_size or page.evaluate("() => ({ width: window.innerWidth, height: window.innerHeight })")
        # Final fallback for XHS' sticky bottom publish control in the fixed 1440x1000 probe viewport.
        page.mouse.click(float(viewport["width"]) * 0.515, float(viewport["height"]) - 45)
        return True
    except Exception:
        return False


def publish_state_hint(page: Any) -> str:
    for text in ("发布成功", "审核中", "发布中", "笔记发布成功", "发布失败", "违规", "验证码", "安全验证"):
        if visible_text_found(page, text, timeout_ms=500):
            return text
    return ""


def dump_publish_candidates(page: Any, logger: RunLogger, tag: str) -> Path:
    path = logger.json_path("publish_candidates", tag)
    candidates = page.evaluate(
        """
        () => Array.from(document.querySelectorAll('button, [role="button"], div, span'))
          .map((node, index) => {
            const rect = node.getBoundingClientRect();
            return {
              index,
              tag: node.tagName.toLowerCase(),
              role: node.getAttribute('role') || '',
              text: (node.innerText || node.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 120),
              visible: rect.width > 0 && rect.height > 0,
              rect: { x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height) }
            };
          })
          .filter((item) => item.visible && item.text.includes('发布'))
        """
    )
    write_json(path, {"candidates": candidates})
    logger.event("publish_candidates_dumped", path=str(path))
    return path


def inspect_summary(parsed: dict[str, Any], screenshot: Path, glm_result: Path) -> dict[str, Any]:
    return {
        "page_state": parsed["page_state"],
        "upload_area_found": bool(parsed["upload_area"]["found"]),
        "title_input_found": bool(parsed["title_input"]["found"]),
        "body_input_found": bool(parsed["body_input"]["found"]),
        "publish_button_found": bool(parsed["publish_button"]["found"]),
        "risk_warning_found": bool(parsed["risk_warning"]["found"]),
        "screenshot_path": str(screenshot),
        "glm_result_path": str(glm_result),
    }


def run_login(args: argparse.Namespace) -> int:
    config = load_config()
    ensure_dirs(config)
    run_id = make_run_id()
    logger = RunLogger(config, run_id)
    logger.event("run_started", command="login")
    playwright = context = page = None
    try:
        playwright, context, page = start_browser(config)
        goto_publish(page, config)
        print("login mode: browser opened with profiles\\xhs. Log in manually, then close the browser window.")
        wait_for_browser_close(page)
        logger.event("login_completed")
        return 0
    except Exception as exc:
        screenshot = ""
        if page is not None:
            try:
                screenshot = str(capture_screenshot(page, config, run_id, "login_error"))
            except Exception:
                pass
        logger.event("error", command="login", error=str(exc), traceback=traceback.format_exc(), screenshot_path=screenshot)
        print_json({"ok": False, "error": redact_text(str(exc)), "screenshot_path": screenshot})
        return 1
    finally:
        if playwright is not None and context is not None:
            close_browser(playwright, context)


def run_inspect(args: argparse.Namespace) -> int:
    config = load_config()
    ensure_dirs(config)
    run_id = make_run_id()
    logger = RunLogger(config, run_id)
    logger.event("run_started", command="inspect")
    playwright = context = page = None
    try:
        playwright, context, page = start_browser(config)
        goto_publish(page, config)
        if args.debug_elements:
            dump_elements(page, logger, "inspect")
        screenshot = capture_screenshot(page, config, run_id, "inspect")
        parsed, result_path, _ok = analyze_with_glm(config, logger, screenshot, "inspect")
        summary = inspect_summary(parsed, screenshot, result_path)
        logger.event("inspect_completed", **summary)
        print_json(summary)
        return 0
    except Exception as exc:
        screenshot = ""
        if page is not None:
            try:
                screenshot = str(capture_screenshot(page, config, run_id, "inspect_error"))
            except Exception:
                pass
        result_path = logger.json_path("glm_result", "inspect_error")
        write_json(result_path, {"ok": False, "error": redact_text(str(exc)), "parsed": default_vision_result()})
        summary = inspect_summary(default_vision_result(), Path(screenshot), result_path)
        logger.event("error", command="inspect", error=str(exc), traceback=traceback.format_exc(), screenshot_path=screenshot)
        summary["screenshot_path"] = screenshot
        print_json(summary)
        return 1
    finally:
        if playwright is not None and context is not None:
            close_browser(playwright, context)


def run_dry_run(args: argparse.Namespace) -> int:
    config = load_config()
    ensure_dirs(config)
    run_id = make_run_id()
    logger = RunLogger(config, run_id)
    logger.event("run_started", command="dry-run")

    image_path = Path(args.image).expanduser()
    if not image_path.exists() or not image_path.is_file():
        summary = {
            "dry_run_completed": False,
            "image_uploaded": False,
            "title_filled": False,
            "body_filled": False,
            "publish_button_visible": False,
            "risk_warning_found": False,
            "screenshot_path": "",
            "glm_result_path": "",
        }
        print_json(summary)
        logger.event("dry_run_completed", error="image file not found", **summary)
        return 2

    defaults = config.get("dry_run_defaults", {})
    title = args.title if args.title is not None else str(defaults.get("title") or "1234")
    body = args.body if args.body is not None else str(defaults.get("body") or "12345")
    wait_seconds = int(defaults.get("upload_wait_seconds") or 20)

    playwright = context = page = None
    keep_open = False
    try:
        playwright, context, page = start_browser(config)
        goto_publish(page, config)
        if args.debug_elements:
            dump_elements(page, logger, "dry_run_initial")

        blocking = detect_blocking_state(page)
        if blocking["found"]:
            screenshot = capture_screenshot(page, config, run_id, "dry_run_blocked")
            parsed, result_path, _ok = analyze_with_glm(config, logger, screenshot, "dry_run_blocked")
            risk_found = True
            summary = {
                "dry_run_completed": False,
                "image_uploaded": False,
                "title_filled": False,
                "body_filled": False,
                "publish_button_visible": publish_button_visible_dom(page),
                "risk_warning_found": risk_found,
                "screenshot_path": str(screenshot),
                "glm_result_path": str(result_path),
            }
            logger.event("dry_run_blocked", blocking_text=blocking["text"], **summary)
            print_json(summary)
            if not args.close_after:
                print("dry-run mode: stopped before publish. Close the browser window when done.")
                wait_for_browser_close(page)
            return 1

        image_uploaded = upload_image(page, image_path, wait_seconds)
        if not image_uploaded:
            screenshot = capture_screenshot(page, config, run_id, "dry_run_upload_not_found")
            parsed, result_path, _ok = analyze_with_glm(config, logger, screenshot, "dry_run_upload_not_found")
            summary = {
                "dry_run_completed": False,
                "image_uploaded": False,
                "title_filled": False,
                "body_filled": False,
                "publish_button_visible": bool(parsed["publish_button"]["found"]) or publish_button_visible_dom(page),
                "risk_warning_found": bool(parsed["risk_warning"]["found"])
                or parsed["page_state"] in {"login_required", "captcha_or_verification"},
                "screenshot_path": str(screenshot),
                "glm_result_path": str(result_path),
            }
            logger.event("dry_run_completed", **summary)
            print_json(summary)
            if not args.close_after:
                print("dry-run mode: stopped before publish. Close the browser window when done.")
                wait_for_browser_close(page)
            return 1

        title_filled = fill_text_field(page, "title", title)
        body_filled = fill_text_field(page, "body", body)
        page.wait_for_timeout(1500)
        if args.debug_elements:
            dump_elements(page, logger, "dry_run_final")

        final_screenshot = capture_screenshot(page, config, run_id, "dry_run_final")
        parsed, result_path, _ok = analyze_with_glm(config, logger, final_screenshot, "dry_run_final")
        blocking_after = detect_blocking_state(page)
        publish_visible = publish_button_visible_dom(page) or bool(parsed["publish_button"]["found"])
        risk_found = (
            blocking_after["found"]
            or bool(parsed["risk_warning"]["found"])
            or parsed["page_state"] in {"login_required", "captcha_or_verification"}
        )
        dry_run_completed = bool(image_uploaded and title_filled and body_filled and publish_visible and not risk_found)
        summary = {
            "dry_run_completed": dry_run_completed,
            "image_uploaded": image_uploaded,
            "title_filled": title_filled,
            "body_filled": body_filled,
            "publish_button_visible": publish_visible,
            "risk_warning_found": risk_found,
            "screenshot_path": str(final_screenshot),
            "glm_result_path": str(result_path),
        }
        logger.event("dry_run_completed", **summary)
        print_json(summary)
        keep_open = not args.close_after
        if keep_open:
            print("dry-run mode: browser is stopped before publish. Close the browser window when done.")
            wait_for_browser_close(page)
        return 0 if dry_run_completed else 1
    except Exception as exc:
        screenshot = ""
        if page is not None:
            try:
                screenshot = str(capture_screenshot(page, config, run_id, "dry_run_error"))
            except Exception:
                pass
        result_path = logger.json_path("glm_result", "dry_run_error")
        write_json(result_path, {"ok": False, "error": redact_text(str(exc)), "parsed": default_vision_result()})
        summary = {
            "dry_run_completed": False,
            "image_uploaded": False,
            "title_filled": False,
            "body_filled": False,
            "publish_button_visible": False,
            "risk_warning_found": False,
            "screenshot_path": screenshot,
            "glm_result_path": str(result_path),
        }
        logger.event("error", command="dry-run", error=str(exc), traceback=traceback.format_exc(), **summary)
        print_json(summary)
        return 1
    finally:
        if playwright is not None and context is not None:
            close_browser(playwright, context)


def run_publish_once(args: argparse.Namespace) -> int:
    config = load_config()
    ensure_dirs(config)
    run_id = make_run_id()
    logger = RunLogger(config, run_id)
    logger.event("run_started", command="publish-once")

    image_path = Path(args.image).expanduser()
    if not image_path.exists() or not image_path.is_file():
        summary = {"publish_completed": False, "error": "image file not found", "screenshot_path": ""}
        print_json(summary)
        logger.event("publish_completed", **summary)
        return 2

    body = args.body or ""
    if args.body_file:
        body_file = Path(args.body_file).expanduser()
        body = body_file.read_text(encoding="utf-8")

    defaults = config.get("dry_run_defaults", {})
    title = args.title if args.title is not None else str(defaults.get("title") or "1234")
    body = body if body else str(defaults.get("body") or "12345")
    wait_seconds = int(defaults.get("upload_wait_seconds") or 20)

    playwright = context = page = None
    try:
        playwright, context, page = start_browser(config)
        goto_publish(page, config)

        blocking = detect_blocking_state(page)
        if blocking["found"]:
            screenshot = capture_screenshot(page, config, run_id, "publish_blocked")
            summary = {
                "publish_completed": False,
                "publish_clicked": False,
                "blocking_text": blocking["text"],
                "screenshot_path": str(screenshot),
            }
            logger.event("publish_blocked", **summary)
            print_json(summary)
            return 1

        image_uploaded = upload_image(page, image_path, wait_seconds)
        title_filled = fill_text_field(page, "title", title) if image_uploaded else False
        body_filled = fill_text_field(page, "body", body) if image_uploaded else False
        page.wait_for_timeout(1500)

        pre_screenshot = capture_screenshot(page, config, run_id, "publish_before_click")
        parsed, glm_result, _ok = analyze_with_glm(config, logger, pre_screenshot, "publish_before_click")
        blocking_after = detect_blocking_state(page)
        risk_found = (
            blocking_after["found"]
            or bool(parsed["risk_warning"]["found"])
            or parsed["page_state"] in {"login_required", "captcha_or_verification"}
        )
        publish_visible = final_publish_button_visible(page) or bool(parsed["publish_button"]["found"])
        ready = bool(image_uploaded and title_filled and body_filled and publish_visible and not risk_found)

        if not ready:
            summary = {
                "publish_completed": False,
                "publish_clicked": False,
                "image_uploaded": image_uploaded,
                "title_filled": title_filled,
                "body_filled": body_filled,
                "publish_button_visible": publish_visible,
                "risk_warning_found": risk_found,
                "screenshot_path": str(pre_screenshot),
                "glm_result_path": str(glm_result),
            }
            logger.event("publish_precheck_failed", **summary)
            print_json(summary)
            return 1

        candidates_path = dump_publish_candidates(page, logger, "publish_before_click")
        publish_clicked = click_final_publish_button(page)
        page.wait_for_timeout(int(args.after_click_wait_ms))
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        after_screenshot = capture_screenshot(page, config, run_id, "publish_after_click")
        after_parsed, after_glm_result, _after_ok = analyze_with_glm(config, logger, after_screenshot, "publish_after_click")
        hint = publish_state_hint(page)
        still_on_publish_form = bool(
            after_parsed["page_state"] == "xhs_publish_page" and after_parsed["publish_button"]["found"]
        )
        completed = bool(
            publish_clicked
            and hint not in {"发布失败", "违规", "验证码", "安全验证"}
            and not still_on_publish_form
        )
        summary = {
            "publish_completed": completed,
            "publish_clicked": publish_clicked,
            "post_click_hint": hint,
            "image_uploaded": image_uploaded,
            "title_filled": title_filled,
            "body_filled": body_filled,
            "risk_warning_found": bool(detect_blocking_state(page)["found"]),
            "before_screenshot_path": str(pre_screenshot),
            "after_screenshot_path": str(after_screenshot),
            "glm_result_path": str(glm_result),
            "after_glm_result_path": str(after_glm_result),
            "publish_candidates_path": str(candidates_path),
        }
        logger.event("publish_completed", **summary)
        print_json(summary)
        if not args.close_after:
            print("publish-once mode: close the browser window when done.")
            wait_for_browser_close(page)
        return 0 if completed else 1
    except Exception as exc:
        screenshot = ""
        if page is not None:
            try:
                screenshot = str(capture_screenshot(page, config, run_id, "publish_error"))
            except Exception:
                pass
        summary = {
            "publish_completed": False,
            "publish_clicked": False,
            "error": redact_text(str(exc)),
            "screenshot_path": screenshot,
        }
        logger.event("error", command="publish-once", error=str(exc), traceback=traceback.format_exc(), **summary)
        print_json(summary)
        return 1
    finally:
        if playwright is not None and context is not None:
            close_browser(playwright, context)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Independent XHS UI vision publisher probe.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    login = subparsers.add_parser("login", help="Open XHS creator page with persistent local profile.")
    login.set_defaults(func=run_login)

    inspect = subparsers.add_parser("inspect", help="Screenshot the publish page and ask GLM-V to inspect it.")
    inspect.add_argument("--debug-elements", action="store_true", help="Write safe DOM element metadata to logs.")
    inspect.set_defaults(func=run_inspect)

    dry = subparsers.add_parser("dry-run", help="Upload image, fill fields, stop before publish.")
    dry.add_argument("--image", required=True, help="Image path to upload.")
    dry.add_argument("--title", default=None, help="Title text. Default comes from config.json.")
    dry.add_argument("--body", default=None, help="Body text. Default comes from config.json.")
    dry.add_argument("--debug-elements", action="store_true", help="Write safe DOM element metadata to logs.")
    dry.add_argument("--close-after", action="store_true", help="Close browser after screenshots; for automated checks.")
    dry.set_defaults(func=run_dry_run)

    pub = subparsers.add_parser("publish-once", help="Upload image, fill fields, and click final publish once.")
    pub.add_argument("--image", required=True, help="Image path to upload.")
    pub.add_argument("--title", required=True, help="Title text.")
    pub.add_argument("--body", default="", help="Body text.")
    pub.add_argument("--body-file", default="", help="UTF-8 file containing body text.")
    pub.add_argument("--after-click-wait-ms", type=int, default=15000, help="Wait time after clicking publish.")
    pub.add_argument("--close-after", action="store_true", help="Close browser after screenshots.")
    pub.set_defaults(func=run_publish_once)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
