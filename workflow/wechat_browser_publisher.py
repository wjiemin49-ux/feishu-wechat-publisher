from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


WECHAT_HOME_URL = "https://mp.weixin.qq.com/"
PUBLISH_SUCCESS_PATTERNS = ("发表成功", "发布成功", "群发成功", "已发表", "已发布", "已群发")
DRAFT_SUCCESS_PATTERNS = ("保存成功", "已保存")
STORAGE_STATE_NAME = "storage_state.json"
SECRET_RE = re.compile(
    r"(?i)\b(access[_-]?token|api[_-]?key|authorization|cookie|secret|webhook|session)\b"
    r"(\s*[:=]\s*)([^\s,;\"'}]+)"
)
URL_TOKEN_RE = re.compile(r"(?i)([?&](?:token|access_token|key|secret|signature|code)=)[^&#\s]+")


def redact_sensitive_text(value: Any) -> str:
    text = str(value or "")
    text = SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
    return URL_TOKEN_RE.sub(lambda m: f"{m.group(1)}[REDACTED]", text)


def sanitize_output(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): sanitize_output(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_output(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(sanitize_output(payload), ensure_ascii=False), flush=True)


def compact(value: Any, limit: int = 800) -> str:
    text = redact_sensitive_text(value).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit].rstrip()


def normalized_text(value: str) -> str:
    return re.sub(r"\s+", "", (value or "").replace("\u00a0", ""))


def common_chrome_paths() -> list[Path]:
    candidates = [
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
    ]
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(Path(local_appdata) / "Google/Chrome/Application/chrome.exe")
    candidates.extend(
        [
            Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
            Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        ]
    )
    return candidates


def find_chrome(configured: str | None = None) -> str | None:
    if configured:
        path = Path(configured)
        if path.exists():
            return str(path)
    env_path = os.environ.get("WECHAT_BROWSER_CHROME_PATH")
    if env_path and Path(env_path).exists():
        return env_path
    for path in common_chrome_paths():
        if path.exists():
            return str(path)
    return None


def compress_title(title: str, max_len: int = 20) -> str:
    title = " ".join((title or "").split())
    if len(title) <= max_len:
        return title
    for prefix in ("如何", "为什么", "什么是", "怎样", "怎么", "关于"):
        if title.startswith(prefix) and len(title) > max_len:
            title = title[len(prefix) :]
            if len(title) <= max_len:
                return title
    for token in ("的", "了", "在", "是", "和", "与", "以及", "但是", "因为", "所以", "——"):
        if len(title) <= max_len:
            break
        title = title.replace(token, "")
    return title[:max_len]


def compress_content(content: str, max_len: int = 1000) -> str:
    content = (content or "").strip()
    if len(content) <= max_len:
        return content
    lines: list[str] = []
    total = 0
    for line in content.splitlines():
        if total + len(line) + 1 > max_len:
            remain = max_len - total - 1
            if remain > 20:
                lines.append(line[: remain - 3] + "...")
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines).strip()


def visible_text(page: Any) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""


def is_logged_in(page: Any) -> bool:
    if "/cgi-bin/home" in page.url:
        return True
    try:
        if page.locator(".new-creation__menu-title").count() > 0:
            return True
    except Exception:
        pass
    body = visible_text(page)
    return "新的创作" in body or "草稿箱" in body or "发表记录" in body


