from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_ANDROID_PUBLISHER_DIR = (Path(__file__).resolve().parents[1] / "wechat-mumu").resolve()
DEFAULT_MUMU_EXE = Path(r"D:\Program Files\Netease\MuMuPlayer\nx_main\MuMuNxMain.exe")
DEFAULT_MUMU_CLI = Path(r"D:\Program Files\Netease\MuMuPlayer\nx_main\mumu-cli.exe")
DEFAULT_MUMU_ADB = Path(r"D:\Program Files\Netease\MuMuPlayer\nx_device\12.0\shell\adb.exe")
DEFAULT_MUMU_DEVICE = "127.0.0.1:7555"
DEFAULT_MUMU_INDEX = "0"
SCRIPT_NAME = "wechat_mp_sticker_mumu.py"
STATE_FILE = "wechat_mp_publish_state.json"
ATTEMPTS_FILE = "wechat_mp_publish_attempts.jsonl"
SECRET_RE = re.compile(
    r"(?i)\b(access[_-]?token|api[_-]?key|authorization|cookie|secret|webhook|session)\b"
    r"(\s*[:=]\s*)([^\s,;\"'}]+)"
)
URL_TOKEN_RE = re.compile(r"(?i)([?&](?:token|access_token|key|secret|signature|code)=)[^&#\s]+")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def compact_text(value: Any, limit: int = 2000) -> str:
    text = redact_sensitive_text(value).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + f"... [truncated {len(text) - limit} chars]"


def redact_sensitive_text(value: Any) -> str:
    text = str(value or "")
    text = SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
    return URL_TOKEN_RE.sub(lambda m: f"{m.group(1)}[REDACTED]", text)


def state_dir(config: dict[str, Any]) -> Path:
    return Path(str(config["state_dir"]))


def publisher_dir(config: dict[str, Any]) -> Path:
    return Path(str(config.get("wechat_mp_publisher_dir") or DEFAULT_ANDROID_PUBLISHER_DIR))


def mumu_exe(config: dict[str, Any]) -> Path:
    return Path(str(config.get("wechat_mp_mumu_exe") or DEFAULT_MUMU_EXE))


def mumu_cli(config: dict[str, Any]) -> Path:
    return Path(str(config.get("wechat_mp_mumu_cli") or DEFAULT_MUMU_CLI))


def mumu_adb(config: dict[str, Any]) -> Path:
    return Path(str(config.get("wechat_mp_mumu_adb") or DEFAULT_MUMU_ADB))


def mumu_device(config: dict[str, Any]) -> str:
    return str(config.get("wechat_mp_mumu_device") or DEFAULT_MUMU_DEVICE)


def mumu_index(config: dict[str, Any]) -> str:
    return str(config.get("wechat_mp_mumu_index") or DEFAULT_MUMU_INDEX)


def python_command(config: dict[str, Any]) -> list[str]:
    executable = str(config.get("wechat_mp_python") or "py")
    args = config.get("wechat_mp_python_args")
    if isinstance(args, list):
        return [executable, *[str(arg) for arg in args]]
    if args:
        return [executable, str(args)]
    return [executable, "-3.12"]


def state_path(config: dict[str, Any]) -> Path:
    return state_dir(config) / STATE_FILE


def attempts_path(config: dict[str, Any]) -> Path:
    return state_dir(config) / ATTEMPTS_FILE


def load_publish_state(config: dict[str, Any]) -> dict[str, Any] | None:
    path = state_path(config)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def write_publish_state(event: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    path = state_path(config)
    existing = load_publish_state(config) or {}
    timestamp = now_iso()
    same_run = str(existing.get("xhs_workflow_run_id") or existing.get("run_id") or "") == str(
        event.get("xhs_workflow_run_id") or ""
    )
    state = dict(event)
    state["created_at"] = str(event.get("created_at") or (existing.get("created_at") if same_run else "") or timestamp)
    state["updated_at"] = timestamp
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)
    write_jsonl(attempts_path(config), state)
    return state


