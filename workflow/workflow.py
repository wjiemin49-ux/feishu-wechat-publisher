from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from feedback.feedback_router import FeedbackRouter
import xhs_vision_publish_bridge as xhs_vision_bridge
import wechat_mp_publish_bridge as wechat_mp_bridge


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "config.json"
XIAOHONGSHU_ACCOUNT = "myaccount"
DEFAULT_SAU_EXE = Path("D:/me/social-auto-upload/.venv/Scripts/sau.exe")
DEFAULT_SAU_ROOT = Path("D:/me/social-auto-upload")
DEFAULT_WECHAT_PUBLISHER_DIR = Path.home() / ".claude" / "skills" / "wechat-draft-publisher"
DEFAULT_WECHAT_BROWSER_SCRIPT = ROOT / "wechat_browser_publisher.py"
PUBLISH_CONFIRM_TEXT = "请选择发布平台：小红书、微信公众号，还是两个都发布？"
XHS_VISION_DRY_RUN_COMMANDS = {"预检小红书", "小红书预检", "发布小红书", "发小红书"}
WECHAT_MP_PREPARE_COMMANDS = {"预检公众号", "公众号预检", "发布公众号", "发公众号", "发布到公众号", "微信公众号发布", "微信公众号发表"}
USED_IMAGE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}__")
USED_MARKER_RE = re.compile(r"\[USED:(\d{4}-\d{2}-\d{2}-\d{2})\]")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
SECRET_RE = re.compile(
    r"(?i)\b(access[_-]?token|api[_-]?key|authorization|cookie|secret|webhook|session)\b"
    r"(\s*[:=]\s*)([^\s,;\"'}]+)"
)
URL_TOKEN_RE = re.compile(r"(?i)([?&](?:token|access_token|key|secret|signature|code)=)[^&#\s]+")
CHARACTER_RE = re.compile(
    r"^(?P<ordinal>\d+)\.\s+(?P<name>.+?)\s+——\s+《(?P<work>.+?)》(?P<tail>.*)$"
)
COUNT_RE = re.compile(r"([1-9]\d*)\s*篇")
COUNT_WORD_RE = re.compile(r"([一二两三四五六七八九十两])\s*篇")
COUNT_IMAGE_RE = re.compile(r"([1-9]\d*)\s*张\s*(?:图|图片)?")
COUNT_IMAGE_WORD_RE = re.compile(r"([一二两三四五六七八九十两])\s*张\s*(?:图|图片)?")
CHINESE_COUNTS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
CN_INDEX_RE = r"[一二两三四五六七八九十\d]+"

WORKFLOW_PLATFORM_KEYWORDS = (
    "小红书",
    "xhs",
    "公众号",
    "gzh",
    "图文",
)
WORKFLOW_TASK_KEYWORDS = (
    "生图",
    "出图",
    "作图",
    "做图",
    "配图",
    "封面",
    "发文",
    "文章",
    "文案",
    "话题",
    "生成",
    "发",
    "来",
    "做",
    "搞",
)
DIRECT_WORKFLOW_PHRASES = (
    "生成今天文章",
    "发今天文章",
    "重新生成",
    "再来一篇",
    "再来两篇",
    "再来2篇",
)
QUESTION_HINTS = (
    "怎么",
    "如何",
    "为什么",
    "是什么",
    "能不能",
    "可以吗",
    "是不是",
    "?",
    "？",
)
IMAGE_INBOX_KEYWORDS = (
    "图片收件箱",
    "收图",
    "存图",
    "保存图片",
    "图片保存",
    "保存图",
    "图保存",
    "存图片",
    "图片存",
    "把图片保存",
    "把图保存",
    "发图到电脑",
    "传图到电脑",
    "图片传电脑",
    "素材文件夹",
)
XIAOHONGSHU_PLATFORM_KEYWORDS = ("小红书", "xhs", "xiaohongshu")
WECHAT_PLATFORM_KEYWORDS = ("公众号", "微信公众号", "gzh", "wechat", "weixin")
IMAGE_INBOX_LINK_DEFAULT = (
    "https://applink.feishu.cn/client/chat/open?openChatId="
    "oc_3b5bd38ad79e0b3b30ce20dcdf27f41e"
)

for stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        stream.reconfigure(encoding="utf-8", errors="replace")


@dataclasses.dataclass(frozen=True)
class Character:
    ordinal: int
    name: str
    work: str
    line_index: int
    line: str
    used: bool


@dataclasses.dataclass(frozen=True)
class Selection:
    run_id: str
    reference_image: Path
    character: Character
    prompt: str


@dataclasses.dataclass(frozen=True)
class BotIntent:
    kind: str
    count: int = 0
    reason: str = ""
    platforms: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class ManualPublishState:
    stage: str
    created_at: str
    updated_at: str
    platforms: tuple[str, ...] = ()
    image_path: str = ""
    image_key: str = ""
    image_message_id: str = ""
    caption_text: str = ""


class WorkflowError(RuntimeError):
    pass


class MaterialShortageError(WorkflowError):
    pass


