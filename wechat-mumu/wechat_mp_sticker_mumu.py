#!/usr/bin/env python3
"""MuMu + WeChat MP Assistant sticker publishing helper.

This script uses normal Android UI automation through adb. It does not read
cookies, tokens, localStorage, app private files, or WeChat databases.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageChops, ImageOps

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
SCREENSHOT_DIR = ROOT / "screenshots"
STATE_DIR = ROOT / "state"
DEFAULT_ADB = Path(r"D:\Program Files\Netease\MuMuPlayer\nx_device\12.0\shell\adb.exe")
DEFAULT_DEVICE = "127.0.0.1:7555"
MP_PACKAGE = "com.tencent.mp"
RISK_WORDS = (
    "违规",
    "审核",
    "风险",
    "敏感",
    "失败",
    "异常",
    "登录",
    "扫码",
    "验证",
    "二维码",
    "操作频繁",
    "请修改",
    "无法发布",
    "不能发表",
    "内容不符合",
    "标题不能为空",
    "正文不能为空",
    "封面不能为空",
)
BENIGN_RISK_PHRASES = (
    "有草稿保存异常，已保留本地副本",
)


def now_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def ensure_dirs() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    STATE_DIR.mkdir(exist_ok=True)


def write_jsonl(event: dict) -> None:
    ensure_dirs()
    event = {"ts": datetime.now().isoformat(timespec="seconds"), **event}
    with (LOG_DIR / "run.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


class Adb:
    def __init__(self, adb_path: Path, device: str, run_id: str):
        self.adb_path = adb_path
        self.device = device
        self.run_id = run_id
        if not adb_path.exists():
            raise SystemExit(f"adb_not_found={adb_path}")

    def run(self, args: list[str], check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess:
        cmd = [str(self.adb_path), "-s", self.device, *args]
        cp = subprocess.run(
            cmd,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        if check and cp.returncode != 0:
            raise RuntimeError(f"adb failed: {' '.join(args)}\n{cp.stderr.strip()}")
        return cp

    def shell(self, command: str, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess:
        return self.run(["shell", command], check=check, timeout=timeout)

    def tap(self, x: int, y: int, note: str = "") -> None:
        write_jsonl({"event": "tap", "run_id": self.run_id, "x": x, "y": y, "note": note})
        self.shell(f"input tap {x} {y}")
        time.sleep(0.8)

    def keyevent(self, key: str | int) -> None:
        self.shell(f"input keyevent {key}")
        time.sleep(0.35)

    def text(self, value: str) -> None:
        # Quoting protects spaces, #, |, etc. MuMu's Android 12 input keeps "%s"
        # literally, so pass real spaces.
        escaped = value
        quoted = "'" + escaped.replace("'", "'\\''") + "'"
        self.shell(f"input text {quoted}", timeout=30)
        time.sleep(0.35)

    def multiline_text(self, value: str) -> None:
        lines = value.splitlines()
        for idx, line in enumerate(lines):
            if line:
                self.text(line)
            if idx != len(lines) - 1:
                self.keyevent("ENTER")

    def clear_focused_text(self, count: int = 220) -> None:
        self.keyevent("MOVE_END")
        chunk = " ".join(["DEL"] * 40)
        for _ in range(max(1, count // 40)):
            self.shell(f"input keyevent {chunk}", check=False, timeout=20)
        time.sleep(0.3)

    def ime_visible(self) -> bool:
        cp = self.shell("dumpsys input_method", check=False, timeout=20)
        text = cp.stdout + cp.stderr
        return "mInputShown=true" in text or "mIsInputViewShown=true" in text

    def hide_keyboard_if_visible(self) -> None:
        if self.ime_visible():
            self.keyevent("BACK")

    def screenshot(self, label: str) -> Path:
        ensure_dirs()
        remote = f"/sdcard/{self.run_id}_{label}.png"
        local = SCREENSHOT_DIR / f"{self.run_id}_{label}.png"
        self.shell(f"screencap -p {remote}")
        self.run(["pull", remote, str(local)])
        self.shell(f"rm {remote}", check=False)
        return local

    def dump_ui(self, label: str) -> tuple[Path, list[dict]]:
        ensure_dirs()
        remote = f"/sdcard/Download/{self.run_id}_{label}.xml"
        local = LOG_DIR / f"{self.run_id}_{label}.xml"
        self.shell("mkdir -p /sdcard/Download", check=False)
        last_error = ""
        for attempt in range(4):
            dump = self.shell(f"uiautomator dump {remote}", check=False)
            time.sleep(0.5)
            exists = self.shell(f"ls {remote}", check=False)
            pull = self.run(["pull", remote, str(local)], check=False) if exists.returncode == 0 else None
            if pull and pull.returncode == 0 and local.exists() and local.stat().st_size > 0:
                break
            last_error = (dump.stderr or dump.stdout or (pull.stderr if pull else "") or "").strip()
            write_jsonl(
                {
                    "event": "dump_ui_retry",
                    "run_id": self.run_id,
                    "label": label,
                    "attempt": attempt + 1,
                    "error": last_error,
                }
            )
            time.sleep(1)
        else:
            raise RuntimeError(f"uiautomator dump failed for {label}: {last_error}")
        self.shell(f"rm {remote}", check=False)
        nodes: list[dict] = []
        root = ET.parse(local).getroot()
        for elem in root.iter("node"):
            item = dict(elem.attrib)
            item["bounds_tuple"] = parse_bounds(item.get("bounds", ""))
            nodes.append(item)
        return local, nodes


def parse_bounds(raw: str) -> tuple[int, int, int, int]:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", raw or "")
    if not m:
        return (0, 0, 0, 0)
    return tuple(map(int, m.groups()))  # type: ignore[return-value]


def center(node: dict) -> tuple[int, int]:
    x1, y1, x2, y2 = node["bounds_tuple"]
    return (x1 + x2) // 2, (y1 + y2) // 2


def lower_right(node: dict) -> tuple[int, int]:
    x1, y1, x2, y2 = node["bounds_tuple"]
    return max(x1 + 10, x2 - 30), max(y1 + 10, y2 - 20)


def find(nodes: list[dict], *, text: str | None = None, desc: str | None = None,
         rid: str | None = None, contains: bool = False) -> dict | None:
    for node in nodes:
        checks: list[bool] = []
        if text is not None:
            actual = node.get("text", "")
            checks.append(text in actual if contains else actual == text)
        if desc is not None:
            actual = node.get("content-desc", "")
            checks.append(desc in actual if contains else actual == desc)
        if rid is not None:
            actual = node.get("resource-id", "")
            checks.append(rid in actual if contains else actual == rid)
        if checks and all(checks):
            return node
    return None


def visible_text(nodes: list[dict]) -> str:
    parts = []
    for node in nodes:
        for key in ("text", "content-desc"):
            value = node.get(key, "")
            if value:
                parts.append(value)
    return "\n".join(parts)


def risk_found(nodes: list[dict]) -> list[str]:
    text = visible_text(nodes)
    for phrase in BENIGN_RISK_PHRASES:
        text = text.replace(phrase, "")
    return [word for word in RISK_WORDS if word in text]


def tap_found(adb: Adb, nodes: list[dict], note: str, **kwargs) -> bool:
    node = find(nodes, **kwargs)
    if not node:
        return False
    x, y = center(node)
    adb.tap(x, y, note)
    return True


def tap_node(adb: Adb, node: dict, note: str, fallback_xy: tuple[int, int] | None = None) -> None:
    x1, y1, x2, y2 = node["bounds_tuple"]
    if x2 > x1 and y2 > y1:
        x, y = center(node)
    elif fallback_xy:
        x, y = fallback_xy
    else:
        x, y = center(node)
    adb.tap(x, y, note)


def wait_for(adb: Adb, label: str, predicate, timeout: int = 20) -> tuple[Path, list[dict]]:
    deadline = time.time() + timeout
    last: tuple[Path, list[dict]] | None = None
    while time.time() < deadline:
        last = adb.dump_ui(label)
        if predicate(last[1]):
            return last
        time.sleep(1)
    if last is None:
        last = adb.dump_ui(label)
    return last


def launch_mp(adb: Adb) -> None:
    adb.run(["connect", adb.device], check=False)
    adb.shell(f"am force-stop {MP_PACKAGE}", check=False)
    time.sleep(1)
    for attempt in range(4):
        adb.shell(f"monkey -p {MP_PACKAGE} -c android.intent.category.LAUNCHER 1", check=False)
        adb.shell(
            "am start -W -n com.tencent.mp/.feature.launcher.ui.LauncherActivity",
            check=False,
            timeout=30,
        )
        time.sleep(4)
        _, nodes = adb.dump_ui(f"after_launch_mp_{attempt + 1}")
        text = visible_text(nodes)
        if find(nodes, desc="发表贴图") or find(nodes, text="贴图") or "创作发表" in text:
            return
        launcher_icon = find(nodes, text="公众号助手") or find(nodes, desc="公众号助手")
        if launcher_icon:
            adb.tap(*center(launcher_icon), note="launch mp from launcher icon")
            time.sleep(5)
            _, nodes = adb.dump_ui(f"after_launcher_icon_mp_{attempt + 1}")
            text = visible_text(nodes)
            if find(nodes, desc="发表贴图") or find(nodes, text="贴图") or "创作发表" in text:
                return
    screenshot = adb.screenshot("mp_home_not_reached")
    raise RuntimeError(f"mp_home_not_reached; screenshot={screenshot}")


def grant_media_permissions(adb: Adb) -> None:
    for perm in (
        "android.permission.READ_EXTERNAL_STORAGE",
        "android.permission.WRITE_EXTERNAL_STORAGE",
        "android.permission.READ_MEDIA_IMAGES",
    ):
        adb.shell(f"pm grant {MP_PACKAGE} {perm}", check=False)


def cleanup_probe_media(adb: Adb) -> None:
    # Only remove files this probe created in public media locations.
    for pattern in (
        "/sdcard/mumu_*.png",
        "/sdcard/*_mumu_*.png",
        "/sdcard/wechat_mp_probe_johnny.png",
        "/sdcard/IMG_20260701_Johnny.png",
        "/sdcard/DCIM/Camera/IMG_*_JOHNNY.png",
        "/sdcard/Pictures/MPProbe/*.png",
    ):
        adb.shell(f"rm -f {pattern}", check=False)
    for where in (
        "\"_display_name LIKE 'mumu_%'\"",
        "\"_display_name LIKE '%_mumu_%'\"",
        "\"_display_name='wechat_mp_probe_johnny.png'\"",
        "\"_display_name='IMG_20260701_Johnny.png'\"",
        "\"_display_name LIKE 'IMG_%_JOHNNY.%'\"",
    ):
        adb.shell(f"content delete --uri content://media/external/images/media --where {where}", check=False)


def push_image(adb: Adb, image_path: Path) -> str:
    if not image_path.exists():
        raise SystemExit(f"image_not_found={image_path}")
    cleanup_probe_media(adb)
    remote_dir = "/sdcard/DCIM/Camera"
    suffix = image_path.suffix.lower() or ".png"
    remote = f"{remote_dir}/IMG_{adb.run_id.replace('-', '_')}_JOHNNY{suffix}"
    media_path = remote.replace("/sdcard/", "/storage/emulated/0/", 1)
    adb.shell(f"mkdir -p {remote_dir}")
    adb.run(["push", str(image_path), remote], timeout=120)
    adb.shell(f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://{media_path} -p com.android.providers.media.module", check=False)
    adb.shell(f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://{media_path}", check=False)
    adb.shell(f"cmd media scan {media_path}", check=False)
    time.sleep(3)
    write_jsonl({"event": "image_pushed_and_scanned", "run_id": adb.run_id, "remote_path": remote})
    return remote


def go_home_if_needed(adb: Adb) -> None:
    for _ in range(6):
        _, nodes = adb.dump_ui("ensure_home")
        if find(nodes, desc="发表贴图") or find(nodes, text="贴图"):
            return
        close = find(nodes, desc="关闭") or find(nodes, text="关闭")
        if close:
            adb.tap(*center(close), note="close current mp page")
        else:
            adb.keyevent("BACK")
        time.sleep(1)


def stop_if_risky(adb: Adb, label: str, nodes: list[dict]) -> None:
    hits = risk_found(nodes)
    if not hits:
        return
    screenshot = adb.screenshot(f"{label}_risk")
    raise RuntimeError(f"risk_or_login_text_found={hits}; screenshot={screenshot}")


def open_sticker_picker(adb: Adb) -> list[dict]:
    _, nodes = adb.dump_ui("home_before_sticker")
    stop_if_risky(adb, "home_before_sticker", nodes)
    if not tap_found(adb, nodes, "open sticker by desc", desc="发表贴图"):
        if not tap_found(adb, nodes, "open sticker by id", rid="com.tencent.mp:id/ll_image_text"):
            if not tap_found(adb, nodes, "open sticker by text", text="贴图"):
                screenshot = adb.screenshot("sticker_entry_not_found")
                text_sample = "|".join(visible_text(nodes).splitlines()[:12])
                raise RuntimeError(
                    f"sticker_entry_not_found; screenshot={screenshot}; text_sample={text_sample}"
                )
    time.sleep(2)
    _, nodes = adb.dump_ui("after_sticker_tap")
    if tap_found(adb, nodes, "close guide", text="我知道了"):
        time.sleep(1)
        _, nodes = adb.dump_ui("after_guide_closed")
    return nodes


def thumb_distance(target_path: Path, screenshot_path: Path, bounds: tuple[int, int, int, int]) -> float:
    x1, y1, x2, y2 = bounds
    with Image.open(target_path) as target, Image.open(screenshot_path) as screen:
        target = ImageOps.fit(target.convert("RGB"), (32, 32))
        crop = screen.crop((x1, y1, x2, y2)).convert("RGB")
        crop = ImageOps.fit(crop, (32, 32))
        diff = ImageChops.difference(target, crop)
        hist = diff.histogram()
        sq = (value * ((idx % 256) ** 2) for idx, value in enumerate(hist))
        return (sum(sq) / (32 * 32 * 3)) ** 0.5


def candidate_thumb_bounds(nodes: list[dict]) -> list[tuple[int, int, int, int]]:
    bounds = []
    for node in nodes:
        x1, y1, x2, y2 = node["bounds_tuple"]
        is_media = node.get("resource-id", "").endswith(":id/iv_media") or "[图片]" in node.get("content-desc", "")
        if is_media and y1 >= 350 and (x2 - x1) >= 80 and (y2 - y1) >= 80:
            bounds.append((x1, y1, x2, y2))
    if bounds:
        return sorted(bounds, key=lambda b: (b[1], b[0]))
    # Fallback to the normal 3-column grid below the camera/AI/poster row.
    cells = []
    for row in range(3):
        for col in range(3):
            x1 = col * 300
            y1 = 436 + row * 300
            cells.append((x1, y1, x1 + 300, y1 + 300))
    return cells


def find_next_button(nodes: list[dict]) -> dict | None:
    return (
        find(nodes, rid="com.tencent.mp:id/tv_next")
        or find(nodes, text="下一步", contains=True)
        or find(nodes, desc="下一步", contains=True)
    )


def tap_next_button(adb: Adb, node: dict, note: str) -> None:
    # WeChat sometimes reports tv_next bounds as [0,0][0,0] while the button
    # is visibly anchored at the bottom-right of a 900x1600 screen.
    tap_node(adb, node, note, fallback_xy=(790, 1515))


def find_editor_publish_button(nodes: list[dict]) -> dict | None:
    node = find(nodes, rid="com.tencent.mp:id/btn_action_option")
    if node and "发表" in node.get("text", ""):
        return node
    for item in nodes:
        x1, y1, x2, y2 = item["bounds_tuple"]
        label = item.get("text", "") + item.get("content-desc", "")
        if "发表" in label and item.get("clickable") == "true" and x1 >= 700 and y2 <= 120:
            return item
    return None


def find_dialog_publish_button(nodes: list[dict]) -> dict | None:
    has_publish_dialog = bool(find(nodes, text="发表后通知用户", contains=True) or find(nodes, desc="发表后通知用户", contains=True))
    buttons = []
    for item in nodes:
        x1, y1, x2, y2 = item["bounds_tuple"]
        label = item.get("text", "") + item.get("content-desc", "")
        if label == "发表" and item.get("clickable") == "true" and ((x2 > x1 and y2 > y1 and y1 >= 300) or has_publish_dialog):
            buttons.append(item)
    buttons.sort(key=lambda n: (n["bounds_tuple"][1], n["bounds_tuple"][0]))
    return buttons[-1] if buttons else None


def tap_next_after_image_select(adb: Adb) -> None:
    _, nodes = wait_for(
        adb,
        "picker_after_select",
        lambda ns: bool(find_next_button(ns) or find(ns, rid="com.tencent.mp:id/check_view")),
        timeout=10,
    )

    next_button = find_next_button(nodes)
    if next_button:
        tap_next_button(adb, next_button, "tap next")
        return

    preview_check = find(nodes, rid="com.tencent.mp:id/check_view")
    if preview_check:
        x, y = center(preview_check)
        adb.tap(x, y, "tap preview check")
        time.sleep(1)
        _, nodes = wait_for(
            adb,
            "picker_after_preview_check",
            lambda ns: bool(find_next_button(ns) or find(ns, rid="com.tencent.mp:id/actionbar_up_indicator_btn")),
            timeout=10,
        )
        next_button = find_next_button(nodes)
        if next_button:
            tap_next_button(adb, next_button, "tap next after preview check")
            return
        back_button = find(nodes, rid="com.tencent.mp:id/actionbar_up_indicator_btn")
        if back_button:
            x, y = center(back_button)
            adb.tap(x, y, "back from preview after check")
            _, nodes = wait_for(adb, "picker_after_preview_back", lambda ns: bool(find_next_button(ns)), timeout=10)
            next_button = find_next_button(nodes)
            if next_button:
                tap_next_button(adb, next_button, "tap next after preview back")
                return

    screenshot = adb.screenshot("next_button_missing_after_image_select")
    ui_dump, nodes = adb.dump_ui("next_button_missing_after_image_select")
    text_sample = "|".join(visible_text(nodes).splitlines()[:12])
    raise RuntimeError(
        f"next_button_not_found_after_image_select; screenshot={screenshot}; ui_dump={ui_dump}; text_sample={text_sample}"
    )


def select_target_image(adb: Adb, image_path: Path) -> None:
    _, nodes = wait_for(
        adb,
        "picker_wait",
        lambda ns: bool(find(ns, text="拍照") or find(ns, desc="拍照")) and bool(find(ns, text="写文字")),
        timeout=15,
    )
    stop_if_risky(adb, "picker", nodes)
    screenshot = adb.screenshot("picker_for_visual_match")
    candidates = candidate_thumb_bounds(nodes)
    scored = [(thumb_distance(image_path, screenshot, b), b) for b in candidates]
    scored.sort(key=lambda item: item[0])
    best_score, (x1, y1, x2, y2) = scored[0]
    write_jsonl({
        "event": "picker_visual_match",
        "run_id": adb.run_id,
        "best_score": round(best_score, 2),
        "best_bounds": [x1, y1, x2, y2],
    })
    adb.tap(max(x1 + 10, x2 - 28), y1 + 28, "select target thumbnail circle")
    tap_next_after_image_select(adb)

    _, nodes = wait_for(
        adb,
        "preview_wait_done",
        lambda ns: bool(find(ns, text="完成", contains=True) or find(ns, desc="完成", contains=True)),
        timeout=30,
    )
    done_node = find(nodes, text="完成", contains=True) or find(nodes, desc="完成", contains=True)
    if done_node:
        tap_node(adb, done_node, "tap done", fallback_xy=(831, 1557))
    else:
        screenshot = adb.screenshot("preview_done_missing")
        ui_dump, nodes = adb.dump_ui("preview_done_missing")
        text_sample = "|".join(visible_text(nodes).splitlines()[:12])
        raise RuntimeError(f"done_button_not_found_after_next; screenshot={screenshot}; ui_dump={ui_dump}; text_sample={text_sample}")
    time.sleep(3)


def fill_title_body(adb: Adb, title: str, body: str) -> None:
    _, nodes = wait_for(
        adb,
        "edit_before_fill",
        lambda ns: len([n for n in ns if "EditText" in n.get("class", "")]) >= 2
        or bool(find(ns, text="填写描述", contains=True)),
        timeout=15,
    )
    stop_if_risky(adb, "edit_before_fill", nodes)
    edit_nodes = [n for n in nodes if "EditText" in n.get("class", "")]
    edit_nodes.sort(key=lambda n: (n["bounds_tuple"][1], n["bounds_tuple"][0]))
    if len(edit_nodes) >= 2:
        title_node, body_node = edit_nodes[0], edit_nodes[1]
    else:
        title_node = find(nodes, rid="com.tencent.mp:id/et_title") or find(nodes, text="标题", contains=True) or find(nodes, desc="标题", contains=True)
        body_node = find(nodes, text="描述", contains=True) or find(nodes, desc="描述", contains=True)
    if not title_node or not body_node:
        raise RuntimeError("title_or_body_input_not_found")

    x, y = lower_right(title_node)
    adb.tap(x, y, "focus title")
    adb.clear_focused_text(160)
    adb.text(title)

    x, y = lower_right(body_node)
    adb.tap(x, y, "focus body")
    adb.clear_focused_text(520)
    adb.multiline_text(body)
    adb.hide_keyboard_if_visible()
    time.sleep(1)


def finish_current(args: argparse.Namespace) -> None:
    ensure_dirs()
    run_id = now_id()
    adb = Adb(Path(args.adb), args.device, run_id)
    fill_title_body(adb, args.title, args.body)
    ui_path, nodes = adb.dump_ui("ready_before_publish")
    screenshot = adb.screenshot("ready_before_publish")
    publish_visible = bool(find(nodes, text="发表", contains=True) or find(nodes, desc="发表", contains=True))
    hits = risk_found(nodes)
    state = {
        "mode": "fill-current",
        "run_id": run_id,
        "title": args.title,
        "body": args.body,
        "publish_button_visible": publish_visible,
        "risk_warning_found": bool(hits),
        "risk_words": hits,
        "screenshot_path": str(screenshot),
        "ui_dump_path": str(ui_path),
        "state_path": str(STATE_DIR / f"{run_id}_pending_publish.json"),
        "stopped_before_final_publish": True,
    }
    state_path = STATE_DIR / f"{run_id}_pending_publish.json"
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    write_jsonl({"event": "fill_current_finished", **state})
    print(json.dumps(state, ensure_ascii=False, indent=2))


def prepare(args: argparse.Namespace) -> None:
    ensure_dirs()
    run_id = now_id()
    adb = Adb(Path(args.adb), args.device, run_id)
    image_remote = push_image(adb, Path(args.image)) if args.image else ""
    grant_media_permissions(adb)
    launch_mp(adb)
    go_home_if_needed(adb)
    nodes = open_sticker_picker(adb)
    select_target_image(adb, Path(args.image))
    fill_title_body(adb, args.title, args.body)
    ui_path, nodes = adb.dump_ui("ready_before_publish")
    screenshot = adb.screenshot("ready_before_publish")
    publish_visible = bool(find(nodes, text="发表", contains=True) or find(nodes, desc="发表", contains=True))
    hits = risk_found(nodes)
    state = {
        "mode": "prepare",
        "run_id": run_id,
        "image_remote_path": image_remote,
        "title": args.title,
        "body": args.body,
        "publish_button_visible": publish_visible,
        "risk_warning_found": bool(hits),
        "risk_words": hits,
        "screenshot_path": str(screenshot),
        "ui_dump_path": str(ui_path),
        "state_path": str(STATE_DIR / f"{run_id}_pending_publish.json"),
        "stopped_before_final_publish": True,
    }
    state_path = STATE_DIR / f"{run_id}_pending_publish.json"
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    write_jsonl({"event": "prepare_finished", **state})
    print(json.dumps(state, ensure_ascii=False, indent=2))


def tap_publish_sequence(adb: Adb, source_state_run_id: str = "") -> dict:
    ui_path, nodes = adb.dump_ui("before_first_publish_tap")
    before_screenshot = adb.screenshot("before_first_publish_tap")
    stop_if_risky(adb, "before_first_publish_tap", nodes)
    already_in_dialog = bool(find(nodes, text="发表后通知用户", contains=True) or find(nodes, desc="发表后通知用户", contains=True))
    if already_in_dialog:
        dialog_ui_path, dialog_nodes = ui_path, nodes
        dialog_screenshot = before_screenshot
    else:
        publish = find_editor_publish_button(nodes)
        if not publish:
            text_sample = "|".join(visible_text(nodes).splitlines()[:12])
            raise RuntimeError(f"publish_button_not_found_on_editor; screenshot={before_screenshot}; ui_dump={ui_path}; text_sample={text_sample}")

        tap_node(adb, publish, "tap first publish", fallback_xy=(831, 48))
        dialog_ui_path, dialog_nodes = wait_for(
            adb,
            "final_publish_dialog",
            lambda ns: bool(find_dialog_publish_button(ns)),
            timeout=20,
        )
        dialog_screenshot = adb.screenshot("final_publish_dialog")
    stop_if_risky(adb, "final_publish_dialog", dialog_nodes)
    dialog_publish = find_dialog_publish_button(dialog_nodes)
    if not dialog_publish:
        raise RuntimeError("final_confirm_publish_button_not_found")

    tap_node(adb, dialog_publish, "tap final dialog publish", fallback_xy=(570, 960))
    time.sleep(8)
    _, maybe_dialog_nodes = adb.dump_ui("after_final_dialog_publish_check")
    dialog_publish = find_dialog_publish_button(maybe_dialog_nodes)
    if dialog_publish:
        tap_node(adb, dialog_publish, "tap repeated final dialog publish", fallback_xy=(570, 960))
        time.sleep(8)
    after_ui_path, after_nodes = adb.dump_ui("after_publish")
    after_screenshot = adb.screenshot("after_publish")
    return {
        "source_state_run_id": source_state_run_id,
        "before_publish_screenshot_path": str(before_screenshot),
        "before_publish_ui_dump_path": str(ui_path),
        "final_dialog_screenshot_path": str(dialog_screenshot),
        "final_dialog_ui_dump_path": str(dialog_ui_path),
        "after_publish_screenshot_path": str(after_screenshot),
        "after_publish_ui_dump_path": str(after_ui_path),
        "after_publish_risk_words": risk_found(after_nodes),
        "after_publish_text_sample": visible_text(after_nodes).splitlines()[:40],
    }


def run_once_publish(args: argparse.Namespace) -> None:
    ensure_dirs()
    run_id = now_id()
    adb = Adb(Path(args.adb), args.device, run_id)
    image_remote = push_image(adb, Path(args.image))
    grant_media_permissions(adb)
    launch_mp(adb)
    go_home_if_needed(adb)
    open_sticker_picker(adb)
    select_target_image(adb, Path(args.image))
    fill_title_body(adb, args.title, args.body)
    ui_path, nodes = adb.dump_ui("ready_before_publish")
    screenshot = adb.screenshot("ready_before_publish")
    stop_if_risky(adb, "ready_before_publish", nodes)
    publish_visible = bool(find(nodes, rid="com.tencent.mp:id/btn_action_option") or find(nodes, text="发表", contains=True))
    if not publish_visible:
        raise RuntimeError(f"publish_button_not_visible; screenshot={screenshot}; ui={ui_path}")
    publish_result = tap_publish_sequence(adb, source_state_run_id=run_id)
    result = {
        "mode": "run-once-publish",
        "run_id": run_id,
        "image_remote_path": image_remote,
        "title": args.title,
        "body": args.body,
        "ready_before_publish_screenshot_path": str(screenshot),
        "ready_before_publish_ui_dump_path": str(ui_path),
        "published_clicks_completed": True,
        **publish_result,
    }
    write_jsonl({"event": "run_once_publish_finished", **result})
    print(json.dumps(result, ensure_ascii=False, indent=2))


def confirm_publish(args: argparse.Namespace) -> None:
    if args.confirm.strip() != "可以发表":
        raise SystemExit("refused: --confirm must be exactly 可以发表")
    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    run_id = now_id()
    adb = Adb(Path(args.adb), args.device, run_id)
    adb.run(["connect", adb.device], check=False)
    publish_result = tap_publish_sequence(adb, source_state_run_id=state.get("run_id", ""))
    result = {
        "mode": "confirm-publish",
        "run_id": run_id,
        "source_state_run_id": state.get("run_id"),
        "published_clicks_completed": True,
        **publish_result,
    }
    write_jsonl({"event": "confirm_publish_finished", **result})
    print(json.dumps(result, ensure_ascii=False, indent=2))


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
    return total


def cleanup_sdk(args: argparse.Namespace) -> None:
    targets = [
        ROOT / "tools" / "android-sdk",
        ROOT / "data" / "avd",
        ROOT / "downloads" / "commandlinetools-win-14742923_latest.zip",
        ROOT / "downloads" / "platform-tools-latest-windows.zip",
    ]
    root_resolved = ROOT.resolve()
    rows = []
    for target in targets:
        resolved = target.resolve()
        if root_resolved not in (resolved, *resolved.parents):
            raise SystemExit(f"refused_outside_project={resolved}")
        rows.append({"path": str(resolved), "exists": resolved.exists(), "bytes": dir_size(resolved)})

    if args.execute:
        for row in rows:
            path = Path(row["path"])
            if not path.exists():
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        write_jsonl({"event": "cleanup_sdk_executed", "targets": rows})
    else:
        write_jsonl({"event": "cleanup_sdk_dry_run", "targets": rows})
    print(json.dumps({"execute": args.execute, "targets": rows}, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="WeChat MP Assistant sticker publish helper for MuMu.")
    parser.add_argument("--adb", default=str(DEFAULT_ADB))
    parser.add_argument("--device", default=DEFAULT_DEVICE)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare", help="Create sticker draft and stop before final publish.")
    p.add_argument("--image", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--body", required=True)
    p.set_defaults(func=prepare)

    r = sub.add_parser("run-once-publish", help="Run the full sticker flow once and publish after the final dialog.")
    r.add_argument("--image", required=True)
    r.add_argument("--title", required=True)
    r.add_argument("--body", required=True)
    r.set_defaults(func=run_once_publish)

    f = sub.add_parser("fill-current", help="Fill title/body on the current editor page and stop before publish.")
    f.add_argument("--title", required=True)
    f.add_argument("--body", required=True)
    f.set_defaults(func=finish_current)

    c = sub.add_parser("confirm-publish", help="Click publish only after explicit external confirmation.")
    c.add_argument("--state", required=True)
    c.add_argument("--confirm", required=True)
    c.set_defaults(func=confirm_publish)

    s = sub.add_parser("status", help="Capture current screen and UI dump.")
    s.set_defaults(func=lambda args: status(args))

    clean = sub.add_parser("cleanup-sdk", help="Remove old Android SDK/AVD files under this project.")
    clean.add_argument("--execute", action="store_true")
    clean.set_defaults(func=cleanup_sdk)

    args = parser.parse_args()
    args.func(args)


def status(args: argparse.Namespace) -> None:
    ensure_dirs()
    run_id = now_id()
    adb = Adb(Path(args.adb), args.device, run_id)
    ui_path, nodes = adb.dump_ui("status")
    screenshot = adb.screenshot("status")
    result = {
        "mode": "status",
        "run_id": run_id,
        "screenshot_path": str(screenshot),
        "ui_dump_path": str(ui_path),
        "visible_risk_words": risk_found(nodes),
        "text_sample": visible_text(nodes).splitlines()[:30],
    }
    write_jsonl({"event": "status", **result})
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        ensure_dirs()
        write_jsonl({"event": "error", "error": str(exc)})
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        sys.exit(1)