def wait_for_login(page: Any, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if is_logged_in(page):
            return
        time.sleep(2)
    raise RuntimeError("公众号后台未登录或登录超时，请在打开的 Chrome 里扫码登录后重试。")


def menu_texts(page: Any) -> list[str]:
    try:
        values = page.evaluate(
            """
            () => Array.from(document.querySelectorAll('.new-creation__menu-title'))
              .map(node => (node.textContent || '').trim())
              .filter(Boolean)
            """
        )
        return [str(value) for value in values]
    except Exception:
        return []


def click_text_menu(page: Any, labels: tuple[str, ...]) -> None:
    script = """
    (labels) => {
      const titles = Array.from(document.querySelectorAll('.new-creation__menu-title'));
      for (const title of titles) {
        const text = (title.textContent || '').trim();
        if (labels.includes(text)) {
          const item = title.closest('.new-creation__menu-item') || title;
          item.scrollIntoView({ block: 'center' });
          item.click();
          return { ok: true, text };
        }
      }
      return { ok: false, texts: titles.map(t => (t.textContent || '').trim()) };
    }
    """
    result = page.evaluate(script, list(labels))
    if not result.get("ok"):
        raise RuntimeError(f"没有找到公众号后台入口：{','.join(labels)}；当前菜单：{result.get('texts')}")


def check_ready(page: Any) -> dict[str, Any]:
    logged_in = is_logged_in(page)
    menus = menu_texts(page) if logged_in else []
    return {
        "ok": logged_in and any(label in menus for label in ("贴图", "图文")),
        "action": "check",
        "logged_in": logged_in,
        "url": page.url,
        "menu_texts": menus,
        "has_image_text_entry": any(label in menus for label in ("贴图", "图文")),
    }


def save_storage_state(context: Any, profile_dir: Path) -> str:
    state_path = profile_dir / STORAGE_STATE_NAME
    context.storage_state(path=str(state_path))
    return str(state_path)


def open_editor(context: Any, home_page: Any) -> Any:
    old_pages = set(context.pages)
    try:
        with context.expect_page(timeout=8000) as page_info:
            click_text_menu(home_page, ("贴图", "图文"))
        editor = page_info.value
        editor.wait_for_load_state("domcontentloaded", timeout=30000)
        return editor
    except PlaywrightTimeoutError:
        for page in context.pages:
            if page not in old_pages and "mp.weixin.qq.com" in page.url:
                page.wait_for_load_state("domcontentloaded", timeout=30000)
                return page
        home_page.wait_for_load_state("domcontentloaded", timeout=30000)
        return home_page


def upload_images(page: Any, images: list[str]) -> str:
    selectors = [
        ".js_upload_btn_container input[type=file]",
        "input[type=file][multiple][accept*='image']",
        "input[type=file][accept*='image']",
        "input[type=file][multiple]",
        "input[type=file]",
    ]
    for selector in selectors:
        locator = page.locator(selector)
        if locator.count() > 0:
            locator.first.set_input_files(images)
            return selector

    upload_texts = ("上传图片", "上传", "选择图片", "添加图片")
    for text in upload_texts:
        try:
            with page.expect_file_chooser(timeout=5000) as chooser_info:
                page.get_by_text(text, exact=False).first.click()
            chooser_info.value.set_files(images)
            return f"text:{text}"
        except Exception:
            continue
    raise RuntimeError("没有找到公众号贴图上传入口。")


def uploaded_image_count(page: Any) -> int:
    return int(
        page.evaluate(
            """
            () => {
              const selectorImages = Array.from(document.querySelectorAll(
                '.image-selector__preview-center-img img, .image-selector img, img[src*="mmbiz.qpic.cn"]'
              ));
              const urls = new Set();
              for (const img of selectorImages) {
                const src = img.currentSrc || img.src || '';
                if (!src.includes('mmbiz.qpic.cn')) continue;
                if ((img.naturalWidth || img.width || 0) < 80) continue;
                if ((img.naturalHeight || img.height || 0) < 80) continue;
                urls.add(src.split('&token=')[0]);
              }
              const styleNodes = Array.from(document.querySelectorAll('[style*="mmbiz.qpic.cn"]'));
              for (const node of styleNodes) {
                const style = node.getAttribute('style') || '';
                const matches = style.match(/https:\\/\\/mmbiz\\.qpic\\.cn[^"')]+/g) || [];
                for (const url of matches) urls.add(url);
              }
              return urls.size;
            }
            """
        )
    )


def wait_upload_finished(page: Any, expected: int) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        status = page.evaluate(
            """
            () => ({
              uploaded: document.querySelectorAll(
                '.weui-desktop-upload__thumb, .pic_item, [class*=upload_thumb], [class*="pic_item"], [class*="upload__thumb"]'
              ).length,
              modern_uploaded: (() => {
                const urls = new Set();
                const imgs = Array.from(document.querySelectorAll(
                  '.image-selector__preview-center-img img, .image-selector img, img[src*="mmbiz.qpic.cn"]'
                ));
                for (const img of imgs) {
                  const src = img.currentSrc || img.src || '';
                  if (!src.includes('mmbiz.qpic.cn')) continue;
                  if ((img.naturalWidth || img.width || 0) < 80) continue;
                  if ((img.naturalHeight || img.height || 0) < 80) continue;
                  urls.add(src.split('&token=')[0]);
                }
                const styleNodes = Array.from(document.querySelectorAll('[style*="mmbiz.qpic.cn"]'));
                for (const node of styleNodes) {
                  const style = node.getAttribute('style') || '';
                  const matches = style.match(/https:\\/\\/mmbiz\\.qpic\\.cn[^"')]+/g) || [];
                  for (const url of matches) urls.add(url);
                }
                return urls.size;
              })(),
              loading: document.querySelectorAll('[class*="upload_loading"], [class*="uploading"], .weui-desktop-upload__loading').length
            })
            """
        )
        uploaded = max(status.get("uploaded", 0), status.get("modern_uploaded", 0))
        if uploaded >= expected and status.get("loading", 0) == 0:
            return
        time.sleep(2)
    raise RuntimeError("公众号图片上传超时。")


def inspect_editor(page: Any, profile_dir: Path) -> dict[str, Any]:
    output_dir = profile_dir / "diagnostics"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    screenshot_path = output_dir / f"wechat-editor-{stamp}.png"
    page.screenshot(path=str(screenshot_path), full_page=True)
    details = page.evaluate(
        """
        () => {
          const textOf = node => (node.textContent || node.innerText || '').trim().replace(/\\s+/g, ' ');
          const attr = (node, name) => node.getAttribute(name) || '';
          return {
            url: location.href,
            title: document.title,
            body_text: (document.body.innerText || '').slice(0, 2500),
            file_inputs: Array.from(document.querySelectorAll('input[type=file]')).map((node, index) => ({
              index,
              accept: attr(node, 'accept'),
              multiple: node.multiple,
              id: node.id || '',
              name: node.name || '',
              class: node.className || '',
              style: attr(node, 'style'),
              hidden: node.hidden,
              disabled: node.disabled,
              parent_text: textOf(node.parentElement || node).slice(0, 200)
            })),
            controls: Array.from(document.querySelectorAll('button, a, .weui-desktop-btn, [role=button]'))
              .map(node => textOf(node))
              .filter(Boolean)
              .slice(0, 160),
            fields: Array.from(document.querySelectorAll('input, textarea, [contenteditable=true]')).map((node, index) => ({
              index,
              tag: node.tagName,
              id: node.id || '',
              name: node.name || '',
              class: node.className || '',
              placeholder: attr(node, 'placeholder'),
              type: attr(node, 'type'),
              contenteditable: attr(node, 'contenteditable'),
              text: textOf(node).slice(0, 200)
            })).slice(0, 120),
            images: Array.from(document.images).map((node, index) => ({
              index,
              src: (node.currentSrc || node.src || '').slice(0, 180),
              alt: node.alt || '',
              class: node.className || '',
              width: node.naturalWidth || node.width || 0,
              height: node.naturalHeight || node.height || 0
            })).slice(0, 120)
          };
        }
        """
    )
    details.update(
        {
            "ok": True,
            "action": "inspect",
            "screenshot": str(screenshot_path),
            "html": "",
        }
    )
    return details


def fill_contenteditable(page: Any, selector: str, text: str, multiline: bool = False) -> bool:
    locator = page.locator(selector)
    if locator.count() <= 0:
        return False
    locator.first.scroll_into_view_if_needed()
    locator.first.click()
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    if multiline:
        lines = text.splitlines() or [text]
        for index, line in enumerate(lines):
            if line:
                page.keyboard.insert_text(line)
            if index < len(lines) - 1:
                page.keyboard.press("Shift+Enter")
    else:
        page.keyboard.insert_text(text)
    page.evaluate(
        """
        (selector) => {
          const node = document.querySelector(selector);
          if (!node) return;
          node.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText' }));
          node.dispatchEvent(new Event('change', { bubbles: true }));
        }
        """,
        selector,
    )
    return True


def fill_title(page: Any, title: str) -> None:
    if fill_contenteditable(page, ".title-editor__input .ProseMirror[contenteditable=true]", title):
        return
    selectors = ("#title", "input[name='title']", "textarea[name='title']")
    for selector in selectors:
        locator = page.locator(selector)
        if locator.count() > 0:
            locator.first.fill(title)
            return
    raise RuntimeError("没有找到公众号标题输入框。")


def fill_content(page: Any, content: str) -> None:
    for selector in (
        "#guide_words_main .ProseMirror[contenteditable=true]",
        "#ueditor_0 .ProseMirror[contenteditable=true]",
    ):
        if fill_contenteditable(page, selector, content, multiline=True):
            return

    html = "<p>" + "</p><p>".join(
        line.strip() for line in content.splitlines() if line.strip()
    ) + "</p>"
    result = page.evaluate(
        """
        (html) => {
          const pm = document.querySelector('.ProseMirror[contenteditable=true]');
          if (pm) {
            pm.innerHTML = html;
            pm.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText' }));
            return 'ProseMirror';
          }
          const oldEditor = document.querySelector('.js_pmEditorArea, #ueditor_0, [contenteditable=true]');
          if (oldEditor) {
            oldEditor.innerHTML = html;
            oldEditor.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText' }));
            return 'contenteditable';
          }
          return '';
        }
        """,
        html,
    )
    if result:
        return

    for selector in ("#js_description", "#ueditor_0", ".js_pmEditorArea", "[contenteditable=true]"):
        locator = page.locator(selector)
        if locator.count() > 0:
            locator.first.click()
            page.keyboard.insert_text(content)
            return
    raise RuntimeError("没有找到公众号正文编辑区。")


def click_button_by_text(
    page: Any,
    labels: tuple[str, ...],
    timeout_ms: int = 8000,
    forbidden: tuple[str, ...] = (),
    containers: tuple[str, ...] = (),
) -> str:
    deadline = time.monotonic() + max(1, timeout_ms / 1000)
    script = """
    ([labels, forbidden, containers]) => {
      const visible = (node) => {
        const style = window.getComputedStyle(node);
        return style.display !== 'none'
          && style.visibility !== 'hidden'
          && style.opacity !== '0'
          && (node.offsetWidth || node.offsetHeight || node.getClientRects().length);
      };
      const roots = containers.length
        ? containers.flatMap(selector => Array.from(document.querySelectorAll(selector))).filter(visible)
        : [document];
      const candidates = [];
      for (const root of roots) {
        const nodes = Array.from(root.querySelectorAll('button, a, .weui-desktop-btn, [role=button]'));
        for (const node of nodes) {
          if (!visible(node)) continue;
          const text = (node.textContent || '').trim();
          if (!text) continue;
          if (forbidden.some(label => text.includes(label))) continue;
          const exact = labels.includes(text);
          const fuzzy = labels.some(label => text.includes(label));
          if (!exact && !fuzzy) continue;
          const cls = String(node.className || '');
          let score = 0;
          if (exact) score += 100;
          if (cls.includes('primary')) score += 30;
          if (node.closest('.weui-desktop-dialog')) score += 20;
          if (node.closest('.new_mass_send_dialog')) score += 40;
          if (node.closest('.double_check_dialog')) score += 40;
          candidates.push({ node, text, score });
        }
      }
      candidates.sort((a, b) => b.score - a.score);
      const best = candidates[0];
      if (best) {
        best.node.scrollIntoView({ block: 'center' });
        best.node.click();
        return best.text;
      }
      return '';
    }
    """
    while time.monotonic() < deadline:
        result = page.evaluate(script, [list(labels), list(forbidden), list(containers)])
        if result:
            return str(result)
        time.sleep(0.5)
    raise RuntimeError(f"没有找到按钮：{','.join(labels)}")


def disable_mass_notify(page: Any) -> bool:
    return bool(
        page.evaluate(
            """
            () => {
              const visible = (node) => {
                const style = window.getComputedStyle(node);
                return style.display !== 'none'
                  && style.visibility !== 'hidden'
                  && style.opacity !== '0'
                  && (node.offsetWidth || node.offsetHeight || node.getClientRects().length);
              };
              const dialogs = Array.from(document.querySelectorAll('.new_mass_send_dialog .weui-desktop-dialog__wrp'))
                .filter(visible);
              for (const dialog of dialogs) {
                const rows = Array.from(dialog.querySelectorAll('.publish_container, .weui-desktop-form__control-group'));
                for (const row of rows) {
                  const text = (row.textContent || '').trim();
                  if (!text.includes('群发通知')) continue;
                  const switchBox = row.querySelector('.weui-desktop-switch__box, .weui-desktop-switch');
                  if (switchBox) {
                    switchBox.scrollIntoView({ block: 'center' });
                    switchBox.click();
                    return true;
                  }
                  return false;
                }
              }
              return false;
            }
            """
        )
    )


def recent_publish_contains(page: Any, title: str) -> bool:
    page.goto(WECHAT_HOME_URL, wait_until="domcontentloaded")
    time.sleep(5)
    body = visible_text(page)
    if "近期发表" not in body:
        return False
    publish_section = body.split("近期发表", 1)[1]
    if "查看全部" in publish_section:
        publish_section = publish_section.split("查看全部", 1)[0]
    return normalized_text(title) in normalized_text(publish_section)


def has_qr_confirmation(page: Any) -> bool:
    body = visible_text(page)
    return "扫码" in body or "二维码" in body


def publish_failure_reason(page: Any) -> str | None:
    body = visible_text(page)
    if "运营规则学习提醒" in body or "开始答题" in body:
        return "公众号发布前需要手动完成运营规则学习题目。"
    if "系统繁忙" in body:
        return "公众号后台系统繁忙。"
    if "扫码" in body or "二维码" in body:
        return "公众号扫码确认超时。"
    return None


def write_qr_event(
    page: Any,
    profile_dir: Path,
    qr_event_path: Path | None,
    title: str,
) -> str | None:
    if qr_event_path is None:
        return None
    if qr_event_path.exists():
        with contextlib.suppress(Exception):
            return str(json.loads(qr_event_path.read_text(encoding="utf-8")).get("screenshot") or "")
    output_dir = profile_dir / "diagnostics"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    screenshot_path = output_dir / f"wechat-qr-{stamp}.png"
    try:
        dialog = page.locator(".weui-desktop-dialog__wrp:visible").filter(has_text=re.compile("扫码|二维码"))
        if dialog.count() > 0:
            dialog.last.screenshot(path=str(screenshot_path))
        else:
            page.screenshot(path=str(screenshot_path), full_page=True)
    except Exception:
        page.screenshot(path=str(screenshot_path), full_page=True)
    payload = {
        "event": "wechat_qr_required",
        "title": title,
        "screenshot": str(screenshot_path),
        "url": redact_sensitive_text(page.url),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "message": "公众号需要扫码确认，请扫这张码。",
    }
    qr_event_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = qr_event_path.with_suffix(qr_event_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(qr_event_path)
    return str(screenshot_path)


def wait_after_qr_confirmation(
    page: Any,
    title: str,
    timeout_seconds: int,
    button_label: str,
    profile_dir: Path,
    qr_event_path: Path | None,
) -> dict[str, Any]:
    qr_screenshot = write_qr_event(page, profile_dir, qr_event_path, title)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        text = visible_text(page)
        if any(pattern in text for pattern in PUBLISH_SUCCESS_PATTERNS):
            return {
                "ok": True,
                "action": "publish",
                "button": button_label,
                "url": page.url,
                "qr_screenshot": qr_screenshot,
            }
        if "运营规则学习提醒" in text or "开始答题" in text:
            return {
                "ok": False,
                "reason": "公众号发布前需要手动完成运营规则学习题目。",
                "action": "publish",
                "button": button_label,
                "url": page.url,
                "qr_screenshot": qr_screenshot,
            }
        if "系统繁忙" in text:
            return {
                "ok": False,
                "reason": "公众号后台系统繁忙。",
                "action": "publish",
                "button": button_label,
                "url": page.url,
                "qr_screenshot": qr_screenshot,
            }
        if not has_qr_confirmation(page):
            time.sleep(5)
            with contextlib.suppress(Exception):
                if recent_publish_contains(page, title):
                    return {
                        "ok": True,
                        "action": "publish",
                        "button": button_label,
                        "url": page.url,
                        "verified_by": "recent_publish",
                        "qr_screenshot": qr_screenshot,
                    }
            return {
                "ok": False,
                "reason": "公众号浏览器发布未确认成功。",
                "action": "publish",
                "button": button_label,
                "url": page.url,
                "qr_screenshot": qr_screenshot,
            }
        time.sleep(2)
    return {
        "ok": False,
        "reason": "公众号扫码确认超时。",
        "action": "publish",
        "button": button_label,
        "url": page.url,
        "qr_screenshot": qr_screenshot,
    }


def wait_for_any_success(page: Any, patterns: tuple[str, ...], timeout_seconds: int) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        text = visible_text(page)
        if any(pattern in text for pattern in patterns):
            return True
        time.sleep(2)
    return False


def save_draft(page: Any) -> dict[str, Any]:
    label = click_button_by_text(page, ("保存为草稿", "保存草稿", "保存"))
    wait_for_any_success(page, DRAFT_SUCCESS_PATTERNS, 20)
    return {"ok": True, "action": "draft", "button": label, "url": page.url}


def publish(
    page: Any,
    timeout_seconds: int,
    title: str,
    profile_dir: Path,
    qr_event_path: Path | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    label = click_button_by_text(
        page,
        ("发表", "发布", "群发"),
        forbidden=("保存", "草稿", "预览"),
    )
    time.sleep(1)
    confirm_steps = (
        (
            ".new_mass_send_dialog .weui-desktop-dialog__wrp",
            ("发表", "发布", "群发"),
        ),
        (
            ".double_check_dialog .weui-desktop-dialog__wrp",
            ("继续发表", "继续发布", "继续群发", "确定"),
        ),
        (
            ".weui-desktop-dialog__wrp",
            ("继续发表", "继续发布", "继续群发", "确定"),
        ),
    )
    for container, labels in confirm_steps:
        if "new_mass_send_dialog" in container:
            with contextlib.suppress(Exception):
                if disable_mass_notify(page):
                    label += "->关闭群发通知"
                    time.sleep(1)
        try:
            confirm = click_button_by_text(
                page,
                labels,
                timeout_ms=8000,
                forbidden=("保存", "草稿", "预览", "取消"),
                containers=(container,),
            )
            label += f"->{confirm}"
            time.sleep(2)
        except Exception:
            pass
        reason = publish_failure_reason(page)
        if reason:
            if reason == "公众号扫码确认超时。":
                remaining = max(1, int(deadline - time.monotonic()))
                return wait_after_qr_confirmation(
                    page,
                    title,
                    remaining,
                    label,
                    profile_dir,
                    qr_event_path,
                )
            return {
                "ok": False,
                "reason": reason,
                "action": "publish",
                "button": label,
                "url": page.url,
            }
    if wait_for_any_success(page, PUBLISH_SUCCESS_PATTERNS, timeout_seconds):
        return {"ok": True, "action": "publish", "button": label, "url": page.url}
    with contextlib.suppress(Exception):
        if recent_publish_contains(page, title):
            return {
                "ok": True,
                "action": "publish",
                "button": label,
                "url": page.url,
                "verified_by": "recent_publish",
            }
    reason = publish_failure_reason(page)
    if reason:
        if reason == "公众号扫码确认超时。":
            remaining = max(1, int(deadline - time.monotonic()))
            return wait_after_qr_confirmation(
                page,
                title,
                remaining,
                label,
                profile_dir,
                qr_event_path,
            )
        return {
            "ok": False,
            "reason": reason,
            "action": "publish",
            "button": label,
            "url": page.url,
        }
    return {
        "ok": False,
        "reason": "公众号浏览器发布未确认成功。",
        "action": "publish",
        "button": label,
        "url": page.url,
    }


def run(payload: dict[str, Any]) -> dict[str, Any]:
    title = compress_title(str(payload.get("title") or "图文"))
    content = compress_content(str(payload.get("content") or ""))
    images = [str(Path(path).resolve()) for path in payload.get("images") or []]
    action = str(payload.get("action") or "publish").strip().lower()
    timeout_seconds = int(payload.get("timeout_seconds") or 900)
    profile_dir = Path(str(payload.get("profile_dir") or Path.cwd() / "state/wechat_chrome_profile"))
    qr_event_path = Path(str(payload["qr_event_path"])) if payload.get("qr_event_path") else None
    chrome_path = find_chrome(payload.get("chrome_path"))
    cdp_url = str(payload.get("cdp_url") or "").strip()

    if action not in {"draft", "publish", "check", "login", "inspect", "upload_inspect", "fill_inspect", "publish_inspect"}:
        raise RuntimeError(f"不支持的公众号浏览器动作：{action}")
    if action not in {"check", "login", "inspect", "upload_inspect"} and not content:
        raise RuntimeError("公众号正文不能为空。")
    if action not in {"check", "login", "inspect"} and not images:
        raise RuntimeError("至少需要一张图片。")
    for image in images:
        if not Path(image).exists():
            raise RuntimeError(f"图片不存在：{image}")

    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = None
        context = None
        if cdp_url:
            browser = p.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
        else:
            launch_kwargs: dict[str, Any] = {
                "user_data_dir": str(profile_dir),
                "headless": False,
                "viewport": {"width": 1440, "height": 1000},
                "args": ["--start-maximized"],
            }
            if chrome_path:
                launch_kwargs["executable_path"] = chrome_path
            else:
                launch_kwargs["channel"] = "chrome"
            context = p.chromium.launch_persistent_context(**launch_kwargs)
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(30000)
        page.goto(WECHAT_HOME_URL, wait_until="domcontentloaded")
        if action == "check":
            return check_ready(page)
        wait_for_login(page, timeout_seconds)
        if action == "login":
            state_path = save_storage_state(context, profile_dir)
            result = check_ready(page)
            result.update({"ok": True, "action": "login", "storage_state": state_path})
            return result
        editor = open_editor(context, page)
        editor.set_default_timeout(30000)
        time.sleep(2)
        if action == "inspect":
            return inspect_editor(editor, profile_dir)
        upload_selector = upload_images(editor, images)
        if action == "upload_inspect":
            time.sleep(15)
            result = inspect_editor(editor, profile_dir)
            result.update({"action": "upload_inspect", "upload_selector": upload_selector})
            return result
        wait_upload_finished(editor, len(images))
        fill_title(editor, title)
        fill_content(editor, content)
        if action == "fill_inspect":
            time.sleep(2)
            result = inspect_editor(editor, profile_dir)
            result.update(
                {
                    "action": "fill_inspect",
                    "upload_selector": upload_selector,
                    "title": title,
                    "image_count": len(images),
                    "uploaded_image_count": uploaded_image_count(editor),
                }
            )
            return result
        if action == "publish_inspect":
            label = click_button_by_text(
                editor,
                ("发表", "发布", "群发"),
                forbidden=("保存", "草稿", "预览"),
            )
            time.sleep(3)
            result = inspect_editor(editor, profile_dir)
            result.update(
                {
                    "action": "publish_inspect",
                    "upload_selector": upload_selector,
                    "publish_button": label,
                    "title": title,
                    "image_count": len(images),
                    "uploaded_image_count": uploaded_image_count(editor),
                }
            )
            return result
        time.sleep(1)
        result = (
            save_draft(editor)
            if action == "draft"
            else publish(editor, timeout_seconds, title, profile_dir, qr_event_path)
        )
        result.update({"upload_selector": upload_selector, "title": title, "image_count": len(images)})
        result["storage_state"] = save_storage_state(context, profile_dir)
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish WeChat Official Account image-text via browser.")
    parser.add_argument("--payload", required=True, help="JSON payload path")
    args = parser.parse_args()
    try:
        payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
        result = run(payload)
        emit(result)
        return 0 if result.get("ok") else 2
    except Exception as exc:
        emit({"ok": False, "reason": compact(exc, 500)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