class FileLock:
    def __init__(self, path: Path, timeout_seconds: int) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self._fd: int | None = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self._fd = os.open(
                    str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
                payload = {
                    "pid": os.getpid(),
                    "created_at": now_iso("Asia/Shanghai"),
                }
                os.write(self._fd, json.dumps(payload).encode("utf-8"))
                return self
            except FileExistsError:
                owner = self._read_owner()
                if self._owner_is_stale(owner):
                    with contextlib.suppress(FileNotFoundError):
                        self.path.unlink()
                    continue
                if time.monotonic() >= deadline:
                    raise WorkflowError(
                        f"lock timeout: {self.path} ({self._owner_description(owner)})"
                    )
                time.sleep(0.2)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    def _read_owner(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _owner_is_stale(self, owner: dict[str, Any]) -> bool:
        pid = owner.get("pid")
        if not isinstance(pid, int):
            return False
        return not pid_exists(pid)

    def _owner_description(self, owner: dict[str, Any]) -> str:
        if not owner:
            return "owner=unknown"
        pid = owner.get("pid", "unknown")
        created_at = owner.get("created_at", "unknown")
        return f"held_by_pid={pid}, created_at={created_at}"


def pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        if os.name != "nt":
            return False
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        return str(pid) in result.stdout


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        config = json.load(f)
    config["_config_path"] = str(path)
    config["_workspace_root"] = str(infer_workspace_root(path))
    return config


def infer_workspace_root(path: Path) -> Path:
    resolved = path.resolve()
    for candidate in [resolved.parent, *resolved.parents]:
        if (candidate / "workflow" / "workflow.py").exists():
            return candidate
        if (candidate / "work" / "xhs_workflow" / "workflow.py").exists():
            return candidate
    return resolved.parent


def _codex_extension_version(path: Path) -> tuple[int, int, int]:
    match = re.search(r"openai\.chatgpt-(\d+)\.(\d+)\.(\d+)-", str(path))
    if not match:
        return (0, 0, 0)
    return tuple(int(part) for part in match.groups())


def resolve_vs_plugin_codex_exe(configured: str | None) -> str:
    if configured:
        configured_path = Path(configured)
        if configured_path.exists():
            return str(configured_path)
    else:
        configured_path = None

    roots: list[Path] = []
    if configured_path is not None:
        with contextlib.suppress(IndexError):
            roots.append(configured_path.parents[3])
    if os.environ.get("USERPROFILE"):
        roots.extend(
            [
                Path(os.environ["USERPROFILE"]) / ".vscode" / "extensions",
                Path(os.environ["USERPROFILE"]) / ".vscode-insiders" / "extensions",
            ]
        )

    seen: set[Path] = set()
    for root in roots:
        try:
            root = root.resolve()
        except OSError:
            continue
        if root in seen or not root.exists():
            continue
        seen.add(root)
        candidates = [
            path / "bin" / "windows-x86_64" / "codex.exe"
            for path in root.glob("openai.chatgpt-*-win32-x64")
        ]
        existing = [path for path in candidates if path.exists()]
        if existing:
            existing.sort(
                key=lambda path: (_codex_extension_version(path), path.stat().st_mtime),
                reverse=True,
            )
            return str(existing[0])

    return str(configured_path or "")


def now_iso(timezone: str) -> str:
    return dt.datetime.now(resolve_timezone(timezone)).isoformat(timespec="seconds")


def today_string(timezone: str) -> str:
    return dt.datetime.now(resolve_timezone(timezone)).date().isoformat()


def resolve_timezone(timezone: str) -> dt.tzinfo:
    try:
        return ZoneInfo(timezone)
    except Exception:
        if timezone in {"Asia/Shanghai", "China Standard Time", "UTC+08:00", "+08:00"}:
            return dt.timezone(dt.timedelta(hours=8), name="Asia/Shanghai")
        raise


def read_api_key(path: Path) -> str:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            value = line.strip()
            if value:
                return value
    raise WorkflowError(f"empty API key file: {path}")


def api_key_path_ok(config: dict[str, Any]) -> bool:
    path_value = config.get("api_key_path")
    if not path_value:
        return False
    path = Path(path_value)
    return path.exists() and path.stat().st_size > 0


def json_line(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(redact_log_payload(payload), ensure_ascii=False) + "\n")


def compact_text(value: Any, limit: int = 1200) -> str:
    text = redact_sensitive_text(ANSI_RE.sub("", str(value or ""))).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"... [truncated {len(text) - limit} chars]"


def redact_sensitive_text(value: Any) -> str:
    text = str(value or "")
    text = SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
    return URL_TOKEN_RE.sub(lambda m: f"{m.group(1)}[REDACTED]", text)


def redact_log_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: redact_log_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_log_payload(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


def last_json_line(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return {"status": "unreadable", "raw": line}
    return None


def parse_character_pool(path: Path) -> list[Character]:
    lines = path.read_text(encoding="utf-8").splitlines()
    characters: list[Character] = []
    for idx, line in enumerate(lines):
        match = CHARACTER_RE.match(line.strip())
        if not match:
            continue
        tail = match.group("tail") or ""
        characters.append(
            Character(
                ordinal=int(match.group("ordinal")),
                name=match.group("name").strip(),
                work=match.group("work").strip(),
                line_index=idx,
                line=line,
                used=bool(USED_MARKER_RE.search(tail)),
            )
        )
    return characters


def append_character_used_marker(path: Path, character: Character, run_id: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    if character.line_index >= len(lines):
        raise WorkflowError(f"character line no longer exists: {character.name}")
    line = lines[character.line_index]
    if USED_MARKER_RE.search(line):
        raise WorkflowError(f"character already marked used: {line}")
    lines[character.line_index] = f"{line} [USED:{run_id}]"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def list_reference_images(config: dict[str, Any]) -> list[Path]:
    root = Path(config["reference_image_dir"])
    exts = {ext.lower() for ext in config.get("reference_extensions", [])}
    if not root.exists():
        raise WorkflowError(f"reference image dir does not exist: {root}")
    images = [
        path
        for path in root.iterdir()
        if path.is_file()
        and path.suffix.lower() in exts
        and not USED_IMAGE_RE.match(path.name)
    ]
    return sorted(images, key=lambda p: p.name.lower())


def used_run_numbers_for_date(config: dict[str, Any], date_str: str) -> set[int]:
    numbers: set[int] = set()
    image_root = Path(config["reference_image_dir"])
    if image_root.exists():
        for item in image_root.iterdir():
            match = re.match(rf"^{re.escape(date_str)}-(\d{{2}})__", item.name)
            if match:
                numbers.add(int(match.group(1)))

    pool_path = Path(config["character_pool_path"])
    if pool_path.exists():
        for match in USED_MARKER_RE.finditer(pool_path.read_text(encoding="utf-8")):
            marker = match.group(1)
            if marker.startswith(date_str + "-"):
                numbers.add(int(marker[-2:]))
    return numbers


def next_run_ids(config: dict[str, Any], date_str: str, count: int) -> list[str]:
    used = used_run_numbers_for_date(config, date_str)
    start = max(used, default=0) + 1
    return [f"{date_str}-{number:02d}" for number in range(start, start + count)]


def select_materials(config: dict[str, Any], count: int, date_str: str) -> list[Selection]:
    images = list_reference_images(config)
    characters = [c for c in parse_character_pool(Path(config["character_pool_path"])) if not c.used]
    if len(images) < count or len(characters) < count:
        raise MaterialShortageError(
            f"素材不足：可用参考图 {len(images)} 张，可用人物 {len(characters)} 个，请求 {count} 篇"
        )
    run_ids = next_run_ids(config, date_str, count)
    prompt_template = config["image_prompt_template"]
    return [
        Selection(
            run_id=run_ids[i],
            reference_image=images[i],
            character=characters[i],
            prompt=prompt_template.format(character=characters[i].name),
        )
        for i in range(count)
    ]


def normalize_user_text(text: str) -> str:
    compact = re.sub(r"\s+", "", text or "")
    return compact.lower()


def is_xhs_vision_dry_run_command(text: str) -> bool:
    return normalize_user_text(text) in XHS_VISION_DRY_RUN_COMMANDS


def is_wechat_mp_prepare_command(text: str) -> bool:
    return normalize_user_text(text) in WECHAT_MP_PREPARE_COMMANDS


def parse_xhs_vision_confirm_run_id(text: str) -> str | None:
    stripped = (text or "").strip()
    if stripped == "确认发布":
        return ""
    match = re.fullmatch(r"确认发布\s+([A-Za-z0-9_.:-]+)", stripped)
    if match:
        return match.group(1)
    return None


def parse_wechat_mp_confirm_run_id(text: str) -> str | None:
    stripped = (text or "").strip()
    if stripped == "可以发表":
        return ""
    match = re.fullmatch(r"可以发表\s+([A-Za-z0-9_.:-]+)", stripped)
    if match:
        return match.group(1)
    return None


def parse_publish_platforms(text: str) -> tuple[str, ...]:
    compact = normalize_user_text(text)
    if not compact:
        return ()
    both_hit = any(
        phrase in compact
        for phrase in (
            "两个平台都发",
            "两个都发",
            "两边都发",
            "全部都发",
            "都发布",
            "都发",
        )
    ) or ("两个平台" in compact and ("发" in compact or "发布" in compact))
    if both_hit:
        return ("xiaohongshu", "wechat")
    platforms: list[str] = []
    if any(keyword in compact for keyword in XIAOHONGSHU_PLATFORM_KEYWORDS):
        platforms.append("xiaohongshu")
    if any(keyword in compact for keyword in WECHAT_PLATFORM_KEYWORDS):
        platforms.append("wechat")
    return tuple(platforms)


def parse_cn_index(value: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    if value in CHINESE_COUNTS:
        return CHINESE_COUNTS[value]
    if value == "十":
        return 10
    if value.startswith("十") and len(value) == 2:
        return 10 + CHINESE_COUNTS.get(value[1], 0)
    if value.endswith("十") and len(value) == 2:
        return CHINESE_COUNTS.get(value[0], 0) * 10
    if "十" in value:
        left, right = value.split("十", 1)
        tens = CHINESE_COUNTS.get(left, 1 if not left else 0)
        ones = CHINESE_COUNTS.get(right, 0) if right else 0
        return tens * 10 + ones if tens else None
    return None


def parse_publish_selection(text: str) -> dict[str, Any]:
    compact = normalize_user_text(text)
    selection: dict[str, Any] = {
        "image_number": None,
        "caption_number": None,
        "last_image": False,
        "last_caption": False,
        "has_selection": False,
    }
    if not compact:
        return selection
    if "最后一张图" in compact or "最后一张图片" in compact:
        selection["last_image"] = True
        selection["has_selection"] = True
    if "最后一篇文案" in compact or "最后一篇文章" in compact:
        selection["last_caption"] = True
        selection["has_selection"] = True

    image_patterns = (
        rf"第({CN_INDEX_RE})张(?:图|图片)?",
        rf"图({CN_INDEX_RE})",
        rf"图片({CN_INDEX_RE})",
    )
    caption_patterns = (
        rf"第({CN_INDEX_RE})篇(?:文案|文章)?",
        rf"第({CN_INDEX_RE})(?:个)?文案",
        rf"文案({CN_INDEX_RE})",
        rf"文章({CN_INDEX_RE})",
    )
    for pattern in image_patterns:
        match = re.search(pattern, compact)
        if match:
            selection["image_number"] = parse_cn_index(match.group(1))
            selection["has_selection"] = True
            break
    for pattern in caption_patterns:
        match = re.search(pattern, compact)
        if match:
            selection["caption_number"] = parse_cn_index(match.group(1))
            selection["has_selection"] = True
            break
    return selection


def has_publish_selection(text: str) -> bool:
    return bool(parse_publish_selection(text).get("has_selection"))


def is_manual_publish_start(text: str) -> bool:
    compact = normalize_user_text(text)
    if not compact:
        return False
    if has_publish_selection(text):
        return False
    own_image_hit = any(
        phrase in compact
        for phrase in (
            "自己的图片",
            "自己的图",
            "我自己的图片",
            "我自己的图",
            "自己发图片",
            "自己发图",
            "自己的照片",
            "我自己的照片",
            "自己拍的图",
            "自己拍的照片",
            "我拍的图",
            "我拍的照片",
            "手动发图片",
            "手动发图",
            "上传自己的图片",
            "上传自己的图",
            "上传自己的照片",
        )
    )
    image_publish_hit = ("图片" in compact or "图" in compact or "照片" in compact) and any(
        word in compact for word in ("发布", "发送", "发到", "发在", "发小红书", "发公众号")
    )
    return own_image_hit or image_publish_hit


def is_publish_intent(text: str) -> bool:
    compact = normalize_user_text(text)
    if not compact:
        return False
    question_like = any(hint in compact for hint in QUESTION_HINTS)
    command_hit = any(
        phrase in compact
        for phrase in (
            "帮我发布",
            "发布到",
            "发布至",
            "发布去",
            "发布小红书",
            "发布公众号",
            "发到",
            "发去",
            "发上",
            "上传到",
            "推送到",
            "两个平台都发",
            "两个都发",
            "两边都发",
            "都发布",
            "都发",
        )
    )
    if question_like and not command_hit:
        return False
    if command_hit:
        return True
    if has_publish_selection(text) and "发" in compact:
        return True
    platform_hit = parse_publish_platforms(text)
    if platform_hit and any(
        phrase in compact
        for phrase in (
            "发公众号",
            "发微信",
            "发gzh",
            "发小红书",
            "发xhs",
        )
    ):
        return True
    if platform_hit and any(keyword in compact for keyword in ("发布", "上传", "推送")):
        return True
    if "发布" in compact and not question_like:
        return True
    if "平台" in compact and "发" in compact and "生成" not in compact:
        return True
    return False


def requested_count(compact: str, max_count: int = 3) -> int:
    count_match = COUNT_RE.search(compact)
    if count_match:
        return min(max(int(count_match.group(1)), 1), max_count)
    word_match = COUNT_WORD_RE.search(compact)
    if word_match:
        return min(max(CHINESE_COUNTS.get(word_match.group(1), 1), 1), max_count)
    image_count_match = COUNT_IMAGE_RE.search(compact)
    if image_count_match:
        return min(max(int(image_count_match.group(1)), 1), max_count)
    image_word_match = COUNT_IMAGE_WORD_RE.search(compact)
    if image_word_match:
        return min(max(CHINESE_COUNTS.get(image_word_match.group(1), 1), 1), max_count)
    return 1


def detect_bot_intent(text: str, max_count: int = 3) -> BotIntent:
    compact = normalize_user_text(text)
    if not compact:
        return BotIntent("ignore", reason="empty")
    if is_manual_publish_start(text):
        return BotIntent(
            "manual_publish",
            reason="manual_publish_start",
            platforms=parse_publish_platforms(text),
        )
    if is_publish_intent(text):
        return BotIntent(
            "publish",
            reason="publish_intent",
            platforms=parse_publish_platforms(text),
        )
    count = requested_count(compact, max_count)
    has_trigger = "生成" in compact or "发" in compact
    has_subject = (
        "文章" in compact
        or "篇" in compact
        or "张图" in compact
        or "张图片" in compact
        or ("图" in compact and "生成" in compact)
    )
    if has_trigger and has_subject:
        return BotIntent("workflow", count=count, reason="legacy_trigger")
    if any(phrase in compact for phrase in DIRECT_WORKFLOW_PHRASES):
        return BotIntent("workflow", count=count, reason="direct_phrase")
    platform_hit = any(keyword in compact for keyword in WORKFLOW_PLATFORM_KEYWORDS)
    task_hit = any(keyword in compact for keyword in WORKFLOW_TASK_KEYWORDS)
    question_like = any(hint in compact for hint in QUESTION_HINTS)
    if platform_hit and task_hit and not (question_like and "生成" not in compact and "生图" not in compact):
        return BotIntent("workflow", count=count, reason="platform_task")
    if ("生图" in compact or "出图" in compact or "发文" in compact) and not question_like:
        return BotIntent("workflow", count=count, reason="direct_task")
    if any(keyword in compact for keyword in IMAGE_INBOX_KEYWORDS):
        return BotIntent("image_inbox", reason="image_inbox")
    return BotIntent("chat", reason="general_chat")


def should_use_ai_intent_classifier(text: str, deterministic: BotIntent) -> bool:
    compact = normalize_user_text(text)
    if not compact:
        return False
    if deterministic.kind == "publish":
        return True
    if deterministic.kind == "manual_publish":
        return True
    if has_publish_selection(text):
        return True
    manual_upload_hint = any(
        keyword in compact
        for keyword in (
            "自己的",
            "自己拍",
            "我拍",
            "我的图片",
            "我的图",
            "我的照片",
            "照片",
        )
    ) and any(
        keyword in compact
        for keyword in (
            "发",
            "发布",
            "上传",
            "放到",
            "发到",
            "发在",
            "平台",
            "公众号",
            "小红书",
        )
    )
    if manual_upload_hint:
        return True
    if parse_publish_platforms(text):
        return True
    platform_hit = any(keyword in compact for keyword in WORKFLOW_PLATFORM_KEYWORDS)
    action_hit = any(
        keyword in compact
        for keyword in (
            "发",
            "发布",
            "上传",
            "推送",
            "平台",
            "图文",
            "文案",
            "文章",
        )
    )
    return platform_hit and action_hit


def publish_intent_requires_ai(text: str, deterministic: BotIntent) -> bool:
    compact = normalize_user_text(text)
    if not compact:
        return False
    if deterministic.kind in {"publish", "manual_publish"}:
        return True
    if has_publish_selection(text):
        return True
    platform_hit = bool(parse_publish_platforms(text))
    action_hit = any(keyword in compact for keyword in ("发", "发布", "上传", "推送", "平台"))
    return platform_hit and action_hit


def parse_trigger_command(text: str, max_count: int = 3) -> int | None:
    intent = detect_bot_intent(text, max_count)
    return intent.count if intent.kind == "workflow" else None


def message_position(message: dict[str, Any]) -> int:
    value = message.get("message_position") or message.get("position") or "0"
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def is_user_text_message(message: dict[str, Any]) -> bool:
    sender = message.get("sender") or {}
    return message.get("msg_type") == "text" and sender.get("sender_type") == "user"


def is_user_image_message(message: dict[str, Any]) -> bool:
    sender = message.get("sender") or {}
    return message.get("msg_type") == "image" and sender.get("sender_type") == "user"


def extract_feishu_image_key(message: dict[str, Any]) -> str | None:
    content = str(message.get("content") or "")
    match = re.search(r"(img_[A-Za-z0-9_\-]+)", content)
    if match:
        return match.group(1)
    parsed = extract_any_json_object(content)
    if parsed:
        value = parsed.get("image_key") or parsed.get("file_key")
        if value:
            return str(value)
    return None


def local_caption(character: Character) -> dict[str, Any]:
    clean_work = character.work.replace("《", "").replace("》", "")
    return {
        "title": f"{character.name} | {clean_work}",
        "copy": (
            f"{character.name} 的气质很适合这一张图：清晰、鲜明，"
            f"带着《{clean_work}》里一眼能被记住的存在感。"
        ),
        "topics": [
            f"#{character.name}",
            f"#{clean_work}",
            "#动漫",
            "#AI绘画",
            "#AIGC",
            "#小红书图文",
        ],
    }


def normalize_caption(caption: dict[str, Any]) -> dict[str, Any]:
    topics = caption.get("topics") or []
    if isinstance(topics, str):
        topics = topics.split()
    clean_topics = []
    for topic in topics:
        value = str(topic).strip()
        if not value:
            continue
        if not value.startswith("#"):
            value = "#" + value
        clean_topics.append(value)
    return {
        "title": str(caption.get("title") or "").strip(),
        "copy": caption_copy_text(caption.get("copy")),
        "topics": clean_topics[:10],
    }


def caption_copy_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "\n".join(str(item).strip() for item in value if item is not None and str(item).strip())
    return str(value or "").strip()


def caption_text(caption: dict[str, Any]) -> str:
    normalized = normalize_caption(caption)
    topics = " ".join(normalized["topics"])
    parts = [
        normalized["title"],
        normalized["copy"],
        topics,
    ]
    return "\n\n".join(part for part in parts if part).rstrip() + "\n"


def trigger_ack_text(count: int) -> str:
    return (
        f"收到，开始生成 {count} 篇。\n\n"
        "生成图片通常需要 3-8 分钟；完成后会把图片、标题、文案和话题发回这里。"
    )


def publish_ack_text() -> str:
    return "收到，正在处理。"


def scheduled_platforms_from_config(config: dict[str, Any], override: str | None = None) -> tuple[str, ...]:
    raw: Any = override if override is not None else config.get("scheduled_publish_platforms", ["wechat"])
    if isinstance(raw, str):
        parts = re.split(r"[,，\s]+", raw.strip())
    elif isinstance(raw, list):
        parts = [str(item) for item in raw]
    else:
        parts = []
    platforms: list[str] = []
    for item in parts:
        value = item.strip().lower()
        if value in {"wechat", "gzh", "公众号", "weixin"}:
            value = "wechat"
        elif value in {"xhs", "xiaohongshu", "小红书"}:
            value = "xiaohongshu"
        if value in {"wechat", "xiaohongshu"} and value not in platforms:
            platforms.append(value)
    return tuple(platforms or ("wechat",))


def state_expired(state: dict[str, Any] | None, ttl_seconds: int) -> bool:
    if not state or ttl_seconds <= 0:
        return False
    raw = str(state.get("updated_at") or state.get("created_at") or "")
    if not raw:
        return False
    try:
        updated_at = dt.datetime.fromisoformat(raw)
    except ValueError:
        return False
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=resolve_timezone("Asia/Shanghai"))
    return (dt.datetime.now(updated_at.tzinfo) - updated_at).total_seconds() > ttl_seconds


def manual_image_request_text() -> str:
    return "好的，请发图片。"


def manual_caption_request_text() -> str:
    return "收到，请发文案。"


def manual_confirm_text() -> str:
    return "确定发布吗？"


def manual_cancel_text() -> str:
    return "已取消。"


def is_confirm_text(text: str) -> bool:
    compact = normalize_user_text(text)
    return compact in {"确定", "确认", "可以", "行", "好", "好的", "发吧", "发布", "发送"} or (
        "确定" in compact or "确认" in compact
    )


def is_cancel_text(text: str) -> bool:
    compact = normalize_user_text(text)
    return compact in {"取消", "算了", "不发了", "停止", "取消发布", "停止发布"}


def user_failure_reason(error: Any) -> str:
    text = compact_text(error, limit=800)
    lowered = text.lower()
    if "servererror" in lowered or "server error" in lowered:
        return "生图服务返回 ServerError，通常是模型端或额度/限流侧的临时失败。"
    if "rate-limit" in lowered or "rate limited" in lowered or "限流" in text:
        return "模型服务当前限流。"
    if "素材不足" in text:
        return text
    if "lock timeout" in lowered:
        return "工作流锁被占用，可能还有上一轮任务没结束。"
    if "lark-cli failed" in lowered or "bot/user can not be out of the chat" in lowered:
        return "飞书发送或会话权限失败。"
    if text:
        return text[:220].rstrip()
    return "未知错误。"


def failure_notice_text(error: Any) -> str:
    return (
        "这次没有生成成功。\n\n"
        f"原因：{user_failure_reason(error)}\n"
        "素材状态：没有标记已用，下次会继续使用同一张参考图和同一个人物。\n"
        "下一步：可以稍后直接再发“生成今天文章”重试。"
    )


def text_message_content(text: str) -> str:
    return json.dumps({"text": text}, ensure_ascii=False)


def extract_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and {"title", "copy", "topics"} <= obj.keys():
            return obj
    return None


def extract_any_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def extract_response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return str(output_text)
    chunks: list[str] = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                chunks.append(str(text))
    return "\n".join(chunks)


def ai_caption_prompt(character: Character) -> dict[str, str]:
    clean_work = character.work.replace("《", "").replace("》", "")
    return {
        "role": "user",
        "content": (
            "请为小红书/公众号图文生成 JSON。只输出 JSON，字段为 title、copy、topics。\n"
            "title 固定格式：人物名 | 作品名。不要写“的记忆点”，不要加“标题：”。\n"
            "copy 必须是字符串，不要返回数组；只写这个角色最出名、最常被记住的经典原话、短台词或口头禅，一到两句即可。\n"
            "不要写解释，不要写评价，不要写剧情描述，不要写人物分析或“他/她如何如何”的旁白包装。\n"
            "如果不能确定准确原话，就选更短、更常见的口头禅式经典表达；不要伪造长引用。\n"
            "每句必须很短，避免大段原文；整体适合直接复制到小红书/公众号图文。\n"
            "topics 小于等于 10 个，返回数组。\n"
            f"人物：{character.name}\n"
            f"作品：{clean_work}"
        ),
    }


def generate_ai_caption(openai_client: Any, config: dict[str, Any], character: Character) -> dict[str, Any]:
    response = openai_client.responses.create(
        model=config.get("caption_model") or config.get("text_model") or config["general_answer_model"],
        input=[ai_caption_prompt(character)],
        max_output_tokens=int(config.get("caption_max_output_tokens", 500)),
        temperature=float(config.get("caption_temperature", 0.7)),
        timeout=float(config.get("caption_timeout_seconds", 60)),
    )
    response_text = extract_response_text(response)
    parsed = extract_json_object(response_text)
    if not parsed:
        raise WorkflowError(
            "caption model returned non-json text: "
            + (compact_text(response_text, limit=240) or "<empty>")
        )
    caption = normalize_caption(parsed)
    missing = [key for key in ("title", "copy") if not caption.get(key)]
    if missing:
        raise WorkflowError(f"caption model returned incomplete JSON; missing: {', '.join(missing)}")
    return caption


class AITextCaptionClient:
    def __init__(self, config: dict[str, Any]) -> None:
        from openai import OpenAI

        self.config = config
        self.client = OpenAI(api_key=read_api_key(Path(config["api_key_path"])))

    def generate_caption(self, character: Character) -> dict[str, Any]:
        return generate_ai_caption(self.client, self.config, character)


class OpenAIWorkflowClient:
    def __init__(self, config: dict[str, Any]) -> None:
        from openai import OpenAI

        self.config = config
        self.client = OpenAI(api_key=read_api_key(Path(config["api_key_path"])))

    def generate_image(self, selection: Selection, output_path: Path) -> dict[str, Any]:
        started = time.monotonic()
        with selection.reference_image.open("rb") as image_file:
            result = self.client.images.edit(
                model=self.config["image_model"],
                image=image_file,
                prompt=selection.prompt,
                size=self.config["image_size"],
                quality=self.config["image_quality"],
                output_format=self.config["image_output_format"],
                timeout=float(self.config.get("openai_timeout_seconds", 240)),
            )
        b64_json = result.data[0].b64_json
        output_path.write_bytes(base64.b64decode(b64_json))
        return {
            "request_id": getattr(result, "_request_id", None),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }

    def generate_caption(self, selection: Selection) -> dict[str, Any]:
        return generate_ai_caption(self.client, self.config, selection.character)


class GeneralAnswerClient:
    def __init__(self, config: dict[str, Any]) -> None:
        from openai import OpenAI

        self.config = config
        self.client = OpenAI(api_key=read_api_key(Path(config["api_key_path"])))

    def answer(self, text: str) -> str:
        system_prompt = self.config.get("general_answer_system_prompt") or (
            "你是一个飞书私聊里的中文助手。回答要直接、短、可执行。"
            "如果用户是在问小红书、公众号、发文、生图以外的普通问题，"
            "直接回答，不要启动自动生图工作流。"
        )
        response = self.client.responses.create(
            model=self.config.get("general_answer_model") or self.config["text_model"],
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            max_output_tokens=int(self.config.get("general_answer_max_output_tokens", 600)),
            temperature=float(self.config.get("general_answer_temperature", 0.4)),
            timeout=float(self.config.get("general_answer_timeout_seconds", 45)),
        )
        answer = compact_text(extract_response_text(response), limit=1800)
        if not answer:
            raise WorkflowError("general answer model returned empty text")
        return answer


def normalize_deepseek_model(model: str | None) -> str:
    value = str(model or "deepseek-v4-flash").strip()
    aliases = {
        "v4-flash": "deepseek-v4-flash",
        "flash": "deepseek-v4-flash",
        "v4-pro": "deepseek-v4-pro",
        "pro": "deepseek-v4-pro",
    }
    return aliases.get(value.lower(), value)


def extract_chat_completion_text(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    return str(content or "")


class DeepSeekGeneralAnswerClient:
    def __init__(self, config: dict[str, Any]) -> None:
        from openai import OpenAI

        self.config = config
        self.model = normalize_deepseek_model(config.get("general_answer_model"))
        api_key_path = Path(config.get("general_answer_api_key_path") or config["api_key_path"])
        self.client = OpenAI(
            api_key=read_api_key(api_key_path),
            base_url=str(config.get("general_answer_base_url") or "https://api.deepseek.com"),
        )

    def answer(self, text: str) -> str:
        system_prompt = self.config.get("general_answer_system_prompt") or (
            "你是一个飞书私聊里的中文助手。回答要直接、短、可执行。"
        )
        thinking = str(self.config.get("general_answer_deepseek_thinking", "disabled")).strip().lower()
        extra_body = {"thinking": {"type": thinking}} if thinking in {"enabled", "disabled"} else None
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            "max_tokens": int(self.config.get("general_answer_max_output_tokens", 600)),
            "temperature": float(self.config.get("general_answer_temperature", 0.4)),
            "stream": False,
            "timeout": float(self.config.get("general_answer_timeout_seconds", 45)),
        }
        if extra_body:
            kwargs["extra_body"] = extra_body
        response = self.client.chat.completions.create(**kwargs)
        answer = compact_text(extract_chat_completion_text(response), limit=1800)
        if not answer:
            raise WorkflowError("general answer model returned empty text")
        return answer


def intent_classifier_prompt(text: str, max_count: int) -> list[dict[str, str]]:
    system = (
        "你是飞书自动化工作流的意图分类器，只输出 JSON，不要解释。\n"
        "kind 只能是 publish、manual_publish、workflow、image_inbox、chat、ignore。\n"
        "publish=用户想把最近已生成的图片/文案发布到小红书或公众号。\n"
        "manual_publish=用户想发布自己接下来上传的图片，或者说想发自己的图片。\n"
        "workflow=用户想新生成图片、文案或文章。\n"
        "image_inbox=用户想保存/传图片到电脑图片收件箱。\n"
        "chat=普通聊天或咨询。\n"
        "platforms 只能包含 xiaohongshu、wechat；平台不明确则空数组。\n"
        f"count 是 workflow 生成数量，范围 1 到 {max_count}；不是 workflow 时为 0。\n"
        "重要例子：\n"
        "发公众号 => publish, platforms=[wechat]\n"
        "发小红书 => publish, platforms=[xiaohongshu]\n"
        "帮我发在两个平台上 => publish, platforms=[xiaohongshu,wechat]\n"
        "图2配文案1发公众号 => publish, platforms=[wechat]\n"
        "我想发自己的图片 => manual_publish, platforms=[]\n"
        "我想把自己的图片发公众号 => manual_publish, platforms=[wechat]\n"
        "我拍的照片想放到公众号 => manual_publish, platforms=[wechat]\n"
        "生成3张图 => workflow, count=3\n"
        "生成一篇公众号图文 => workflow, count=1\n"
        "公众号怎么发比较好 => chat\n"
        "返回格式：{\"kind\":\"publish\",\"platforms\":[\"wechat\"],\"count\":0,\"reason\":\"...\"}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": text},
    ]


class DeepSeekIntentClassifierClient:
    def __init__(self, config: dict[str, Any]) -> None:
        from openai import OpenAI

        self.config = config
        self.model = normalize_deepseek_model(
            config.get("intent_classifier_model") or config.get("general_answer_model")
        )
        api_key_path = Path(config.get("general_answer_api_key_path") or config["api_key_path"])
        self.client = OpenAI(
            api_key=read_api_key(api_key_path),
            base_url=str(config.get("general_answer_base_url") or "https://api.deepseek.com"),
        )

    def classify(self, text: str, max_count: int) -> BotIntent:
        thinking = str(self.config.get("general_answer_deepseek_thinking", "disabled")).strip().lower()
        extra_body = {"thinking": {"type": thinking}} if thinking in {"enabled", "disabled"} else None
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": intent_classifier_prompt(text, max_count),
            "max_tokens": int(self.config.get("intent_classifier_max_tokens", 180)),
            "temperature": float(self.config.get("intent_classifier_temperature", 0)),
            "stream": False,
            "timeout": float(self.config.get("intent_classifier_timeout_seconds", 12)),
        }
        if extra_body:
            kwargs["extra_body"] = extra_body
        response = self.client.chat.completions.create(**kwargs)
        response_text = extract_chat_completion_text(response)
        parsed = extract_any_json_object(response_text)
        if not parsed:
            raise WorkflowError(
                "intent classifier returned non-json text: "
                + (compact_text(response_text, limit=240) or "<empty>")
            )
        kind = str(parsed.get("kind") or "").strip().lower()
        if kind not in {"publish", "manual_publish", "workflow", "image_inbox", "chat", "ignore"}:
            raise WorkflowError(f"intent classifier returned invalid kind: {kind}")
        raw_platforms = parsed.get("platforms") or []
        platforms = tuple(
            platform
            for platform in raw_platforms
            if platform in {"xiaohongshu", "wechat"}
        )
        count = 0
        if kind == "workflow":
            try:
                count = min(max(int(parsed.get("count") or 1), 1), max_count)
            except (TypeError, ValueError):
                count = 1
        return BotIntent(
            kind=kind,
            count=count,
            reason=f"ai_intent:{parsed.get('reason') or ''}",
            platforms=platforms,
        )


class MockGeneralAnswerClient:
    def answer(self, text: str) -> str:
        return f"普通回答：{text}"


def general_answer_client_from_config(config: dict[str, Any]) -> Any:
    provider = str(config.get("general_answer_provider", "openai")).strip().lower()
    if provider in {"openai", "api"}:
        return GeneralAnswerClient(config)
    if provider in {"deepseek", "deepseek-api"}:
        return DeepSeekGeneralAnswerClient(config)
    if provider == "mock":
        return MockGeneralAnswerClient()
    raise WorkflowError(f"unknown general_answer_provider: {provider}")


def general_answer_failure_text(error: Any) -> str:
    return (
        "普通回答暂时不可用。\n\n"
        f"原因：{user_failure_reason(error)}\n"
        "小红书/公众号生图发文工作流不受这个普通回答失败影响。"
    )


def image_inbox_reply_text(config: dict[str, Any]) -> str:
    link = str(config.get("image_inbox_chat_link") or IMAGE_INBOX_LINK_DEFAULT)
    return (
        "这个属于图片收件箱。\n"
        "请把图片或 jpg/png/webp/gif 文件发到图片收件箱 bot：\n"
        f"{link}"
    )


class VSPluginWorkflowClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        workspace = Path(config.get("_workspace_root") or Path.cwd()).resolve()
        self.workspace = workspace
        self._caption_client: Any | None = None
        self.script_path = Path(
            config.get("vs_plugin_probe_script")
            or workspace / "vs_plugin_route_probe.ps1"
        )
        self.codex_home = str(config.get("vs_plugin_codex_home") or os.environ.get("CODEX_HOME") or "")
        configured_codex_exe = config.get("vs_plugin_codex_exe")
        self.codex_exe = resolve_vs_plugin_codex_exe(configured_codex_exe)
        if not self.codex_exe:
            self.codex_exe = shutil.which("codex") or ""

    def generate_image(self, selection: Selection, output_path: Path) -> dict[str, Any]:
        if not self.script_path.exists():
            raise WorkflowError(f"VS plugin probe script not found: {self.script_path}")
        started = time.monotonic()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        log_path = output_path.parent / "vs_plugin_events.jsonl"
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if not powershell:
            raise WorkflowError("PowerShell executable not found for VS plugin route")
        command = [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.script_path),
            "-ReferenceImage",
            str(selection.reference_image),
            "-Prompt",
            selection.prompt,
            "-OutputImage",
            str(output_path),
            "-JsonlLog",
            str(log_path),
            "-CodexHome",
            self.codex_home,
            "-CodexExe",
            self.codex_exe,
            "-Workspace",
            str(self.workspace),
            "-NoOutputTimeoutSeconds",
            str(int(self.config.get("vs_plugin_no_output_timeout_seconds", 600))),
        ]
        result = subprocess.run(
            command,
            cwd=str(self.workspace),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=int(self.config.get("vs_plugin_timeout_seconds", 900)),
            check=False,
        )
        if result.returncode != 0:
            raise WorkflowError(
                "VS plugin image generation failed "
                f"({result.returncode}): {result.stderr.strip() or result.stdout.strip()}"
            )
        try:
            summary = parse_json_output(result.stdout)
        except Exception as exc:
            raise WorkflowError(f"VS plugin probe returned unreadable output: {exc}") from exc
        if not summary.get("ok"):
            details = [
                "VS plugin image generation did not produce an image",
                f"thread_id={summary.get('threadId')}",
                f"turn_completed={summary.get('turnCompleted')}",
                f"turn_failed={summary.get('turnFailed')}",
            ]
            agent_final = compact_text(summary.get("agentFinalText"), limit=800)
            if agent_final:
                details.append(f"agent_final={agent_final}")
            stderr = compact_text(summary.get("stderr"), limit=500)
            if stderr:
                details.append(f"stderr={stderr}")
            raise WorkflowError("; ".join(details))
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise WorkflowError(f"VS plugin reported success but image is missing: {output_path}")
        image_result = summary.get("imageResult") or {}
        saved = image_result.get("saved") or {}
        return {
            "provider": "vs_plugin",
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "thread_id": summary.get("threadId"),
            "events_log": str(log_path),
            "saved_source": saved.get("source"),
            "saved_value": saved.get("value"),
            "image_status": image_result.get("status"),
            "revised_prompt": image_result.get("revisedPrompt"),
            "agent_final_text": compact_text(summary.get("agentFinalText"), limit=800),
        }

    def generate_caption(self, selection: Selection) -> dict[str, Any]:
        if self._caption_client is None:
            self._caption_client = AITextCaptionClient(self.config)
        return self._caption_client.generate_caption(selection.character)


class MockOpenAIWorkflowClient:
    PNG_BYTES = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )

    def generate_image(self, selection: Selection, output_path: Path) -> dict[str, Any]:
        output_path.write_bytes(self.PNG_BYTES)
        return {"request_id": f"mock-openai-{selection.run_id}", "elapsed_seconds": 0.0}

    def generate_caption(self, selection: Selection) -> dict[str, Any]:
        return local_caption(selection.character)


def workflow_client_from_config(config: dict[str, Any]) -> Any:
    provider = str(config.get("image_provider", "openai")).strip().lower()
    if provider in {"openai", "api"}:
        return OpenAIWorkflowClient(config)
    if provider in {"vs_plugin", "vscode", "codex"}:
        return VSPluginWorkflowClient(config)
    if provider == "mock":
        return MockOpenAIWorkflowClient()
    raise WorkflowError(f"unknown image_provider: {provider}")


def lark_command_prefix(profile: str | None = None) -> list[str]:
    cmd = shutil.which("lark-cli")
    if not cmd:
        raise WorkflowError("lark-cli not found on PATH")
    if cmd.lower().endswith((".cmd", ".bat")):
        script = Path(cmd).parent / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js"
        node = shutil.which("node")
        if script.exists() and node:
            prefix = [node, str(script)]
            if profile:
                prefix += ["--profile", profile]
            return prefix
    if cmd.lower().endswith(".ps1"):
        cmd_sibling = Path(cmd).with_suffix(".cmd")
        if cmd_sibling.exists():
            script = cmd_sibling.parent / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js"
            node = shutil.which("node")
            if script.exists() and node:
                prefix = [node, str(script)]
                if profile:
                    prefix += ["--profile", profile]
                return prefix
            prefix = ["cmd.exe", "/c", str(cmd_sibling)]
            if profile:
                prefix += ["--profile", profile]
            return prefix
    if cmd.lower().endswith(".ps1"):
        prefix = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", cmd]
        if profile:
            prefix += ["--profile", profile]
        return prefix
    prefix = [cmd]
    if profile:
        prefix += ["--profile", profile]
    return prefix


def parse_json_output(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def find_nested_key(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = find_nested_key(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_nested_key(value, key)
            if found is not None:
                return found
    return None


class FeishuClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.cwd = Path(config.get("_workspace_root") or Path.cwd()).resolve()

    def read_as(self) -> str:
        return str(self.config.get("feishu_read_as") or "user")

    def _run(self, args: list[str], timeout: int | None = None, cwd: Path | None = None) -> dict[str, Any]:
        command = lark_command_prefix(self.config.get("feishu_profile")) + args
        result = subprocess.run(
            command,
            cwd=str((cwd or self.cwd).resolve()),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout or int(self.config.get("lark_timeout_seconds", 60)),
            check=False,
        )
        if result.returncode != 0:
            raise WorkflowError(
                f"lark-cli failed ({result.returncode}): {result.stderr.strip() or result.stdout.strip()}"
            )
        return parse_json_output(result.stdout)

    def _relative_to_cwd(self, path: Path) -> str:
        resolved = path.resolve()
        rel = os.path.relpath(resolved, self.cwd)
        if rel.startswith("..") or Path(rel).is_absolute():
            raise WorkflowError(
                f"local Feishu upload path must be inside workspace cwd {self.cwd}: {resolved}"
            )
        return rel

    def upload_image(self, path: Path) -> str:
        upload_path = self._relative_to_cwd(path)
        data = self._run(
            [
                "im",
                "images",
                "create",
                "--as",
                "bot",
                "--data",
                "{\"image_type\":\"message\"}",
                "--file",
                f"image={upload_path}",
                "--json",
            ]
        )
        image_key = find_nested_key(data, "image_key")
        if not image_key:
            raise WorkflowError(f"image_key missing from lark upload response: {data}")
        return str(image_key)

    def upload_local_image(self, path: Path) -> str:
        resolved = path.resolve()
        if not resolved.exists() or not resolved.is_file():
            raise WorkflowError(f"local image missing: {resolved}")
        data = self._run(
            [
                "im",
                "images",
                "create",
                "--as",
                "bot",
                "--data",
                "{\"image_type\":\"message\"}",
                "--file",
                f"image={resolved.name}",
                "--json",
            ],
            cwd=resolved.parent,
        )
        image_key = find_nested_key(data, "image_key")
        if not image_key:
            raise WorkflowError(f"image_key missing from lark upload response: {data}")
        return str(image_key)

    def send_local_image_with_text(self, image_path: Path, text: str) -> dict[str, Any]:
        send_as = self.config.get("feishu_send_as", "bot")
        image_key = self.upload_local_image(image_path)
        image_rsp = self._run(
            [
                "im",
                "+messages-send",
                "--as",
                send_as,
                "--chat-id",
                self.config["feishu_chat_id"],
                "--image",
                image_key,
                "--json",
            ]
        )
        text_rsp = self.send_text(text)
        return {
            "image_key": image_key,
            "image_message_id": find_nested_key(image_rsp, "message_id"),
            "text_message_id": find_nested_key(text_rsp, "message_id"),
            "caption_message_type": "text",
            "identity": send_as,
        }

    def send_post(self, image_path: Path, text: str) -> dict[str, Any]:
        send_as = self.config.get("feishu_send_as", "bot")
        if send_as == "user":
            image_key = self.upload_image(image_path)
            image_rsp = self._run(
                [
                    "im",
                    "+messages-send",
                    "--as",
                    "user",
                    "--chat-id",
                    self.config["feishu_chat_id"],
                    "--image",
                    image_key,
                    "--json",
                ]
            )
            text_rsp = self.send_text(text)
            return {
                "image_key": image_key,
                "image_message_id": find_nested_key(image_rsp, "message_id"),
                "text_message_id": find_nested_key(text_rsp, "message_id"),
                "caption_message_type": "text",
                "identity": "user",
            }

        image_key = self.upload_image(image_path)
        image_rsp = self._run(
            [
                "im",
                "+messages-send",
                "--as",
                "bot",
                "--chat-id",
                self.config["feishu_chat_id"],
                "--image",
                image_key,
                "--json",
                ]
            )
        text_rsp = self.send_text(text)
        return {
            "image_key": image_key,
            "image_message_id": find_nested_key(image_rsp, "message_id"),
            "text_message_id": find_nested_key(text_rsp, "message_id"),
            "caption_message_type": "text",
            "identity": "bot",
        }

    def send_text(self, text: str) -> dict[str, Any]:
        send_as = self.config.get("feishu_send_as", "bot")
        return self._run(
            [
                "im",
                "+messages-send",
                "--as",
                send_as,
                "--chat-id",
                self.config["feishu_chat_id"],
                "--msg-type",
                "text",
                "--content",
                text_message_content(text),
                "--json",
            ]
        )

    def download_message_image(self, message_id: str, image_key: str, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_rel = self._relative_to_cwd(output_path)
        data = self._run(
            [
                "im",
                "+messages-resources-download",
                "--as",
                self.config.get("feishu_send_as", "bot"),
                "--message-id",
                message_id,
                "--file-key",
                image_key,
                "--type",
                "image",
                "--output",
                output_rel,
                "--json",
            ],
            timeout=int(self.config.get("lark_timeout_seconds", 60)),
        )
        if output_path.exists() and output_path.stat().st_size > 0:
            return output_path
        stem_matches = sorted(output_path.parent.glob(output_path.stem + "*"))
        for match in stem_matches:
            if match.is_file() and match.stat().st_size > 0:
                return match
        raise WorkflowError(f"Feishu image download finished but file is missing: {data}")

    def dry_run_text(self, text: str = "doctor dry-run") -> dict[str, Any]:
        return self._run(
            [
                "im",
                "+messages-send",
                "--as",
                self.config.get("feishu_send_as", "bot"),
                "--chat-id",
                self.config["feishu_chat_id"],
                "--msg-type",
                "text",
                "--content",
                text_message_content(text),
                "--dry-run",
                "--json",
            ]
        )

    def list_recent_messages(self, page_size: int = 20) -> list[dict[str, Any]]:
        data = self._run(
            [
                "im",
                "+chat-messages-list",
                "--as",
                self.read_as(),
                "--chat-id",
                self.config["feishu_chat_id"],
                "--page-size",
                str(page_size),
                "--sort",
                "desc",
                "--no-reactions",
                "--json",
            ]
        )
        messages = data.get("data", {}).get("messages") or []
        return messages if isinstance(messages, list) else []

    def chat_info(self) -> dict[str, Any]:
        data = self._run(
            [
                "im",
                "chats",
                "get",
                "--as",
                self.read_as(),
                "--params",
                json.dumps({"chat_id": self.config["feishu_chat_id"]}, ensure_ascii=False),
                "--json",
            ]
        )
        return data.get("data", {}) if isinstance(data, dict) else {}

    def chat_bots(self) -> list[dict[str, Any]]:
        data = self._run(
            [
                "im",
                "chat.members",
                "bots",
                "--as",
                self.read_as(),
                "--params",
                json.dumps({"chat_id": self.config["feishu_chat_id"]}, ensure_ascii=False),
                "--json",
            ]
        )
        items = data.get("data", {}).get("items") if isinstance(data, dict) else []
        return items if isinstance(items, list) else []

    def event_inventory_summary(self) -> dict[str, Any]:
        result = subprocess.run(
            lark_command_prefix(self.config.get("feishu_profile")) + ["event", "list", "--json"],
            cwd=str(self.cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=int(self.config.get("lark_timeout_seconds", 60)),
            check=False,
        )
        if result.returncode != 0:
            raise WorkflowError(
                f"lark-cli event list failed ({result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        events = json.loads(result.stdout.strip())
        if not isinstance(events, list):
            raise WorkflowError("lark-cli event list returned non-list JSON")
        groups: dict[str, int] = {}
        for event in events:
            key = str(event.get("key") or "") if isinstance(event, dict) else ""
            group = key.split(".", 1)[0] if key else "unknown"
            groups[group] = groups.get(group, 0) + 1
        return {
            "event_count": len(events),
            "groups": groups,
            "active_receive_event": "im.message.receive_v1",
        }


class MockFeishuClient:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def send_post(self, image_path: Path, text: str) -> dict[str, Any]:
        if self.fail:
            raise WorkflowError("mock Feishu send failure")
        return {
            "image_key": f"mock-image-{uuid.uuid4().hex[:8]}",
            "image_message_id": f"mock-image-message-{uuid.uuid4().hex[:8]}",
            "text_message_id": f"mock-text-message-{uuid.uuid4().hex[:8]}",
            "caption_message_type": "text",
        }

    def send_local_image_with_text(self, image_path: Path, text: str) -> dict[str, Any]:
        return self.send_post(image_path, text)

    def send_text(self, text: str) -> dict[str, Any]:
        if self.fail:
            raise WorkflowError("mock Feishu send failure")
        return {"text_message_id": f"mock-text-message-{uuid.uuid4().hex[:8]}"}


def safe_publish_title(value: str, fallback: str = "图文") -> str:
    title = " ".join((value or "").strip().split())
    if not title:
        title = fallback
    return title[:64]


def split_caption_text_for_publish(text: str) -> dict[str, Any]:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    title = safe_publish_title(lines[0] if lines else "")
    tags = re.findall(r"#([^\s#]+)", text or "")
    note_lines = lines[1:]
    if note_lines and re.fullmatch(r"(#[^\s#]+)(\s+#[^\s#]+)*", note_lines[-1]):
        note_lines = note_lines[:-1]
    note = "\n".join(note_lines).strip()
    return {
        "title": title,
        "note": note,
        "tags": [tag.strip() for tag in tags if tag.strip()][:10],
        "text": text,
    }


def publish_content_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    caption = metadata.get("caption") if isinstance(metadata.get("caption"), dict) else {}
    if caption:
        normalized = normalize_caption(caption)
        text = caption_text(normalized)
        return {
            "title": safe_publish_title(normalized.get("title", "")),
            "note": normalized.get("copy", ""),
            "tags": [topic.lstrip("#") for topic in normalized.get("topics", [])][:10],
            "text": text,
        }
    caption_path = metadata.get("caption_path")
    if caption_path and Path(caption_path).exists():
        return split_caption_text_for_publish(Path(caption_path).read_text(encoding="utf-8"))
    return split_caption_text_for_publish("")


def publish_candidate_from_metadata(metadata: dict[str, Any]) -> dict[str, Any] | None:
    status = metadata.get("status")
    if status not in {"completed", "completed_uncommitted"}:
        return None
    image_path = metadata.get("image")
    if not image_path:
        return None
    candidate = {
        "run_id": metadata.get("run_id"),
        "image": image_path,
        "caption_path": metadata.get("caption_path"),
        "metadata_path": metadata.get("metadata_path"),
        "created_at": metadata.get("finished_at") or metadata.get("started_at"),
        "character": metadata.get("character"),
        "publish": publish_content_from_metadata(metadata),
    }
    return candidate


def latest_candidate_pool_from_results(
    results: list[dict[str, Any]], timezone: str
) -> dict[str, Any] | None:
    completed = [result for result in results if publish_candidate_from_metadata(result)]
    if not completed:
        return None
    images: list[dict[str, Any]] = []
    captions: list[dict[str, Any]] = []
    for index, metadata in enumerate(completed, start=1):
        candidate = publish_candidate_from_metadata(metadata)
        if not candidate:
            continue
        images.append(
            {
                "number": index,
                "run_id": metadata.get("run_id"),
                "path": candidate["image"],
                "metadata_path": candidate.get("metadata_path"),
                "created_at": candidate.get("created_at"),
                "character": candidate.get("character"),
            }
        )
        captions.append(
            {
                "number": index,
                "run_id": metadata.get("run_id"),
                "caption_path": candidate.get("caption_path"),
                "content": candidate["publish"].get("text", ""),
                "publish": candidate["publish"],
                "created_at": candidate.get("created_at"),
                "character": candidate.get("character"),
            }
        )
    if not images or not captions:
        return None
    first_run_id = str(completed[0].get("run_id") or "batch")
    last_run_id = str(completed[-1].get("run_id") or first_run_id)
    batch_id = first_run_id if first_run_id == last_run_id else f"{first_run_id}_to_{last_run_id}"
    return {
        "batch_id": batch_id,
        "created_at": now_iso(timezone),
        "images": images,
        "captions": captions,
        "default_image_number": images[-1]["number"],
        "default_caption_number": captions[0]["number"],
    }


def numbered_item(items: list[dict[str, Any]], number: int) -> dict[str, Any] | None:
    for item in items:
        if item.get("number") == number:
            return item
    return None


def candidate_from_pool(
    pool: dict[str, Any] | None,
    image_number: int | None = None,
    caption_number: int | None = None,
) -> dict[str, Any] | None:
    if not pool:
        return None
    images = pool.get("images") if isinstance(pool.get("images"), list) else []
    captions = pool.get("captions") if isinstance(pool.get("captions"), list) else []
    if not images or not captions:
        return None
    image_number = int(image_number or pool.get("default_image_number") or images[-1]["number"])
    caption_number = int(caption_number or pool.get("default_caption_number") or captions[0]["number"])
    image = numbered_item(images, image_number)
    caption = numbered_item(captions, caption_number)
    if not image or not caption:
        return None
    return {
        "run_id": image.get("run_id"),
        "batch_id": pool.get("batch_id"),
        "image": image.get("path"),
        "caption_path": caption.get("caption_path"),
        "metadata_path": image.get("metadata_path"),
        "created_at": pool.get("created_at") or image.get("created_at") or caption.get("created_at"),
        "character": image.get("character") or caption.get("character"),
        "publish": caption.get("publish") or split_caption_text_for_publish(caption.get("content", "")),
        "selection": {
            "image_number": image_number,
            "caption_number": caption_number,
            "image_run_id": image.get("run_id"),
            "caption_run_id": caption.get("run_id"),
        },
    }


def candidate_pool_summary_text(pool: dict[str, Any]) -> str:
    images = pool.get("images") if isinstance(pool.get("images"), list) else []
    captions = pool.get("captions") if isinstance(pool.get("captions"), list) else []
    image_labels = "、".join(f"图 {item['number']}" for item in images)
    caption_labels = "、".join(f"文案 {item['number']}" for item in captions)
    return (
        "本次生成已编号，可以直接反馈。\n"
        f"图片：{image_labels}\n"
        f"文案：{caption_labels}\n"
        f"当前默认：图 {pool.get('default_image_number')} + 文案 {pool.get('default_caption_number')}\n"
        "可回复：用第2张图 / 用第3张图 + 第1段文案 / 第2张图废掉\n"
        "也可以：文案太硬，重写口语一点 / 这张脸不像，重生成 / 这个风格好，下次多用\n"
        "入队：加入待发布 / 发布公众号 / 发布小红书\n"
        "想看完整指令：反馈帮助"
    )


def resolve_pool_selection(
    pool: dict[str, Any] | None,
    selection: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if not pool:
        return None, "没有找到刚生成的图片和文案。"
    images = pool.get("images") if isinstance(pool.get("images"), list) else []
    captions = pool.get("captions") if isinstance(pool.get("captions"), list) else []
    if not images or not captions:
        return None, "没有找到刚生成的图片和文案。"

    image_number = selection.get("image_number")
    caption_number = selection.get("caption_number")
    has_selection = bool(selection.get("has_selection"))
    if selection.get("last_image"):
        image_number = images[-1]["number"]
    if selection.get("last_caption"):
        caption_number = captions[-1]["number"]

    if has_selection:
        if image_number is None and len(images) == 1:
            image_number = images[0]["number"]
        if caption_number is None and len(captions) == 1:
            caption_number = captions[0]["number"]
        if image_number is None and len(images) > 1:
            return None, "请选择图片编号。"
        if caption_number is None and len(captions) > 1:
            return None, "请选择文案编号。"
    else:
        image_number = image_number or pool.get("default_image_number") or images[-1]["number"]
        caption_number = caption_number or pool.get("default_caption_number") or captions[0]["number"]

    if image_number is not None and not numbered_item(images, int(image_number)):
        return None, f"没有图 {image_number}。"
    if caption_number is not None and not numbered_item(captions, int(caption_number)):
        return None, f"没有文案 {caption_number}。"
    candidate = candidate_from_pool(pool, image_number, caption_number)
    reason = validate_publish_candidate(candidate)
    return candidate, reason


def validate_publish_candidate(candidate: dict[str, Any] | None) -> str | None:
    if not candidate:
        return "没有找到刚生成的图片和文案。"
    image_path = Path(str(candidate.get("image") or ""))
    if not image_path.exists() or image_path.stat().st_size == 0:
        return "没有找到刚生成的图片。"
    publish = candidate.get("publish") if isinstance(candidate.get("publish"), dict) else {}
    if not publish.get("title") and not publish.get("note") and not publish.get("text"):
        return "没有找到刚生成的文案。"
    return None


def platform_label(platform: str) -> str:
    return {"xiaohongshu": "小红书", "wechat": "公众号"}.get(platform, platform)


def publish_reply_text(results: list[dict[str, Any]]) -> str:
    if results and all(result.get("ok") for result in results):
        return "提交完成，请以平台页面为准。"
    failed = [result for result in results if not result.get("ok")]
    if not failed:
        return "发布失败：未知原因。"
    reason = str(failed[0].get("reason") or "未知原因。").strip()
    if not reason.endswith("。"):
        reason += "。"
    succeeded = [platform_label(result["platform"]) for result in results if result.get("ok")]
    suffix = f"{'、'.join(succeeded)}已成功。" if succeeded else ""
    return f"发布失败：{reason}{suffix}"


def classify_xiaohongshu_failure(text: str) -> str:
    lowered = text.lower()
    if any(token in lowered for token in ("cookie", "login", "expired", "invalid")):
        return "小红书登录态失效。"
    if "not found" in lowered or "no such file" in lowered:
        return "小红书发布工具不可用。"
    return "小红书发布失败。"


def classify_wechat_failure(text: str) -> str:
    if "EOF when reading a line" in text or "首次使用需要配置" in text:
        return "公众号配置缺失。"
    if "40164" in text or "白名单" in text:
        return "公众号 IP 白名单未通过。"
    if "48001" in text or "api功能未授权" in text:
        return "公众号接口未授权。"
    if "45009" in text or "调用超过限制" in text:
        return "公众号接口调用次数已达上限。"
    if "media_id" in text.lower():
        return "公众号贴图 media_id 无效。"
    lowered = text.lower()
    invalid_credential_hit = (
        "40001" in text
        or "40013" in text
        or "40125" in text
        or "invalid appid" in lowered
        or "invalid appsecret" in lowered
        or "不合法的appid" in text
        or "无效的appsecret" in text
        or "请在配置文件中填写有效的appid" in text
        or "请在配置文件中填写有效的appsecret" in text
        or "配置文件格式错误" in text
    )
    if invalid_credential_hit:
        return "公众号配置无效。"
    if "请手动创建配置文件" in text or "config file" in lowered:
        return "公众号配置缺失。"
    return "公众号贴图发布失败。"


def classify_wechat_browser_failure(text: str) -> str:
    if "未登录" in text or "登录超时" in text:
        return "公众号后台未登录。"
    if "扫码确认超时" in text:
        return "公众号扫码确认超时。"
    if "Chrome not found" in text or "找不到 Chrome" in text:
        return "本地 Chrome 不可用。"
    if "没有找到" in text or "not found" in text.lower():
        return "公众号后台页面结构变化。"
    if "图片上传超时" in text:
        return "公众号图片上传超时。"
    return "公众号浏览器发布失败。"


def parse_browser_publish_result(stdout: str) -> dict[str, Any] | None:
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return extract_any_json_object(stdout or "")


def wechat_browser_content_from_publish(publish: dict[str, Any]) -> str:
    note = str(publish.get("note") or "").strip()
    tags = [str(tag).strip().lstrip("#") for tag in publish.get("tags", []) if str(tag).strip()]
    if note:
        parts = [note]
    else:
        text = str(publish.get("text") or "").strip()
        parts = [text] if text else []
    if tags:
        parts.append(" ".join(f"#{tag}" for tag in tags[:10]))
    return "\n\n".join(part for part in parts if part).strip()


class LocalPublisher:
    def __init__(self, config: dict[str, Any], logger: Any) -> None:
        self.config = config
        self.logger = logger

    def publish(self, platform: str, candidate: dict[str, Any]) -> dict[str, Any]:
        if platform == "xiaohongshu":
            return self.publish_xiaohongshu(candidate)
        if platform == "wechat":
            return self.publish_wechat_sticker(candidate)
        return {"platform": platform, "ok": False, "reason": "未知发布平台。"}

    def _run_command(
        self,
        command: list[str],
        cwd: Path,
        timeout: int,
        event: str,
    ) -> subprocess.CompletedProcess[str]:
        self.logger(
            {
                "event": event,
                "command": command,
                "cwd": str(cwd),
            }
        )
        result = subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        self.logger(
            {
                "event": f"{event}_result",
                "returncode": result.returncode,
                "stdout": compact_text(result.stdout, 4000),
                "stderr": compact_text(result.stderr, 4000),
            }
        )
        return result

    def _send_wechat_qr_event(self, qr_event_path: Path) -> dict[str, Any]:
        event_payload = json.loads(qr_event_path.read_text(encoding="utf-8"))
        screenshot = Path(str(event_payload.get("screenshot") or ""))
        if not screenshot.exists():
            raise WorkflowError(f"wechat qr screenshot missing: {screenshot}")
        message = str(event_payload.get("message") or "公众号需要扫码确认，请扫这张码。")
        feishu_info = FeishuClient(self.config).send_post(screenshot, message)
        return {
            "event": event_payload,
            "feishu": feishu_info,
        }

    def _run_command_with_wechat_qr_events(
        self,
        command: list[str],
        cwd: Path,
        timeout: int,
        event: str,
        qr_event_path: Path,
    ) -> subprocess.CompletedProcess[str]:
        self.logger(
            {
                "event": event,
                "command": command,
                "cwd": str(cwd),
                "qr_event_path": str(qr_event_path),
            }
        )
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        def drain(pipe: Any, sink: list[str]) -> None:
            try:
                for line in pipe:
                    sink.append(line)
            finally:
                with contextlib.suppress(Exception):
                    pipe.close()

        threads = [
            threading.Thread(target=drain, args=(proc.stdout, stdout_parts), daemon=True),
            threading.Thread(target=drain, args=(proc.stderr, stderr_parts), daemon=True),
        ]
        for thread in threads:
            thread.start()

        qr_sent = False
        timed_out = False
        next_qr_retry = 0.0
        last_qr_error = ""
        deadline = time.monotonic() + timeout
        while proc.poll() is None:
            now = time.monotonic()
            if qr_event_path.exists() and not qr_sent and now >= next_qr_retry:
                try:
                    qr_result = self._send_wechat_qr_event(qr_event_path)
                    self.logger(
                        {
                            "event": "wechat_browser_qr_sent",
                            "qr_event_path": str(qr_event_path),
                            "result": qr_result,
                        }
                    )
                    qr_sent = True
                except Exception as exc:
                    error = compact_text(exc, 800)
                    if error != last_qr_error:
                        self.logger(
                            {
                                "event": "wechat_browser_qr_send_failed",
                                "qr_event_path": str(qr_event_path),
                                "reason": error,
                            }
                        )
                        last_qr_error = error
                    next_qr_retry = now + 3
            if now >= deadline:
                timed_out = True
                proc.kill()
                break
            time.sleep(0.5)

        returncode = proc.wait()
        for thread in threads:
            thread.join(timeout=5)

        if qr_event_path.exists() and not qr_sent:
            try:
                qr_result = self._send_wechat_qr_event(qr_event_path)
                self.logger(
                    {
                        "event": "wechat_browser_qr_sent",
                        "qr_event_path": str(qr_event_path),
                        "result": qr_result,
                    }
                )
            except Exception as exc:
                self.logger(
                    {
                        "event": "wechat_browser_qr_send_failed",
                        "qr_event_path": str(qr_event_path),
                        "reason": compact_text(exc, 800),
                    }
                )

        stdout = "".join(stdout_parts)
        stderr = "".join(stderr_parts)
        if timed_out:
            returncode = 124
            stderr = (stderr + "\nwechat browser publish timed out").strip()
        result = subprocess.CompletedProcess(command, returncode, stdout, stderr)
        self.logger(
            {
                "event": f"{event}_result",
                "returncode": result.returncode,
                "stdout": compact_text(result.stdout, 4000),
                "stderr": compact_text(result.stderr, 4000),
                "qr_event_path": str(qr_event_path),
            }
        )
        return result

    def publish_xiaohongshu(self, candidate: dict[str, Any]) -> dict[str, Any]:
        if not self.config.get("xiaohongshu_publish_enabled", True):
            return {
                "platform": "xiaohongshu",
                "ok": False,
                "reason": "小红书自动化发布已禁用。",
            }
        reason = validate_publish_candidate(candidate)
        if reason:
            return {"platform": "xiaohongshu", "ok": False, "reason": reason}
        sau_exe = Path(str(self.config.get("xiaohongshu_sau_exe") or DEFAULT_SAU_EXE))
        sau_root = Path(str(self.config.get("xiaohongshu_sau_root") or DEFAULT_SAU_ROOT))
        if not sau_exe.exists():
            return {
                "platform": "xiaohongshu",
                "ok": False,
                "reason": "小红书发布工具不可用。",
            }
        timeout = int(self.config.get("xiaohongshu_publish_timeout_seconds", 900))
        check = self._run_command(
            [str(sau_exe), "xiaohongshu", "check", "--account", XIAOHONGSHU_ACCOUNT],
            sau_root,
            timeout,
            "xiaohongshu_check",
        )
        if check.returncode != 0 or "invalid" in (check.stdout or "").lower():
            return {
                "platform": "xiaohongshu",
                "ok": False,
                "reason": "小红书登录态失效。",
            }

        publish = candidate["publish"]
        command = [
            str(sau_exe),
            "xiaohongshu",
            "upload-note",
            "--account",
            XIAOHONGSHU_ACCOUNT,
            "--images",
            str(Path(candidate["image"])),
            "--title",
            safe_publish_title(str(publish.get("title") or "")),
            "--headless",
        ]
        note = str(publish.get("note") or "").strip()
        if note:
            command.extend(["--note", note])
        tags = [str(tag).strip().lstrip("#") for tag in publish.get("tags", []) if str(tag).strip()]
        if tags:
            command.extend(["--tags", ",".join(tags[:10])])
        result = self._run_command(command, sau_root, timeout, "xiaohongshu_upload_note")
        if result.returncode == 0:
            return {
                "platform": "xiaohongshu",
                "ok": True,
                "account": XIAOHONGSHU_ACCOUNT,
                "stdout": compact_text(result.stdout, 800),
            }
        return {
            "platform": "xiaohongshu",
            "ok": False,
            "reason": classify_xiaohongshu_failure(result.stderr or result.stdout),
        }

    @contextlib.contextmanager
    def _wechat_env(self) -> Iterable[None]:
        keys = (
            "WECHAT_PUBLISHER_HOME",
            "WECHAT_PUBLISHER_APP_ID",
            "WECHAT_PUBLISHER_APP_SECRET",
            "WECHAT_APP_ID",
            "WECHAT_APP_SECRET",
        )
        old_values = {key: os.environ.get(key) for key in keys}
        if not os.environ.get("WECHAT_PUBLISHER_HOME"):
            config_home = self._existing_wechat_config_home()
            if config_home:
                os.environ["WECHAT_PUBLISHER_HOME"] = str(config_home)
        if os.environ.get("WX_APPID") and not (
            os.environ.get("WECHAT_PUBLISHER_APP_ID") or os.environ.get("WECHAT_APP_ID")
        ):
            os.environ["WECHAT_APP_ID"] = os.environ["WX_APPID"]
        if os.environ.get("WX_APP_SECRET") and not (
            os.environ.get("WECHAT_PUBLISHER_APP_SECRET") or os.environ.get("WECHAT_APP_SECRET")
        ):
            os.environ["WECHAT_APP_SECRET"] = os.environ["WX_APP_SECRET"]
        try:
            yield
        finally:
            for key, value in old_values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def _existing_wechat_config_home(self) -> Path | None:
        candidates: list[Path] = []
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            candidates.append(Path(local_appdata) / "wechat-publisher")
        candidates.append(Path.home() / ".wechat-publisher")
        for candidate in candidates:
            if (candidate / "config.json").exists():
                return candidate
        return None

    def publish_wechat_sticker(self, candidate: dict[str, Any]) -> dict[str, Any]:
        method = str(self.config.get("wechat_publish_method") or "browser").strip().lower()
        if method == "api":
            return self.publish_wechat_sticker_api(candidate)
        return self.publish_wechat_browser(candidate)

    def check_wechat_browser(self) -> dict[str, Any]:
        return self.run_wechat_browser_maintenance("check")

    def login_wechat_browser(self) -> dict[str, Any]:
        return self.run_wechat_browser_maintenance("login")

    def run_wechat_browser_maintenance(self, action: str) -> dict[str, Any]:
        script = Path(str(self.config.get("wechat_browser_script") or DEFAULT_WECHAT_BROWSER_SCRIPT))
        if not script.is_absolute():
            script = ROOT / script
        if not script.exists():
            return {"ok": False, "reason": "公众号浏览器发布工具不可用。"}

        timeout_key = (
            "wechat_browser_login_timeout_seconds"
            if action == "login"
            else "wechat_browser_check_timeout_seconds"
        )
        timeout = int(self.config.get(timeout_key, 300 if action == "login" else 60))
        profile_dir = Path(
            str(
                self.config.get("wechat_browser_profile_dir")
                or (Path(self.config["state_dir"]) / "wechat_chrome_profile")
            )
        )
        if not profile_dir.is_absolute():
            profile_dir = ROOT / profile_dir
        payload_dir = Path(self.config["state_dir"]) / "wechat_browser_payloads"
        payload_dir.mkdir(parents=True, exist_ok=True)
        payload_path = payload_dir / f"doctor_{action}.json"
        payload = {
            "action": action,
            "profile_dir": str(profile_dir),
            "timeout_seconds": timeout,
            "chrome_path": self.config.get("wechat_browser_chrome_path") or "",
            "cdp_url": self.config.get("wechat_browser_cdp_url") or "",
        }
        payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        result = self._run_command(
            [sys.executable, str(script), "--payload", str(payload_path)],
            ROOT,
            timeout + 30,
            f"wechat_browser_{action}",
        )
        parsed = parse_browser_publish_result(result.stdout)
        if parsed:
            return parsed
        detail = "\n".join([result.stdout or "", result.stderr or ""])
        return {"ok": False, "reason": classify_wechat_browser_failure(detail)}

    def publish_wechat_browser(self, candidate: dict[str, Any]) -> dict[str, Any]:
        reason = validate_publish_candidate(candidate)
        if reason:
            return {"platform": "wechat", "ok": False, "reason": reason}

        script = Path(str(self.config.get("wechat_browser_script") or DEFAULT_WECHAT_BROWSER_SCRIPT))
        if not script.is_absolute():
            script = ROOT / script
        if not script.exists():
            return {"platform": "wechat", "ok": False, "reason": "公众号浏览器发布工具不可用。"}

        publish = candidate["publish"]
        content = wechat_browser_content_from_publish(publish)
        if not content:
            return {"platform": "wechat", "ok": False, "reason": "没有找到刚生成的文案。"}

        timeout = int(self.config.get("wechat_browser_timeout_seconds", 900))
        action = str(self.config.get("wechat_browser_action") or "publish").strip().lower()
        profile_dir = Path(
            str(
                self.config.get("wechat_browser_profile_dir")
                or (Path(self.config["state_dir"]) / "wechat_chrome_profile")
            )
        )
        if not profile_dir.is_absolute():
            profile_dir = ROOT / profile_dir

        payload_dir = Path(self.config["state_dir"]) / "wechat_browser_payloads"
        payload_dir.mkdir(parents=True, exist_ok=True)
        run_id = re.sub(r"[^A-Za-z0-9_\-]", "_", str(candidate.get("run_id") or uuid.uuid4().hex))
        payload_path = payload_dir / f"{run_id}.json"
        qr_event_path = payload_dir / f"{run_id}.wechat_qr.json"
        with contextlib.suppress(FileNotFoundError):
            qr_event_path.unlink()
        payload = {
            "title": safe_publish_title(str(publish.get("title") or "")),
            "content": content,
            "images": [str(Path(candidate["image"]))],
            "action": action,
            "profile_dir": str(profile_dir),
            "timeout_seconds": timeout,
            "qr_event_path": str(qr_event_path),
            "chrome_path": self.config.get("wechat_browser_chrome_path") or "",
            "cdp_url": self.config.get("wechat_browser_cdp_url") or "",
        }
        payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        command = [sys.executable, str(script), "--payload", str(payload_path)]
        result = self._run_command_with_wechat_qr_events(
            command,
            ROOT,
            timeout + 90,
            "wechat_browser_publish",
            qr_event_path,
        )
        parsed = parse_browser_publish_result(result.stdout)
        if result.returncode == 0 and parsed and parsed.get("ok"):
            self.logger(
                {
                    "event": "wechat_browser_publish_result",
                    "ok": True,
                    "result": parsed,
                    "payload_path": str(payload_path),
                    "qr_event_path": str(qr_event_path),
                }
            )
            return {
                "platform": "wechat",
                "ok": True,
                "result": parsed,
                "method": "browser",
            }

        detail = "\n".join(
            [
                json.dumps(parsed, ensure_ascii=False) if parsed else "",
                result.stdout or "",
                result.stderr or "",
            ]
        )
        reason = str((parsed or {}).get("reason") or "").strip() or classify_wechat_browser_failure(detail)
        self.logger(
            {
                "event": "wechat_browser_publish_result",
                "ok": False,
                "reason": reason,
                "payload_path": str(payload_path),
                "qr_event_path": str(qr_event_path),
            }
        )
        return {"platform": "wechat", "ok": False, "reason": reason}

    def publish_wechat_sticker_api(self, candidate: dict[str, Any]) -> dict[str, Any]:
        reason = validate_publish_candidate(candidate)
        if reason:
            return {"platform": "wechat", "ok": False, "reason": reason}
        publisher_dir = Path(
            str(self.config.get("wechat_publisher_dir") or DEFAULT_WECHAT_PUBLISHER_DIR)
        )
        publisher_py = publisher_dir / "publisher.py"
        if not publisher_py.exists():
            return {"platform": "wechat", "ok": False, "reason": "公众号发布工具不可用。"}

        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with self._wechat_env(), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                spec = importlib.util.spec_from_file_location("wechat_draft_publisher", publisher_py)
                if spec is None or spec.loader is None:
                    raise WorkflowError(f"cannot load wechat publisher: {publisher_py}")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                publisher = module.WeChatPublisher()
                publish_method = getattr(publisher, "publish_image_to_all", None)
                if publish_method is None:
                    raise WorkflowError("公众号贴图发布方法未安装。")
                tag_id = self.config.get("wechat_publish_tag_id")
                result = publish_method(
                    str(Path(candidate["image"])),
                    tag_id=int(tag_id) if tag_id is not None else None,
                )
            self.logger(
                {
                    "event": "wechat_sticker_publish_result",
                    "ok": True,
                    "result": result,
                    "stdout": compact_text(stdout.getvalue(), 4000),
                    "stderr": compact_text(stderr.getvalue(), 4000),
                }
            )
            return {
                "platform": "wechat",
                "ok": True,
                "result": result,
            }
        except Exception as exc:
            detail = "\n".join([str(exc), stdout.getvalue(), stderr.getvalue()])
            self.logger(
                {
                    "event": "wechat_sticker_publish_result",
                    "ok": False,
                    "error": compact_text(detail, 4000),
                }
            )
            return {
                "platform": "wechat",
                "ok": False,
                "reason": classify_wechat_failure(detail),
            }


class Workflow:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.state_dir = Path(config["state_dir"])
        self.output_root = Path(config["output_root"])
        self.runs_path = self.state_dir / "runs.jsonl"
        self.lock_path = self.state_dir / "workflow.lock"
        self.command_events_path = self.state_dir / "command_events.json"
        self.command_events_lock_path = self.state_dir / "command_events.lock"
        self.latest_publish_candidate_path = self.state_dir / "latest_publish_candidate.json"
        self.latest_candidate_pool_path = self.state_dir / "latest_candidate_pool.json"
        self.pending_publish_path = self.state_dir / "publish_pending.json"
        self.manual_publish_state_path = self.state_dir / "manual_publish_state.json"
        self.publish_log_path = self.state_dir / "publish_events.jsonl"

    def ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.output_root.mkdir(parents=True, exist_ok=True)

    def log_publish_event(self, payload: dict[str, Any]) -> None:
        event = dict(payload)
        event.setdefault("created_at", now_iso(self.config["timezone"]))
        json_line(self.publish_log_path, event)

    def _load_command_events(self) -> dict[str, Any]:
        if not self.command_events_path.exists():
            return {}
        try:
            data = json.loads(self.command_events_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _write_command_events(self, events: dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self.command_events_path.with_suffix(self.command_events_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self.command_events_path)

    def _command_event_key(self, event: dict[str, Any], command_name: str) -> str:
        message_id = str(event.get("message_id") or event.get("event_id") or "").strip()
        if not message_id:
            return ""
        text = normalize_user_text(str(event.get("content") or ""))
        digest = hashlib.sha256(f"{command_name}\n{text}".encode("utf-8")).hexdigest()[:16]
        return f"{message_id}:{digest}"

    def _command_result_summary(self, result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            return {"kind": type(result).__name__}
        state = result.get("state") if isinstance(result.get("state"), dict) else {}
        return {
            "kind": result.get("kind"),
            "ok": result.get("ok"),
            "reply": compact_text(result.get("reply") or "", 500),
            "run_id": state.get("xhs_workflow_run_id") or state.get("run_id"),
            "status": state.get("status"),
            "screenshot_path": state.get("screenshot_path"),
        }

    def run_idempotent_message_command(
        self,
        event: dict[str, Any],
        command_name: str,
        feishu_client: Any,
        callback: Any,
    ) -> dict[str, Any]:
        key = self._command_event_key(event, command_name)
        if not key:
            return callback()
        now = now_iso(self.config["timezone"])
        with FileLock(self.command_events_lock_path, int(self.config.get("lock_timeout_seconds", 30))):
            events = self._load_command_events()
            existing = events.get(key)
            if existing:
                reply = (
                    f"这条飞书命令已经处理过（status={existing.get('status') or 'unknown'}），"
                    "不会重复触发。"
                )
                feishu_client.send_text(reply)
                return {
                    "kind": "duplicate_command",
                    "command": command_name,
                    "reply": reply,
                    "previous": existing.get("result"),
                }
            events[key] = {
                "command": command_name,
                "message_id": str(event.get("message_id") or event.get("event_id") or ""),
                "status": "in_progress",
                "created_at": now,
                "updated_at": now,
            }
            # ponytail: tiny JSON ledger; keep the newest 200 message keys until throughput needs a DB.
            if len(events) > 200:
                ordered = sorted(events.items(), key=lambda item: str(item[1].get("updated_at") or ""))
                events = dict(ordered[-200:])
            self._write_command_events(events)
        try:
            result = callback()
            status = "failed" if isinstance(result, dict) and result.get("ok") is False else "done"
            summary = self._command_result_summary(result)
        except Exception as exc:
            status = "failed"
            summary = {"kind": command_name, "error": compact_text(exc, 500)}
            raise
        finally:
            with contextlib.suppress(Exception):
                with FileLock(
                    self.command_events_lock_path,
                    int(self.config.get("lock_timeout_seconds", 30)),
                ):
                    events = self._load_command_events()
                    if key in events:
                        events[key]["status"] = locals().get("status", "failed")
                        events[key]["result"] = locals().get("summary", {})
                        events[key]["updated_at"] = now_iso(self.config["timezone"])
                        self._write_command_events(events)
        return result

    def handle_feedback_event(
        self,
        event: dict[str, Any],
        feishu_client: Any | None = None,
    ) -> dict[str, Any] | None:
        result = FeedbackRouter(self.config).handle_event(event)
        if not result:
            return None
        reply = result.get("reply")
        if reply:
            active_feishu_client = feishu_client or FeishuClient(self.config)
            active_feishu_client.send_text(str(reply))
        return result

    def queue_publish_intent_event(
        self,
        event: dict[str, Any],
        intent: BotIntent,
        selection: dict[str, Any],
        feishu_client: Any | None = None,
    ) -> dict[str, Any]:
        result = FeedbackRouter(self.config).handle_publish_intent(
            event,
            intent.platforms,
            image_number=selection.get("image_number"),
            caption_number=selection.get("caption_number"),
        )
        reply = result.get("reply")
        if reply:
            active_feishu_client = feishu_client or FeishuClient(self.config)
            active_feishu_client.send_text(str(reply))
        return result

    def _send_text_with_optional_image(
        self,
        feishu_client: Any,
        text: str,
        screenshot_path: str | None,
    ) -> dict[str, Any] | None:
        path = Path(str(screenshot_path or ""))
        if screenshot_path and path.exists() and path.is_file():
            try:
                if hasattr(feishu_client, "send_local_image_with_text"):
                    return feishu_client.send_local_image_with_text(path, text)
                return feishu_client.send_post(path, text)
            except Exception as exc:
                fallback = f"{text}\n\n截图发送失败：{user_failure_reason(exc)}\n截图路径：{path}"
                feishu_client.send_text(fallback)
                return {"image_send_failed": True, "screenshot_path": str(path)}
        if screenshot_path:
            text = f"{text}\n\n截图路径：{screenshot_path}"
        return feishu_client.send_text(text)

    def handle_xhs_vision_dry_run(
        self,
        feishu_client: Any | None = None,
        candidate: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        active_feishu_client = feishu_client or FeishuClient(self.config)
        with contextlib.suppress(Exception):
            active_feishu_client.send_text("小红书开始预检 dry-run，完成后会发截图。")
        try:
            with FileLock(self.lock_path, int(self.config.get("lock_timeout_seconds", 30))):
                state = xhs_vision_bridge.start_dry_run(self.config, candidate=candidate)
        except Exception as exc:
            reply = f"小红书预检失败：{user_failure_reason(exc)}"
            active_feishu_client.send_text(reply)
            return {"kind": "xhs_vision_dry_run", "ok": False, "reply": reply, "error": str(exc)}

        dry = state.get("dry_run_result") if isinstance(state.get("dry_run_result"), dict) else {}
        run_id = str(state.get("xhs_workflow_run_id") or "")
        screenshot = str(dry.get("screenshot_path") or state.get("screenshot_path") or "")
        if state.get("status") == "awaiting_confirm":
            reply = (
                "小红书预检成功\n"
                f"run_id: {run_id}\n"
                f"标题: {state.get('title')}\n"
                f"图片路径: {state.get('candidate_image')}\n"
                f"publish_button_visible={bool(dry.get('publish_button_visible'))}\n"
                "risk_warning_found=false\n"
                f"请回复：确认发布 {run_id}"
            )
        elif state.get("status") == "blocked_by_risk_warning":
            reply = (
                "检测到风险提示，需要人工处理，未进入确认发布。\n"
                f"run_id: {run_id}\n"
                f"截图路径: {screenshot}"
            )
        else:
            reason = dry.get("error") or "dry-run 未完成"
            reply = (
                f"小红书预检失败：{reason}\n"
                f"run_id: {run_id}\n"
                f"截图路径: {screenshot}"
            )
        self._send_text_with_optional_image(active_feishu_client, reply, screenshot)
        return {"kind": "xhs_vision_dry_run", "state": state, "reply": reply}

    def handle_xhs_vision_confirm(
        self,
        run_id: str | None = None,
        feishu_client: Any | None = None,
    ) -> dict[str, Any]:
        active_feishu_client = feishu_client or FeishuClient(self.config)
        if run_id is None and self.config.get("publish_confirm_requires_run_id", True):
            reply = "请带 run_id 确认：确认发布 <run_id>"
            active_feishu_client.send_text(reply)
            return {"kind": "xhs_vision_confirm", "ok": False, "reply": reply}
        current_state = xhs_vision_bridge.load_publish_state(self.config)
        if state_expired(current_state, int(self.config.get("publish_confirm_ttl_seconds", 7200))):
            reply = "小红书确认已过期，请重新发送“发布小红书”预检。"
            active_feishu_client.send_text(reply)
            return {"kind": "xhs_vision_confirm", "ok": False, "reply": reply}
        with contextlib.suppress(Exception):
            active_feishu_client.send_text("已收到小红书发布确认，开始执行提交尝试。")
        try:
            with FileLock(self.lock_path, int(self.config.get("lock_timeout_seconds", 30))):
                state = xhs_vision_bridge.confirm_publish(self.config, run_id=run_id or None)
        except Exception as exc:
            reply = str(exc)
            active_feishu_client.send_text(reply)
            return {"kind": "xhs_vision_confirm", "ok": False, "reply": reply, "error": str(exc)}

        publish = state.get("publish_result") if isinstance(state.get("publish_result"), dict) else {}
        screenshot = str(publish.get("screenshot_path") or state.get("screenshot_path") or "")
        if state.get("status") == "submitted":
            headline = "小红书已提交/审核中，以小红书页面和截图为准。"
        elif state.get("status") == "publish_attempted":
            headline = "小红书提交尝试完成，但未确认平台审核状态，以截图为准。"
        else:
            headline = f"小红书提交失败：{publish.get('error') or '未知原因'}"
        reply = (
            f"{headline}\n"
            f"run_id: {state.get('xhs_workflow_run_id')}\n"
            f"publish_attempted={bool(publish.get('publish_attempted'))}\n"
            f"submitted_or_reviewing={bool(publish.get('submitted_or_reviewing'))}\n"
            f"risk_warning_found={bool(publish.get('risk_warning_found'))}\n"
            f"截图路径: {screenshot}"
        )
        self._send_text_with_optional_image(active_feishu_client, reply, screenshot)
        return {"kind": "xhs_vision_confirm", "state": state, "reply": reply}

    def handle_wechat_mp_prepare(
        self,
        feishu_client: Any | None = None,
        candidate: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        active_feishu_client = feishu_client or FeishuClient(self.config)
        if not self.config.get("wechat_mp_publish_enabled", True):
            reply = "公众号 MuMu 发布链路已禁用。"
            active_feishu_client.send_text(reply)
            return {"kind": "wechat_mp_prepare", "ok": False, "reply": reply}

        with contextlib.suppress(Exception):
            active_feishu_client.send_text("公众号开始准备发布预检，完成后会发预览截图。")
        try:
            with FileLock(self.lock_path, int(self.config.get("lock_timeout_seconds", 30))):
                state = wechat_mp_bridge.start_prepare(self.config, candidate=candidate)
        except Exception as exc:
            reply = f"公众号预检失败：{user_failure_reason(exc)}"
            active_feishu_client.send_text(reply)
            return {"kind": "wechat_mp_prepare", "ok": False, "reply": reply, "error": str(exc)}

        prepare = state.get("prepare_result") if isinstance(state.get("prepare_result"), dict) else {}
        run_id = str(state.get("xhs_workflow_run_id") or "")
        screenshot = str(prepare.get("screenshot_path") or state.get("screenshot_path") or "")
        if state.get("status") == "awaiting_confirm":
            reply = (
                "公众号预检成功，尚未发表。\n"
                f"run_id: {run_id}\n"
                f"标题: {state.get('title')}\n"
                f"图片路径: {state.get('candidate_image')}\n"
                f"publish_button_visible={bool(prepare.get('publish_button_visible'))}\n"
                "risk_warning_found=false\n"
                f"请回复：可以发表 {run_id}"
            )
        elif state.get("status") == "blocked_by_risk_warning":
            reply = (
                "公众号预检检测到风险提示，需要人工处理，未进入确认发表。\n"
                f"run_id: {run_id}\n"
                f"risk_words={prepare.get('risk_words') or []}\n"
                f"截图路径: {screenshot}"
            )
        else:
            reply = (
                f"公众号预检失败：{prepare.get('error') or 'prepare 未完成'}\n"
                f"run_id: {run_id}\n"
                f"截图路径: {screenshot}"
            )
        self._send_text_with_optional_image(active_feishu_client, reply, screenshot)
        return {"kind": "wechat_mp_prepare", "state": state, "reply": reply}

    def handle_wechat_mp_confirm(
        self,
        run_id: str | None = None,
        feishu_client: Any | None = None,
    ) -> dict[str, Any]:
        active_feishu_client = feishu_client or FeishuClient(self.config)
        state = wechat_mp_bridge.load_publish_state(self.config)
        expected_run_id = str((state or {}).get("xhs_workflow_run_id") or "")
        if run_id is None and self.config.get("publish_confirm_requires_run_id", True):
            reply = "请带 run_id 确认：可以发表 <run_id>"
            active_feishu_client.send_text(reply)
            return {"kind": "wechat_mp_confirm", "ok": False, "reply": reply}
        if not state or state.get("status") != "awaiting_confirm":
            reply = "没有等待确认的公众号发布任务。"
            active_feishu_client.send_text(reply)
            return {"kind": "wechat_mp_confirm", "ok": False, "reply": reply}
        if state_expired(state, int(self.config.get("publish_confirm_ttl_seconds", 7200))):
            reply = "公众号确认已过期，请重新发送“发布公众号”预检。"
            active_feishu_client.send_text(reply)
            return {"kind": "wechat_mp_confirm", "ok": False, "reply": reply}
        if run_id and run_id != expected_run_id:
            reply = f"确认 run_id 不匹配：当前等待确认的是 {expected_run_id}"
            active_feishu_client.send_text(reply)
            return {"kind": "wechat_mp_confirm", "ok": False, "reply": reply}

        with contextlib.suppress(Exception):
            active_feishu_client.send_text("已收到公众号发表确认，开始执行最终发表。")
        try:
            with FileLock(self.lock_path, int(self.config.get("lock_timeout_seconds", 30))):
                state = wechat_mp_bridge.confirm_publish(self.config, run_id=run_id or None)
        except Exception as exc:
            reply = f"公众号发表失败：{user_failure_reason(exc)}"
            active_feishu_client.send_text(reply)
            return {"kind": "wechat_mp_confirm", "ok": False, "reply": reply, "error": str(exc)}
        finally:
            cleanup_script = ROOT / "cleanup_publish_background.ps1"
            if cleanup_script.exists():
                with contextlib.suppress(Exception):
                    subprocess.run(
                        [
                            "powershell.exe",
                            "-NoProfile",
                            "-ExecutionPolicy",
                            "Bypass",
                            "-File",
                            str(cleanup_script),
                        ],
                        cwd=str(ROOT),
                        capture_output=True,
                        text=True,
                        timeout=60,
                        check=False,
                    )

        publish = state.get("publish_result") if isinstance(state.get("publish_result"), dict) else {}
        screenshot = str(publish.get("screenshot_path") or state.get("screenshot_path") or "")
        if state.get("status") == "published":
            headline = "公众号已验证发表，可以查看了。"
        elif state.get("status") == "publish_attempted":
            headline = "公众号发表点击已完成，但未验证到已发表列表，请看截图确认。"
        elif publish.get("risk_warning_found"):
            headline = "公众号发表后检测到风险提示，需要人工查看。"
        else:
            headline = f"公众号发表未确认成功：{publish.get('error') or '未知原因'}"
        reply = (
            f"{headline}\n"
            f"run_id: {state.get('xhs_workflow_run_id')}\n"
            f"published_clicks_completed={bool(publish.get('published_clicks_completed'))}\n"
            f"risk_warning_found={bool(publish.get('risk_warning_found'))}\n"
            f"截图路径: {screenshot}"
        )
        self._send_text_with_optional_image(active_feishu_client, reply, screenshot)
        return {"kind": "wechat_mp_confirm", "state": state, "reply": reply}

    def save_latest_publish_candidate(self, metadata: dict[str, Any]) -> None:
        candidate = publish_candidate_from_metadata(metadata)
        if not candidate:
            return
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.latest_publish_candidate_path.write_text(
            json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def save_latest_candidate_pool(self, results: list[dict[str, Any]]) -> dict[str, Any] | None:
        pool = latest_candidate_pool_from_results(results, self.config["timezone"])
        if not pool:
            return None
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.latest_candidate_pool_path.write_text(
            json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        candidate = candidate_from_pool(pool)
        if candidate:
            self.latest_publish_candidate_path.write_text(
                json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return pool

    def load_latest_candidate_pool(self) -> dict[str, Any] | None:
        if self.latest_candidate_pool_path.exists():
            try:
                return json.loads(self.latest_candidate_pool_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None
        latest = last_json_line(self.runs_path)
        candidate = publish_candidate_from_metadata(latest or {})
        if not candidate:
            return None
        return latest_candidate_pool_from_results([latest], self.config["timezone"])

    def load_latest_publish_candidate(self) -> dict[str, Any] | None:
        pool = self.load_latest_candidate_pool()
        if pool:
            candidate = candidate_from_pool(pool)
            if candidate:
                return candidate
        if not self.latest_publish_candidate_path.exists():
            latest = last_json_line(self.runs_path)
            return publish_candidate_from_metadata(latest or {})
        try:
            return json.loads(self.latest_publish_candidate_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def save_pending_publish(self, candidate: dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "chat_id": self.config["feishu_chat_id"],
            "created_at": now_iso(self.config["timezone"]),
            "candidate": candidate,
        }
        self.pending_publish_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def load_pending_publish(self) -> dict[str, Any] | None:
        if not self.pending_publish_path.exists():
            return None
        try:
            payload = json.loads(self.pending_publish_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        if payload.get("chat_id") != self.config["feishu_chat_id"]:
            return None
        candidate = payload.get("candidate")
        return candidate if isinstance(candidate, dict) else None

    def clear_pending_publish(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.pending_publish_path.unlink()

    def save_manual_publish_state(
        self,
        stage: str,
        platforms: tuple[str, ...] = (),
        image_path: str = "",
        image_key: str = "",
        image_message_id: str = "",
        caption_text: str = "",
        created_at: str | None = None,
    ) -> ManualPublishState:
        now = now_iso(self.config["timezone"])
        state = ManualPublishState(
            stage=stage,
            created_at=created_at or now,
            updated_at=now,
            platforms=platforms,
            image_path=image_path,
            image_key=image_key,
            image_message_id=image_message_id,
            caption_text=caption_text,
        )
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = dataclasses.asdict(state)
        payload["platforms"] = list(state.platforms)
        self.manual_publish_state_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return state

    def load_manual_publish_state(self) -> ManualPublishState | None:
        if not self.manual_publish_state_path.exists():
            return None
        try:
            payload = json.loads(self.manual_publish_state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return ManualPublishState(
            stage=str(payload.get("stage") or ""),
            created_at=str(payload.get("created_at") or now_iso(self.config["timezone"])),
            updated_at=str(payload.get("updated_at") or ""),
            platforms=tuple(payload.get("platforms") or ()),
            image_path=str(payload.get("image_path") or ""),
            image_key=str(payload.get("image_key") or ""),
            image_message_id=str(payload.get("image_message_id") or ""),
            caption_text=str(payload.get("caption_text") or ""),
        )

    def clear_manual_publish_state(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.manual_publish_state_path.unlink()

    def manual_publish_candidate(self, state: ManualPublishState) -> dict[str, Any] | None:
        if not state.image_path or not state.caption_text:
            return None
        publish = split_caption_text_for_publish(state.caption_text)
        return {
            "run_id": f"manual-{uuid.uuid4().hex[:8]}",
            "batch_id": "manual",
            "image": state.image_path,
            "caption_path": "",
            "metadata_path": "",
            "created_at": now_iso(self.config["timezone"]),
            "character": None,
            "publish": publish,
            "selection": {
                "source": "manual_feishu_image",
                "image_key": state.image_key,
                "image_message_id": state.image_message_id,
            },
        }

    def manual_upload_output_path(self, message_id: str, image_key: str) -> Path:
        safe_message = re.sub(r"[^A-Za-z0-9_\-]", "_", message_id or uuid.uuid4().hex)
        safe_key = re.sub(r"[^A-Za-z0-9_\-]", "_", image_key or "image")
        return self.state_dir / "manual_uploads" / f"{safe_message}_{safe_key}.png"

    def publish_candidate(
        self,
        platforms: tuple[str, ...],
        candidate: dict[str, Any] | None,
        publish_client: Any | None = None,
    ) -> dict[str, Any]:
        reason = validate_publish_candidate(candidate)
        if reason:
            return {
                "kind": "publish",
                "results": [{"platform": "unknown", "ok": False, "reason": reason}],
                "reply": f"发布失败：{reason}",
            }
        if publish_client is None:
            bridge_blocked: list[dict[str, Any]] = []
            for platform in platforms:
                if platform == "xiaohongshu" and self.config.get("xhs_vision_publish_enabled", False):
                    bridge_blocked.append(
                        {
                            "platform": platform,
                            "ok": False,
                            "reason": "小红书已接入视觉确认闭环，请发送“发布小红书”，再用“确认发布 <run_id>”。",
                        }
                    )
                elif platform == "wechat" and self.config.get("wechat_mp_publish_enabled", True):
                    bridge_blocked.append(
                        {
                            "platform": platform,
                            "ok": False,
                            "reason": "公众号已接入 MuMu 确认闭环，请发送“发布公众号”，再用“可以发表 <run_id>”。",
                        }
                    )
            if bridge_blocked:
                reply = "发布已拦截：请走平台预检 + 飞书确认闭环，避免绕过最终确认。"
                self.log_publish_event(
                    {
                        "event": "publish_bridge_blocked",
                        "run_id": candidate.get("run_id"),
                        "platforms": platforms,
                        "results": bridge_blocked,
                        "reply": reply,
                    }
                )
                return {"kind": "publish", "results": bridge_blocked, "reply": reply}
        client = publish_client or LocalPublisher(self.config, self.log_publish_event)
        results = [client.publish(platform, candidate) for platform in platforms]
        reply = publish_reply_text(results)
        self.log_publish_event(
            {
                "event": "publish_summary",
                "run_id": candidate.get("run_id"),
                "platforms": platforms,
                "results": results,
                "reply": reply,
            }
        )
        return {"kind": "publish", "results": results, "reply": reply}

    def detect_event_intent(
        self,
        text: str,
        intent_client: Any | None = None,
    ) -> BotIntent:
        max_count = int(self.config["max_count_per_request"])
        deterministic = detect_bot_intent(text, max_count)
        if not self.config.get("intent_classifier_enabled", True):
            return deterministic
        ai_required = bool(self.config.get("intent_classifier_required_for_publish", False)) and (
            publish_intent_requires_ai(text, deterministic)
        )
        if not should_use_ai_intent_classifier(text, deterministic):
            return deterministic
        try:
            client = intent_client or DeepSeekIntentClassifierClient(self.config)
            ai_intent = client.classify(text, max_count)
            self.log_publish_event(
                {
                    "event": "intent_classifier_result",
                    "text": compact_text(text, 240),
                    "deterministic": dataclasses.asdict(deterministic),
                    "ai_intent": dataclasses.asdict(ai_intent),
                }
            )
            return ai_intent
        except Exception as exc:
            self.log_publish_event(
                {
                    "event": "intent_classifier_failed",
                    "text": compact_text(text, 240),
                    "deterministic": dataclasses.asdict(deterministic),
                    "error": compact_text(exc, 800),
                }
            )
            if ai_required:
                return BotIntent("ignore", reason="intent_classifier_failed_required")
            return deterministic

    def preview(self, count: int, date_str: str | None = None) -> list[dict[str, Any]]:
        date_str = date_str or today_string(self.config["timezone"])
        with FileLock(self.lock_path, int(self.config.get("lock_timeout_seconds", 30))):
            selections = select_materials(self.config, count, date_str)
        return [selection_to_dict(selection) for selection in selections]

    def run_batch(
        self,
        count: int,
        date_str: str | None = None,
        openai_client: Any | None = None,
        feishu_client: Any | None = None,
        no_commit: bool = False,
    ) -> list[dict[str, Any]]:
        self.ensure_dirs()
        date_str = date_str or today_string(self.config["timezone"])
        openai_client = openai_client or workflow_client_from_config(self.config)
        feishu_client = feishu_client or FeishuClient(self.config)
        results: list[dict[str, Any]] = []
        with FileLock(self.lock_path, int(self.config.get("lock_timeout_seconds", 30))):
            selections = select_materials(self.config, count, date_str)
            for selection in selections:
                results.append(
                    self._run_one(selection, openai_client, feishu_client, no_commit)
                )
        pool = self.save_latest_candidate_pool(results)
        if pool and len(results) > 1:
            with contextlib.suppress(Exception):
                feishu_client.send_text(candidate_pool_summary_text(pool))
        return results

    def scheduled_daily_run(
        self,
        count: int | None = None,
        date_str: str | None = None,
        platforms: tuple[str, ...] | None = None,
        openai_client: Any | None = None,
        feishu_client: Any | None = None,
        no_commit: bool = False,
    ) -> dict[str, Any]:
        if not self.config.get("scheduled_publish_enabled", True):
            return {"kind": "scheduled_daily_run", "ok": False, "reason": "定时发布已禁用。"}
        feishu_client = feishu_client or FeishuClient(self.config)
        count = clamp_count(int(count or self.config.get("scheduled_publish_count", 1)), self.config)
        platforms = platforms or scheduled_platforms_from_config(self.config)
        feishu_client.send_text(
            "定时发布开始：先生成内容，再做平台预检/准备；最终发布仍需飞书确认。"
        )
        generated = self.run_batch(
            count=count,
            date_str=date_str,
            openai_client=openai_client,
            feishu_client=feishu_client,
            no_commit=no_commit,
        )
        actions: list[dict[str, Any]] = []
        candidate = publish_candidate_from_metadata(generated[-1]) if generated else None
        candidate = candidate or self.load_latest_publish_candidate()
        if "wechat" in platforms:
            actions.append(self.handle_wechat_mp_prepare(feishu_client=feishu_client, candidate=candidate))
        if "xiaohongshu" in platforms:
            if self.config.get("xhs_vision_publish_enabled", False):
                actions.append(self.handle_xhs_vision_dry_run(feishu_client=feishu_client, candidate=candidate))
            else:
                reply = "小红书视觉预检未启用，本次定时任务跳过小红书。"
                feishu_client.send_text(reply)
                actions.append({"kind": "xhs_vision_dry_run", "ok": False, "reply": reply})
        summary = {
            "kind": "scheduled_daily_run",
            "ok": True,
            "generated_count": len(generated),
            "platforms": platforms,
            "actions": actions,
            "final_publish_requires_feishu_confirm": True,
        }
        self.log_publish_event(
            {
                "event": "scheduled_daily_run",
                "generated_count": len(generated),
                "platforms": platforms,
                "action_kinds": [action.get("kind") for action in actions],
                "final_publish_requires_feishu_confirm": True,
            }
        )
        return summary

    def publish_manual_state(
        self,
        state: ManualPublishState,
        platforms: tuple[str, ...],
        feishu_client: Any,
        publish_client: Any | None = None,
    ) -> dict[str, Any]:
        if not platforms:
            self.save_manual_publish_state(
                "awaiting_platform",
                platforms=state.platforms,
                image_path=state.image_path,
                image_key=state.image_key,
                image_message_id=state.image_message_id,
                caption_text=state.caption_text,
                created_at=state.created_at,
            )
            feishu_client.send_text(PUBLISH_CONFIRM_TEXT)
            return {"kind": "publish_prompt", "reply": PUBLISH_CONFIRM_TEXT}
        with contextlib.suppress(Exception):
            feishu_client.send_text(publish_ack_text())
        result = self.publish_candidate(
            platforms,
            self.manual_publish_candidate(state),
            publish_client=publish_client,
        )
        self.clear_manual_publish_state()
        feishu_client.send_text(result["reply"])
        return result

    def handle_manual_publish_state(
        self,
        state: ManualPublishState,
        event: dict[str, Any],
        text: str,
        feishu_client: Any,
        publish_client: Any | None = None,
    ) -> Any | None:
        msg_type = event.get("msg_type") or "text"
        if msg_type == "image":
            if state.stage != "awaiting_image":
                return None
            image_key = extract_feishu_image_key(event)
            message_id = str(event.get("message_id") or "")
            if not image_key or not message_id:
                reply = "图片接收失败：没有拿到飞书图片。"
                feishu_client.send_text(reply)
                return {"kind": "manual_publish_image", "reply": reply}
            output_path = self.manual_upload_output_path(message_id, image_key)
            try:
                downloaded_path = feishu_client.download_message_image(
                    message_id, image_key, output_path
                )
            except Exception as exc:
                self.log_publish_event(
                    {
                        "event": "manual_image_download_failed",
                        "message_id": message_id,
                        "image_key": image_key,
                        "error": compact_text(exc, 800),
                    }
                )
                reply = "图片接收失败：飞书图片下载失败。"
                feishu_client.send_text(reply)
                return {"kind": "manual_publish_image", "reply": reply}
            self.save_manual_publish_state(
                "awaiting_caption",
                platforms=state.platforms,
                image_path=str(downloaded_path),
                image_key=image_key,
                image_message_id=message_id,
                created_at=state.created_at,
            )
            reply = manual_caption_request_text()
            feishu_client.send_text(reply)
            return {"kind": "manual_publish_image", "reply": reply}
        if msg_type != "text":
            return None
        if is_cancel_text(text):
            self.clear_manual_publish_state()
            reply = manual_cancel_text()
            feishu_client.send_text(reply)
            return {"kind": "manual_publish_cancel", "reply": reply}
        if state.stage == "awaiting_image":
            reply = manual_image_request_text()
            feishu_client.send_text(reply)
            return {"kind": "manual_publish_prompt", "reply": reply}
        if state.stage == "awaiting_caption":
            self.save_manual_publish_state(
                "awaiting_confirm",
                platforms=state.platforms,
                image_path=state.image_path,
                image_key=state.image_key,
                image_message_id=state.image_message_id,
                caption_text=text,
                created_at=state.created_at,
            )
            reply = manual_confirm_text()
            feishu_client.send_text(reply)
            return {"kind": "manual_publish_confirm", "reply": reply}
        if state.stage == "awaiting_confirm":
            if is_confirm_text(text):
                platforms = parse_publish_platforms(text) or state.platforms
                return self.publish_manual_state(
                    state, platforms, feishu_client, publish_client=publish_client
                )
            reply = "请回复确定或取消。"
            feishu_client.send_text(reply)
            return {"kind": "manual_publish_prompt", "reply": reply}
        if state.stage == "awaiting_platform":
            platforms = parse_publish_platforms(text)
            if not platforms:
                feishu_client.send_text(PUBLISH_CONFIRM_TEXT)
                return {"kind": "publish_prompt", "reply": PUBLISH_CONFIRM_TEXT}
            return self.publish_manual_state(
                state, platforms, feishu_client, publish_client=publish_client
            )
        self.clear_manual_publish_state()
        return None

    def handle_event(
        self,
        event: dict[str, Any],
        openai_client: Any | None = None,
        feishu_client: Any | None = None,
        general_client: Any | None = None,
        publish_client: Any | None = None,
        intent_client: Any | None = None,
        no_commit: bool = False,
    ) -> Any | None:
        if event.get("chat_id") != self.config["feishu_chat_id"]:
            return None
        text = str(event.get("content", "") or "")
        active_feishu_client = feishu_client
        if (event.get("msg_type") or "text") == "text":
            if self.config.get("xhs_vision_publish_enabled", False):
                confirm_run_id = parse_xhs_vision_confirm_run_id(text)
                if confirm_run_id is not None:
                    active_feishu_client = active_feishu_client or FeishuClient(self.config)
                    return self.run_idempotent_message_command(
                        event,
                        "xhs_vision_confirm",
                        active_feishu_client,
                        lambda: self.handle_xhs_vision_confirm(
                            run_id=confirm_run_id or None,
                            feishu_client=active_feishu_client,
                        ),
                    )
                if is_xhs_vision_dry_run_command(text):
                    active_feishu_client = active_feishu_client or FeishuClient(self.config)
                    return self.run_idempotent_message_command(
                        event,
                        "xhs_vision_dry_run",
                        active_feishu_client,
                        lambda: self.handle_xhs_vision_dry_run(feishu_client=active_feishu_client),
                    )
            if self.config.get("wechat_mp_publish_enabled", True):
                wechat_confirm_run_id = parse_wechat_mp_confirm_run_id(text)
                if wechat_confirm_run_id is not None:
                    active_feishu_client = active_feishu_client or FeishuClient(self.config)
                    return self.run_idempotent_message_command(
                        event,
                        "wechat_mp_confirm",
                        active_feishu_client,
                        lambda: self.handle_wechat_mp_confirm(
                            run_id=wechat_confirm_run_id or None,
                            feishu_client=active_feishu_client,
                        ),
                    )
                if is_wechat_mp_prepare_command(text):
                    active_feishu_client = active_feishu_client or FeishuClient(self.config)
                    return self.run_idempotent_message_command(
                        event,
                        "wechat_mp_prepare",
                        active_feishu_client,
                        lambda: self.handle_wechat_mp_prepare(feishu_client=active_feishu_client),
                    )
            feedback_result = self.handle_feedback_event(event, feishu_client=active_feishu_client)
            if feedback_result:
                return feedback_result
        manual_state = self.load_manual_publish_state()
        if manual_state:
            active_feishu_client = active_feishu_client or FeishuClient(self.config)
            manual_result = self.handle_manual_publish_state(
                manual_state,
                event,
                text,
                active_feishu_client,
                publish_client=publish_client,
            )
            if manual_result:
                return manual_result
            if (event.get("msg_type") or "text") != "text":
                return None
        if (event.get("msg_type") or "text") != "text":
            return None
        intent = self.detect_event_intent(text, intent_client=intent_client)
        if intent.kind == "ignore":
            return None
        pending_candidate = self.load_pending_publish()
        pending_platforms = parse_publish_platforms(text)
        if intent.kind == "publish" and self.config.get("feedback_queue_all_publish", False):
            active_feishu_client = active_feishu_client or FeishuClient(self.config)
            selection = parse_publish_selection(text)
            return self.queue_publish_intent_event(
                event,
                intent,
                selection,
                feishu_client=active_feishu_client,
            )
        if (
            pending_candidate
            and intent.kind != "publish"
            and pending_platforms
            and not self.config.get("intent_classifier_required_for_publish", False)
        ):
            intent = BotIntent("publish", reason="pending_platform_fallback", platforms=pending_platforms)
        if pending_candidate and intent.kind == "publish" and intent.platforms:
            active_feishu_client = active_feishu_client or FeishuClient(self.config)
            with contextlib.suppress(Exception):
                active_feishu_client.send_text(publish_ack_text())
            result = self.publish_candidate(
                intent.platforms,
                pending_candidate,
                publish_client=publish_client,
            )
            self.clear_pending_publish()
            active_feishu_client.send_text(result["reply"])
            return result
        if intent.kind == "manual_publish":
            active_feishu_client = active_feishu_client or FeishuClient(self.config)
            self.save_manual_publish_state(
                "awaiting_image",
                platforms=intent.platforms,
            )
            reply = manual_image_request_text()
            active_feishu_client.send_text(reply)
            return {"kind": "manual_publish_prompt", "reply": reply}
        if intent.kind == "publish":
            active_feishu_client = active_feishu_client or FeishuClient(self.config)
            pool = self.load_latest_candidate_pool()
            selection = parse_publish_selection(text)
            candidate, missing = resolve_pool_selection(pool, selection)
            if missing:
                if missing.startswith("请选择"):
                    reply = missing
                    active_feishu_client.send_text(reply)
                    return {"kind": "publish_prompt", "reply": reply}
                reply = f"发布失败：{missing}"
                active_feishu_client.send_text(reply)
                return {"kind": "publish", "reply": reply, "results": []}
            candidate = candidate or self.load_latest_publish_candidate()
            if not intent.platforms:
                reason = validate_publish_candidate(candidate)
                if reason:
                    reply = f"发布失败：{reason}"
                    active_feishu_client.send_text(reply)
                    return {"kind": "publish", "reply": reply, "results": []}
                self.save_pending_publish(candidate)
                active_feishu_client.send_text(PUBLISH_CONFIRM_TEXT)
                return {"kind": "publish_prompt", "reply": PUBLISH_CONFIRM_TEXT}
            with contextlib.suppress(Exception):
                active_feishu_client.send_text(publish_ack_text())
            result = self.publish_candidate(
                intent.platforms,
                candidate,
                publish_client=publish_client,
            )
            active_feishu_client.send_text(result["reply"])
            return result
        if intent.kind == "chat":
            if not self.config.get("general_answer_enabled", False):
                return None
            active_feishu_client = active_feishu_client or FeishuClient(self.config)
            try:
                client = general_client or general_answer_client_from_config(self.config)
                reply = client.answer(text)
            except Exception as exc:
                reply = general_answer_failure_text(exc)
            active_feishu_client.send_text(reply)
            return {"kind": "chat", "reply": reply}
        if intent.kind == "image_inbox":
            active_feishu_client = active_feishu_client or FeishuClient(self.config)
            reply = image_inbox_reply_text(self.config)
            active_feishu_client.send_text(reply)
            return {"kind": "image_inbox", "reply": reply}
        count = parse_trigger_command(
            event.get("content", ""), int(self.config["max_count_per_request"])
        )
        if count is None:
            return None
        if self.config.get("send_ack_on_trigger", True):
            active_feishu_client = active_feishu_client or FeishuClient(self.config)
            with contextlib.suppress(Exception):
                active_feishu_client.send_text(trigger_ack_text(count))
        return self.run_batch(
            count=count,
            openai_client=openai_client,
            feishu_client=active_feishu_client,
            no_commit=no_commit,
        )

    @property
    def poll_state_path(self) -> Path:
        return self.state_dir / "poll_state.json"

    def load_poll_position(self) -> int | None:
        if not self.poll_state_path.exists():
            return None
        try:
            data = json.loads(self.poll_state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        if data.get("chat_id") != self.config["feishu_chat_id"]:
            return None
        value = data.get("last_position")
        return int(value) if value is not None else None

    def save_poll_position(self, position: int) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.poll_state_path.write_text(
            json.dumps(
                {
                    "chat_id": self.config["feishu_chat_id"],
                    "last_position": position,
                    "updated_at": now_iso(self.config["timezone"]),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def process_polled_messages(
        self,
        messages: list[dict[str, Any]],
        last_position: int,
        openai_client: Any | None = None,
        feishu_client: Any | None = None,
        general_client: Any | None = None,
        publish_client: Any | None = None,
        intent_client: Any | None = None,
        no_commit: bool = False,
    ) -> tuple[int, int]:
        handled = 0
        max_seen = last_position
        for message in sorted(messages, key=message_position):
            position = message_position(message)
            if position <= last_position:
                continue
            max_seen = max(max_seen, position)
            if not (is_user_text_message(message) or is_user_image_message(message)):
                continue
            result = self.handle_event(
                {
                    "chat_id": message.get("chat_id"),
                    "content": message.get("content", ""),
                    "msg_type": message.get("msg_type"),
                    "message_id": message.get("message_id"),
                },
                openai_client=openai_client,
                feishu_client=feishu_client,
                general_client=general_client,
                publish_client=publish_client,
                intent_client=intent_client,
                no_commit=no_commit,
            )
            if result:
                handled += 1
        return max_seen, handled

    def _run_one(
        self,
        selection: Selection,
        openai_client: Any,
        feishu_client: Any,
        no_commit: bool,
    ) -> dict[str, Any]:
        output_dir = self.output_root / selection.run_id
        output_dir.mkdir(parents=True, exist_ok=True)
        image_path = output_dir / "image.png"
        caption_path = output_dir / "caption.md"
        metadata_path = output_dir / "metadata.json"
        started_at = now_iso(self.config["timezone"])
        metadata: dict[str, Any] = {
            "run_id": selection.run_id,
            "started_at": started_at,
            "status": "started",
            "reference_image": str(selection.reference_image),
            "character": dataclasses.asdict(selection.character),
            "prompt": selection.prompt,
        }
        try:
            image_info = openai_client.generate_image(selection, image_path)
            caption = openai_client.generate_caption(selection)
            text = caption_text(caption)
            caption_path.write_text(text, encoding="utf-8")
            feishu_info = feishu_client.send_post(image_path, text)
            commit_info: dict[str, Any] = {"committed": False}
            if not no_commit:
                commit_info = self.commit_materials(selection)
            metadata.update(
                {
                    "status": "completed_uncommitted" if no_commit else "completed",
                    "finished_at": now_iso(self.config["timezone"]),
                    "image": str(image_path),
                    "caption": caption,
                    "caption_path": str(caption_path),
                    "metadata_path": str(metadata_path),
                    "image_generation": image_info,
                    "feishu": feishu_info,
                    "commit": commit_info,
                }
            )
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            json_line(self.runs_path, metadata)
            self.save_latest_publish_candidate(metadata)
            return metadata
        except Exception as exc:
            metadata.update(
                {
                    "status": "failed",
                    "finished_at": now_iso(self.config["timezone"]),
                    "error": str(exc),
                    "image": str(image_path) if image_path.exists() else None,
                }
            )
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            json_line(self.runs_path, metadata)
            raise

    def commit_materials(self, selection: Selection) -> dict[str, Any]:
        target = selection.reference_image.with_name(
            f"{selection.run_id}__{selection.reference_image.name}"
        )
        if target.exists():
            raise WorkflowError(f"used image target already exists: {target}")
        selection.reference_image.rename(target)
        append_character_used_marker(
            Path(self.config["character_pool_path"]), selection.character, selection.run_id
        )
        return {
            "committed": True,
            "renamed_reference_image": str(target),
            "character_used_marker": f"[USED:{selection.run_id}]",
        }

    def send_failure_notice(self, text: str, feishu_client: Any | None = None) -> None:
        client = feishu_client or FeishuClient(self.config)
        client.send_text(failure_notice_text(text))

    def status_report(self) -> dict[str, Any]:
        date_str = today_string(self.config["timezone"])
        images = list_reference_images(self.config)
        characters = parse_character_pool(Path(self.config["character_pool_path"]))
        available_characters = [c for c in characters if not c.used]
        try:
            next_selection = self.preview(count=1, date_str=date_str)[0]
        except Exception as exc:
            next_selection = {"error": str(exc)}
        return {
            "now": now_iso(self.config["timezone"]),
            "date": date_str,
            "feishu_chat_id": self.config["feishu_chat_id"],
            "feishu_profile": self.config.get("feishu_profile") or "active",
            "feishu_read_as": self.config.get("feishu_read_as") or "user",
            "send_ack_on_trigger": self.config.get("send_ack_on_trigger", True),
            "general_answer_enabled": self.config.get("general_answer_enabled", False),
            "general_answer_provider": self.config.get("general_answer_provider", "openai"),
            "general_answer_model": self.config.get("general_answer_model"),
            "available_reference_images": len(images),
            "available_characters": len(available_characters),
            "next": next_selection,
            "latest_run": last_json_line(self.runs_path),
            "latest_publish_candidate": self.load_latest_publish_candidate(),
            "latest_candidate_pool": self.load_latest_candidate_pool(),
            "poll_position": self.load_poll_position(),
            "paths": {
                "reference_image_dir": self.config["reference_image_dir"],
                "character_pool_path": self.config["character_pool_path"],
                "output_root": self.config["output_root"],
                "runs_log": str(self.runs_path),
                "candidate_pool": str(self.latest_candidate_pool_path),
                "publish_log": str(self.publish_log_path),
            },
        }

    def feishu_layer_report(self) -> dict[str, Any]:
        feishu = FeishuClient(self.config)

        def safe(name: str, fn: Any) -> dict[str, Any]:
            try:
                return {"ok": True, "data": fn()}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        return {
            "checked_at": now_iso(self.config["timezone"]),
            "active_chat_id": self.config["feishu_chat_id"],
            "lark_profile": self.config.get("feishu_profile") or "active",
            "read_identity": self.config.get("feishu_read_as") or "user",
            "send_identity": self.config.get("feishu_send_as", "bot"),
            "active_trigger_mode": "poll",
            "trigger_messages": [
                "生成今天文章",
                "发今天文章",
                "生成2篇",
                "今天发3篇",
                "发布到小红书",
                "发布到公众号",
                "两个平台都发",
            ],
            "intent_routing": {
                "workflow": "小红书/公众号/生图/发文/图文意图",
                "publish": "已生成图片和文案后的发布意图；平台不明确时先询问",
                "image_inbox": "保存图片/收图/发图到电脑意图，提示去图片收件箱 bot",
                "general_chat": "其它普通文本消息",
                "general_answer_enabled": self.config.get("general_answer_enabled", False),
                "general_answer_provider": self.config.get("general_answer_provider", "openai"),
                "general_answer_model": self.config.get("general_answer_model"),
            },
            "workflow_interfaces": {
                "read_triggers": "lark-cli [--profile <feishu_profile>] im +chat-messages-list --as user",
                "upload_image": "lark-cli [--profile <feishu_profile>] im images create --as bot",
                "send_image": "lark-cli [--profile <feishu_profile>] im +messages-send --image",
                "send_caption": "lark-cli [--profile <feishu_profile>] im +messages-send --msg-type text --content",
                "optional_event_stream": "lark-cli [--profile <feishu_profile>] event consume im.message.receive_v1 --as bot",
            },
            "chat_info": safe("chat_info", feishu.chat_info),
            "chat_bots": safe("chat_bots", feishu.chat_bots),
            "recent_messages": safe("recent_messages", lambda: feishu.list_recent_messages(page_size=5)),
            "event_inventory": safe("event_inventory", feishu.event_inventory_summary),
            "notes": [
                "Current workflow owns only one feishu_chat_id.",
                "Multiple bots may be present in the same Feishu chat; routing is controlled by chat_id plus trigger text.",
                "Caption output is plain text for copy/paste, not markdown/post.",
            ],
        }

    def doctor_report(
        self,
        check_openai: bool = False,
        check_image: bool = False,
        check_wechat_browser: bool = False,
        login_wechat_browser: bool = False,
    ) -> dict[str, Any]:
        report: dict[str, Any] = {
            "checked_at": now_iso(self.config["timezone"]),
            "checks": {},
        }
        checks = report["checks"]
        reference_dir = Path(self.config["reference_image_dir"])
        character_pool = Path(self.config["character_pool_path"])
        api_key = Path(self.config["api_key_path"])
        checks["reference_image_dir"] = {
            "ok": reference_dir.exists(),
            "path": str(reference_dir),
            "available": len(list_reference_images(self.config)) if reference_dir.exists() else 0,
        }
        checks["character_pool"] = {
            "ok": character_pool.exists(),
            "path": str(character_pool),
            "available": (
                len([c for c in parse_character_pool(character_pool) if not c.used])
                if character_pool.exists()
                else 0
            ),
        }
        checks["api_key_file"] = {
            "ok": api_key.exists() and api_key.stat().st_size > 0,
            "path": str(api_key),
        }
        checks["image_provider"] = self._check_image_provider_paths()
        try:
            feishu = FeishuClient(self.config)
            recent = feishu.list_recent_messages(page_size=3)
            dry = feishu.dry_run_text()
            checks["feishu_read"] = {"ok": True, "recent_count": len(recent)}
            checks["feishu_send_dry_run"] = {"ok": True, "api": dry.get("api")}
        except Exception as exc:
            checks["feishu"] = {"ok": False, "error": str(exc)}
        if check_openai:
            checks["openai"] = self._check_openai()
        if check_image:
            checks["image_generation"] = self._check_image_generation()
        if check_wechat_browser:
            checks["wechat_browser"] = LocalPublisher(
                self.config, self.log_publish_event
            ).check_wechat_browser()
        if login_wechat_browser:
            checks["wechat_browser_login"] = LocalPublisher(
                self.config, self.log_publish_event
            ).login_wechat_browser()
        return report

    def _check_image_provider_paths(self) -> dict[str, Any]:
        provider = str(self.config.get("image_provider", "openai")).strip().lower()
        if provider in {"vs_plugin", "vscode", "codex"}:
            client = VSPluginWorkflowClient(self.config)
            return {
                "ok": client.script_path.exists()
                and Path(client.codex_home).exists()
                and Path(client.codex_exe).exists(),
                "provider": "vs_plugin",
                "script": str(client.script_path),
                "script_exists": client.script_path.exists(),
                "codex_home": client.codex_home,
                "codex_home_exists": Path(client.codex_home).exists(),
                "codex_exe": client.codex_exe,
                "codex_exe_exists": Path(client.codex_exe).exists(),
            }
        if provider in {"openai", "api"}:
            return {
                "ok": api_key_path_ok(self.config),
                "provider": "openai",
                "api_key_path": self.config.get("api_key_path"),
            }
        if provider == "mock":
            return {"ok": True, "provider": "mock"}
        return {"ok": False, "provider": provider, "error": "unknown image_provider"}

    def _check_openai(self) -> dict[str, Any]:
        try:
            from openai import OpenAI

            client = OpenAI(api_key=read_api_key(Path(self.config["api_key_path"])))
            response = client.responses.create(
                model=self.config["text_model"],
                input="Return the single word OK.",
                max_output_tokens=16,
                timeout=30,
            )
            return {
                "ok": True,
                "response_id": getattr(response, "id", None),
                "text": extract_response_text(response)[:50],
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _check_image_generation(self) -> dict[str, Any]:
        try:
            selection = self.preview(count=1)[0]
            selection_obj = Selection(
                run_id=selection["run_id"],
                reference_image=Path(selection["reference_image"]),
                character=Character(
                    ordinal=selection["character"]["ordinal"],
                    name=selection["character"]["name"],
                    work=selection["character"]["work"],
                    line_index=-1,
                    line="",
                    used=False,
                ),
                prompt=selection["prompt"],
            )
            probe_path = self.state_dir / "doctor_image_probe.png"
            self.ensure_dirs()
            info = workflow_client_from_config(self.config).generate_image(
                selection_obj, probe_path
            )
            return {
                "ok": True,
                "probe_image": str(probe_path),
                "provider": info.get("provider", self.config.get("image_provider", "openai")),
                "request_id": info.get("request_id"),
                "thread_id": info.get("thread_id"),
                "events_log": info.get("events_log"),
                "elapsed_seconds": info.get("elapsed_seconds"),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def selection_to_dict(selection: Selection) -> dict[str, Any]:
    clean_work = selection.character.work.replace("《", "").replace("》", "")
    return {
        "run_id": selection.run_id,
        "reference_image": str(selection.reference_image),
        "character": {
            "ordinal": selection.character.ordinal,
            "name": selection.character.name,
            "work": selection.character.work,
        },
        "prompt": selection.prompt,
        "caption_preview": {
            "provider": "ai_text",
            "title_format": f"{selection.character.name} | {clean_work}",
            "copy_instruction": "AI 运行时只生成角色经典原话、短台词或口头禅，不加解释旁白。",
            "topics_instruction": "AI 运行时生成，最多 10 个。",
        },
    }


def clamp_count(count: int, config: dict[str, Any]) -> int:
    max_count = int(config.get("max_count_per_request", 3))
    if count < 1:
        raise WorkflowError("count must be >= 1")
    if count > max_count:
        raise WorkflowError(f"count must be <= {max_count}")
    return count


def listen(args: argparse.Namespace, workflow: Workflow) -> int:
    command = lark_command_prefix(workflow.config.get("feishu_profile")) + [
        "event",
        "consume",
        "im.message.receive_v1",
        "--as",
        "bot",
    ]
    if args.max_events:
        command += ["--max-events", str(args.max_events)]
    if args.timeout:
        command += ["--timeout", args.timeout]
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert proc.stderr is not None
    assert proc.stdout is not None
    ready = False
    while True:
        line = proc.stderr.readline()
        if not line:
            break
        print(line.rstrip(), file=sys.stderr)
        if "[event] ready" in line:
            ready = True
            break
    if not ready:
        proc.terminate()
        raise WorkflowError("lark event consumer did not become ready")

    def drain_stderr() -> None:
        assert proc.stderr is not None
        for stderr_line in proc.stderr:
            print(stderr_line.rstrip(), file=sys.stderr)

    threading.Thread(target=drain_stderr, daemon=True).start()

    openai_client = MockOpenAIWorkflowClient() if args.mock_openai else None
    feishu_client = MockFeishuClient() if args.mock_feishu else None
    target_chat_id = workflow.config["feishu_chat_id"]
    try:
        for line in proc.stdout:
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("chat_id") != target_chat_id:
                continue
            try:
                workflow.handle_event(
                    event,
                    openai_client=openai_client,
                    feishu_client=feishu_client,
                    no_commit=args.no_commit,
                )
            except Exception as exc:
                print(f"workflow failed for event {event.get('event_id')}: {exc}", file=sys.stderr)
                if not args.mock_feishu:
                    with contextlib.suppress(Exception):
                        workflow.send_failure_notice(str(exc))
    finally:
        if proc.stdin:
            proc.stdin.close()
        with contextlib.suppress(Exception):
            proc.terminate()
        proc.wait(timeout=10)
    return 0


def poll(args: argparse.Namespace, workflow: Workflow) -> int:
    workflow.ensure_dirs()
    feishu_client = MockFeishuClient() if args.mock_feishu else FeishuClient(workflow.config)
    openai_client = MockOpenAIWorkflowClient() if args.mock_openai else None
    reader = FeishuClient(workflow.config)
    if args.arm_latest:
        messages = reader.list_recent_messages(page_size=args.page_size)
        latest = max([message_position(m) for m in messages], default=0)
        workflow.save_poll_position(latest)
        print(f"poll armed at message_position={latest}")
        if args.timeout == 0 and args.max_triggers == 0:
            return 0
    last_position = workflow.load_poll_position()
    if last_position is None:
        messages = reader.list_recent_messages(page_size=args.page_size)
        latest = max([message_position(m) for m in messages], default=0)
        if not args.process_existing:
            workflow.save_poll_position(latest)
            last_position = latest
            print(f"poll armed at message_position={latest}")
        else:
            last_position = 0

    deadline = time.monotonic() + args.timeout if args.timeout else None
    handled_total = 0
    while True:
        try:
            messages = reader.list_recent_messages(page_size=args.page_size)
        except Exception as exc:
            print(f"poll read failed: {exc}", file=sys.stderr)
            if deadline is not None and time.monotonic() >= deadline:
                return 0
            time.sleep(args.interval)
            continue
        try:
            max_seen, handled = workflow.process_polled_messages(
                messages,
                last_position,
                openai_client=openai_client,
                feishu_client=feishu_client,
                no_commit=args.no_commit,
            )
        except Exception as exc:
            print(f"poll workflow failed: {exc}", file=sys.stderr)
            if not args.mock_feishu:
                with contextlib.suppress(Exception):
                    workflow.send_failure_notice(str(exc), feishu_client)
            max_seen = max([message_position(m) for m in messages], default=last_position)
            handled = 0
        if max_seen != last_position:
            workflow.save_poll_position(max_seen)
            last_position = max_seen
        handled_total += handled
        if args.max_triggers and handled_total >= args.max_triggers:
            return 0
        if deadline is not None and time.monotonic() >= deadline:
            return 0
        time.sleep(args.interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="XHS/GZH post material workflow")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    sub = parser.add_subparsers(dest="command", required=True)

    dry = sub.add_parser("dry-run", help="preview selections without API, send, or commit")
    dry.add_argument("--count", type=int, default=1)
    dry.add_argument("--date")

    sub.add_parser("status", help="show current material and run state")
    sub.add_parser("feishu-map", help="show active Feishu chat, bots, and interfaces")

    doctor_parser = sub.add_parser("doctor", help="check local paths and Feishu readiness")
    doctor_parser.add_argument("--check-openai", action="store_true")
    doctor_parser.add_argument("--check-image", action="store_true")
    doctor_parser.add_argument("--check-wechat-browser", action="store_true")
    doctor_parser.add_argument("--login-wechat-browser", action="store_true")

    once = sub.add_parser("once", help="generate and send one batch")
    once.add_argument("--count", type=int, default=1)
    once.add_argument("--date")
    once.add_argument("--mock-openai", action="store_true")
    once.add_argument("--mock-feishu", action="store_true")
    once.add_argument("--no-commit", action="store_true")

    scheduled = sub.add_parser("scheduled-daily-run", help="generate one daily candidate and stop at publish confirmation gates")
    scheduled.add_argument("--count", type=int, default=0)
    scheduled.add_argument("--date")
    scheduled.add_argument("--platforms", default="")
    scheduled.add_argument("--mock-openai", action="store_true")
    scheduled.add_argument("--mock-feishu", action="store_true")
    scheduled.add_argument("--no-commit", action="store_true")

    listener = sub.add_parser("listen", help="listen for Feishu trigger messages")
    listener.add_argument("--max-events", type=int, default=0)
    listener.add_argument("--timeout")
    listener.add_argument("--mock-openai", action="store_true")
    listener.add_argument("--mock-feishu", action="store_true")
    listener.add_argument("--no-commit", action="store_true")

    poller = sub.add_parser("poll", help="poll target Feishu chat for trigger messages")
    poller.add_argument("--interval", type=float, default=5.0)
    poller.add_argument("--timeout", type=float, default=0)
    poller.add_argument("--page-size", type=int, default=20)
    poller.add_argument("--max-triggers", type=int, default=0)
    poller.add_argument("--process-existing", action="store_true")
    poller.add_argument("--arm-latest", action="store_true")
    poller.add_argument("--mock-openai", action="store_true")
    poller.add_argument("--mock-feishu", action="store_true")
    poller.add_argument("--no-commit", action="store_true")

    sub.add_parser("xhs-vision-dry-run", help="run XHS vision probe dry-run and wait for confirm")
    confirm = sub.add_parser("xhs-vision-confirm", help="confirm an awaiting XHS vision publish")
    confirm.add_argument("--run-id", default="")
    sub.add_parser("wechat-mp-prepare", help="run WeChat MP prepare for latest candidate and wait for confirm")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(Path(args.config))
    workflow = Workflow(config)
    if args.command == "dry-run":
        count = clamp_count(args.count, config)
        preview = workflow.preview(count=count, date_str=args.date)
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return 0
    if args.command == "status":
        print(json.dumps(workflow.status_report(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "feishu-map":
        print(json.dumps(workflow.feishu_layer_report(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "doctor":
        print(
            json.dumps(
                workflow.doctor_report(
                    check_openai=args.check_openai,
                    check_image=args.check_image,
                    check_wechat_browser=args.check_wechat_browser,
                    login_wechat_browser=args.login_wechat_browser,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "once":
        count = clamp_count(args.count, config)
        openai_client = MockOpenAIWorkflowClient() if args.mock_openai else None
        feishu_client = MockFeishuClient() if args.mock_feishu else None
        results = workflow.run_batch(
            count=count,
            date_str=args.date,
            openai_client=openai_client,
            feishu_client=feishu_client,
            no_commit=args.no_commit,
        )
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    if args.command == "scheduled-daily-run":
        openai_client = MockOpenAIWorkflowClient() if args.mock_openai else None
        feishu_client = MockFeishuClient() if args.mock_feishu else None
        summary = workflow.scheduled_daily_run(
            count=args.count or None,
            date_str=args.date,
            platforms=scheduled_platforms_from_config(config, args.platforms or None),
            openai_client=openai_client,
            feishu_client=feishu_client,
            no_commit=args.no_commit,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary.get("ok") else 1
    if args.command == "listen":
        return listen(args, workflow)
    if args.command == "poll":
        return poll(args, workflow)
    if args.command == "xhs-vision-dry-run":
        state = xhs_vision_bridge.start_dry_run(config)
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0 if state.get("status") == "awaiting_confirm" else 1
    if args.command == "xhs-vision-confirm":
        state = xhs_vision_bridge.confirm_publish(config, run_id=args.run_id or None)
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0 if state.get("status") in {"submitted", "publish_attempted"} else 1
    if args.command == "wechat-mp-prepare":
        result = workflow.handle_wechat_mp_prepare()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        state = result.get("state") if isinstance(result.get("state"), dict) else {}
        return 0 if state.get("status") == "awaiting_confirm" else 1
    raise WorkflowError(f"unknown command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