def parse_json_stdout(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        return {}
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(text[index:])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return {}


def run_command(command: list[str], timeout: int, config: dict[str, Any]) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            cwd=str(publisher_dir(config)),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            env=python_env(),
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
        return {
            "returncode": -1,
            "parsed": {},
            "stdout_tail": compact_text(stdout),
            "stderr_tail": compact_text((stderr or "") + f"\n命令超时：{timeout} 秒"),
        }
    parsed = parse_json_stdout(result.stdout)
    return {
        "returncode": result.returncode,
        "parsed": parsed,
        "stdout_tail": compact_text(result.stdout),
        "stderr_tail": compact_text(result.stderr),
    }


def python_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def ensure_mumu_running(config: dict[str, Any]) -> bool:
    if not config.get("wechat_mp_mumu_auto_start", True):
        return False
    exe = mumu_exe(config)
    cli = mumu_cli(config)
    adb = mumu_adb(config)
    device = mumu_device(config)
    if not adb.exists():
        raise RuntimeError(f"MuMu adb 不存在：{adb}")
    if cli.exists():
        subprocess.run(
            [str(cli), "control", "--vmindex", mumu_index(config), "launch"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    else:
        if not exe.exists():
            raise RuntimeError(f"MuMu 启动程序不存在：{exe}")
        subprocess.Popen([str(exe)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + int(config.get("wechat_mp_mumu_boot_timeout_seconds", 180))
    while time.monotonic() < deadline:
        subprocess.run(
            [str(adb), "connect", device],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        devices = subprocess.run(
            [str(adb), "devices"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if f"{device}\tdevice" in devices.stdout or f"{device}         device" in devices.stdout:
            return True
        time.sleep(3)
    raise RuntimeError(f"MuMu 启动后 ADB 设备未上线：{device}")


def preflight(config: dict[str, Any]) -> dict[str, Any]:
    if not config.get("wechat_mp_preflight_enabled", True):
        return {"skipped": True}
    timeout = int(config.get("wechat_mp_status_timeout_seconds") or config.get("wechat_mp_preflight_timeout_seconds", 120))
    result = run_command(
        [*python_command(config), SCRIPT_NAME, "status"],
        timeout,
        config,
    )
    if result["returncode"] != 0 and "device" in (result["stderr_tail"] + result["stdout_tail"]).lower():
        ensure_mumu_running(config)
        result = run_command(
            [*python_command(config), SCRIPT_NAME, "status"],
            timeout,
            config,
        )
    if result["returncode"] != 0:
        reason = result["stderr_tail"] or result["stdout_tail"] or "公众号助手 status 前置检查失败。"
        raise RuntimeError(f"公众号前置检查失败：{reason}")
    return result


def load_latest_candidate(config: dict[str, Any]) -> dict[str, Any]:
    path = state_dir(config) / "latest_publish_candidate.json"
    if not path.exists():
        raise RuntimeError("没有 latest_publish_candidate.json，先生成候选内容。")
    candidate = json.loads(path.read_text(encoding="utf-8"))
    reason = validate_candidate(candidate)
    if reason:
        raise RuntimeError(reason)
    return normalize_candidate(candidate)


def body_from_publish(publish: dict[str, Any]) -> str:
    body = str(publish.get("note") or publish.get("text") or "").strip()
    tags = [str(tag).strip().lstrip("#") for tag in publish.get("tags", []) if str(tag).strip()]
    if tags:
        tag_line = " ".join(f"#{tag}" for tag in tags[:10])
        body = "\n\n".join(part for part in (body, tag_line) if part).strip()
    return body


def normalize_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    publish = candidate.get("publish") if isinstance(candidate.get("publish"), dict) else {}
    return {
        "run_id": str(candidate.get("run_id") or ""),
        "image": str(candidate.get("image") or ""),
        "title": str(publish.get("title") or candidate.get("title") or "图文").strip()[:64],
        "body": body_from_publish(publish),
        "raw": candidate,
    }


def validate_candidate(candidate: dict[str, Any] | None) -> str | None:
    if not candidate:
        return "没有找到待发布候选。"
    image = Path(str(candidate.get("image") or ""))
    if not image.exists() or not image.is_file():
        return "公众号发布图片不存在。"
    publish = candidate.get("publish") if isinstance(candidate.get("publish"), dict) else {}
    if not (publish.get("title") or publish.get("note") or publish.get("text")):
        return "公众号发布文案不存在。"
    return None


def same_path(left: str, right: str) -> bool:
    try:
        return str(Path(left).resolve()).lower() == str(Path(right).resolve()).lower()
    except OSError:
        return str(left).lower() == str(right).lower()


def screenshot_from_text(text: str) -> str:
    match = re.search(r"screenshot=([^;\r\n]+\.png)", text or "")
    return match.group(1).strip() if match else ""


def start_prepare(config: dict[str, Any], candidate: dict[str, Any] | None = None) -> dict[str, Any]:
    preflight(config)
    if candidate:
        reason = validate_candidate(candidate)
        if reason:
            raise RuntimeError(reason)
        candidate = normalize_candidate(candidate)
    else:
        candidate = load_latest_candidate(config)
    command = [
        *python_command(config),
        SCRIPT_NAME,
        "prepare",
        "--image",
        candidate["image"],
        "--title",
        candidate["title"],
        "--body",
        candidate["body"],
    ]
    result = run_command(command, int(config.get("wechat_mp_prepare_timeout_seconds", 900)), config)
    parsed = result["parsed"]
    error_text = str(parsed.get("error") or result["stderr_tail"] or result["stdout_tail"] or "")
    screenshot = str(parsed.get("screenshot_path") or screenshot_from_text(error_text) or "")
    risk = bool(parsed.get("risk_warning_found"))
    completed = bool(
        result["returncode"] == 0
        and parsed.get("stopped_before_final_publish")
        and parsed.get("publish_button_visible")
        and screenshot
        and not risk
    )
    prepare_result = {
        "prepare_completed": completed,
        "stopped_before_final_publish": bool(parsed.get("stopped_before_final_publish")),
        "publish_button_visible": bool(parsed.get("publish_button_visible")),
        "risk_warning_found": risk,
        "risk_words": parsed.get("risk_words") or [],
        "screenshot_path": screenshot,
        "ui_dump_path": str(parsed.get("ui_dump_path") or ""),
        "state_path": str(parsed.get("state_path") or ""),
        "wechat_run_id": str(parsed.get("run_id") or ""),
        "stdout_tail": result["stdout_tail"],
        "stderr_tail": result["stderr_tail"],
        "returncode": result["returncode"],
        "error": "" if completed else str(parsed.get("error") or result["stderr_tail"] or "prepare 未完成"),
    }
    if risk:
        status = "blocked_by_risk_warning"
    elif completed:
        status = "awaiting_confirm"
    else:
        status = "prepare_failed"
    return write_publish_state(
        {
            "xhs_workflow_run_id": candidate["run_id"],
            "candidate": candidate["raw"],
            "candidate_image": candidate["image"],
            "title": candidate["title"],
            "status": status,
            "prepare_result": prepare_result,
            "publish_result": None,
            "screenshot_path": screenshot,
        },
        config,
    )


def confirm_publish(config: dict[str, Any], run_id: str | None = None) -> dict[str, Any]:
    state = load_publish_state(config)
    if not state or state.get("status") != "awaiting_confirm":
        raise RuntimeError("没有等待确认的公众号发布任务。")
    expected_run_id = str(state.get("xhs_workflow_run_id") or "")
    if run_id and run_id != expected_run_id:
        raise RuntimeError(f"确认 run_id 不匹配：当前等待确认的是 {expected_run_id}")
    prepare = state.get("prepare_result") if isinstance(state.get("prepare_result"), dict) else {}
    if not prepare.get("prepare_completed") or prepare.get("risk_warning_found"):
        raise RuntimeError("公众号 prepare 未通过，禁止发表。")
    latest = load_latest_candidate(config)
    state_candidate = state.get("candidate") if isinstance(state.get("candidate"), dict) else {}
    state_normalized = normalize_candidate(state_candidate) if state_candidate else {}
    if (
        str(latest.get("run_id") or "") != expected_run_id
        or not same_path(str(latest.get("image") or ""), str(state_normalized.get("image") or ""))
    ):
        raise RuntimeError("当前候选已变化，请重新发送“发布公众号”预检后再确认。")
    screenshot = Path(str(prepare.get("screenshot_path") or ""))
    if not screenshot.exists() or not screenshot.is_file():
        raise RuntimeError("公众号预览截图不存在，请重新准备。")
    pending_state = Path(str(prepare.get("state_path") or ""))
    if not pending_state.exists() or not pending_state.is_file():
        raise RuntimeError("公众号 pending state 不存在，请重新准备。")
    pending_payload = json.loads(pending_state.read_text(encoding="utf-8"))
    prepare_run_id = str(prepare.get("wechat_run_id") or "")
    if str(pending_payload.get("run_id") or "") != prepare_run_id:
        raise RuntimeError("公众号 pending state 与预检 run_id 不匹配，请重新准备。")

    ensure_mumu_running(config)
    command = [
        *python_command(config),
        SCRIPT_NAME,
        "confirm-publish",
        "--state",
        str(pending_state),
        "--confirm",
        "可以发表",
    ]
    result = run_command(command, int(config.get("wechat_mp_confirm_timeout_seconds", 600)), config)
    parsed = result["parsed"]
    status_result = run_command(
        [*python_command(config), SCRIPT_NAME, "status"],
        int(config.get("wechat_mp_status_timeout_seconds", 120)),
        config,
    )
    status_parsed = status_result["parsed"]
    screenshot_path = str(parsed.get("after_publish_screenshot_path") or "")
    risk_words = parsed.get("after_publish_risk_words") or []
    source_matches = str(parsed.get("source_state_run_id") or "") == prepare_run_id
    status_verified = published_verified_by_status(status_parsed, str(state.get("title") or ""))
    published_clicks_completed = bool(parsed.get("published_clicks_completed"))
    published = bool(
        result["returncode"] == 0
        and published_clicks_completed
        and screenshot_path
        and source_matches
        and status_verified
        and not risk_words
    )
    status_screenshot = str(status_parsed.get("screenshot_path") or "")
    publish_result = {
        "published": published,
        "publish_attempted": published_clicks_completed,
        "published_clicks_completed": published_clicks_completed,
        "published_verified": status_verified,
        "source_state_matched": source_matches,
        "risk_warning_found": bool(risk_words),
        "risk_words": risk_words,
        "screenshot_path": status_screenshot or screenshot_path,
        "after_publish_screenshot_path": screenshot_path,
        "status_screenshot_path": status_screenshot,
        "final_dialog_screenshot_path": str(parsed.get("final_dialog_screenshot_path") or ""),
        "wechat_run_id": str(parsed.get("run_id") or ""),
        "source_state_run_id": str(parsed.get("source_state_run_id") or ""),
        "status_run_id": str(status_parsed.get("run_id") or ""),
        "status_text_sample": status_parsed.get("text_sample") or [],
        "stdout_tail": result["stdout_tail"],
        "stderr_tail": result["stderr_tail"],
        "returncode": result["returncode"],
        "error": "" if published else str(parsed.get("error") or result["stderr_tail"] or "confirm-publish 未验证已发表"),
    }
    if not source_matches:
        publish_result["error"] = "confirm-publish source_state_run_id 与预检 run_id 不匹配。"
    return write_publish_state(
        {
            "xhs_workflow_run_id": expected_run_id,
            "candidate": state.get("candidate"),
            "candidate_image": state.get("candidate_image"),
            "title": state.get("title"),
            "status": "published" if published else "publish_attempted" if published_clicks_completed else "publish_failed",
            "prepare_result": prepare,
            "publish_result": publish_result,
            "screenshot_path": publish_result.get("screenshot_path") or state.get("screenshot_path") or "",
        },
        config,
    )


def published_verified_by_status(status: dict[str, Any], title: str) -> bool:
    if status.get("visible_risk_words"):
        return False
    text = "\n".join(str(item) for item in status.get("text_sample") or [])
    if not title or title not in text:
        return False
    return "等候发表" not in text
