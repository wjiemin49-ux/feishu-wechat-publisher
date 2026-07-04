from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "config.json"
DEFAULT_PROBE_DIR = (ROOT.parent / "xhs-probe").resolve()
DEFAULT_PROBE_PYTHON = DEFAULT_PROBE_DIR / ".venv" / "Scripts" / "python.exe"
PROBE_SCRIPT_NAME = "run_xhs_probe.py"
STATE_FILE_NAME = "xhs_vision_publish_state.json"
ATTEMPTS_FILE_NAME = "xhs_vision_publish_attempts.jsonl"
SECRET_RE = re.compile(
    r"(?i)\b(access[_-]?token|api[_-]?key|authorization|cookie|secret|webhook|session)\b"
    r"(\s*[:=]\s*)([^\s,;\"'}]+)"
)
URL_TOKEN_RE = re.compile(r"(?i)([?&](?:token|access_token|key|secret|signature|code)=)[^&#\s]+")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    data["_config_path"] = str(path)
    data.setdefault("_workspace_root", str(path.resolve().parents[2]))
    return data


def state_dir(config: dict[str, Any] | None = None) -> Path:
    config = config or load_config()
    return Path(str(config.get("state_dir") or ROOT / "state"))


def probe_dir(config: dict[str, Any] | None = None) -> Path:
    config = config or load_config()
    return Path(str(config.get("xhs_vision_probe_dir") or DEFAULT_PROBE_DIR))


def probe_python(config: dict[str, Any] | None = None) -> Path:
    config = config or load_config()
    return Path(str(config.get("xhs_vision_probe_python") or DEFAULT_PROBE_PYTHON))


def probe_logs_path(config: dict[str, Any] | None = None) -> Path:
    return probe_dir(config) / "logs" / "run.jsonl"


def load_latest_candidate(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or load_config()
    path = state_dir(config) / "latest_publish_candidate.json"
    if not path.exists():
        raise FileNotFoundError(f"latest publish candidate missing: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    publish = raw.get("publish") if isinstance(raw.get("publish"), dict) else {}
    title = str(publish.get("title") or "").strip()
    body = _body_from_publish(publish)
    if not title or not body:
        fallback = _body_from_caption_path(raw)
        if not title:
            title = fallback.get("title", "")
        if not body:
            body = fallback.get("body", "")
    candidate = {
        "run_id": str(raw.get("run_id") or raw.get("batch_id") or "").strip(),
        "batch_id": raw.get("batch_id"),
        "image": str(raw.get("image") or "").strip(),
        "title": title or "图文",
        "body": body or "12345",
        "caption_path": raw.get("caption_path"),
        "metadata_path": raw.get("metadata_path"),
        "created_at": raw.get("created_at"),
    }
    if not candidate["run_id"]:
        candidate["run_id"] = dt.datetime.now().strftime("xhs-%Y%m%d-%H%M%S")
    if not candidate["image"]:
        raise ValueError("latest publish candidate has no image field")
    image_path = Path(candidate["image"])
    if not image_path.exists() or not image_path.is_file():
        raise FileNotFoundError(f"candidate image missing: {image_path}")
    return candidate


def _body_from_publish(publish: dict[str, Any]) -> str:
    note = str(publish.get("note") or "").strip()
    tags = publish.get("tags") if isinstance(publish.get("tags"), list) else []
    tag_line = " ".join(f"#{str(tag).lstrip('#')}" for tag in tags if str(tag).strip())
    if note and tag_line:
        return f"{note}\n\n{tag_line}"
    if note:
        return note
    text = str(publish.get("text") or "").strip()
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) <= 1:
        return text
    return "\n".join(lines[1:]).strip() or text


def _body_from_caption_path(raw: dict[str, Any]) -> dict[str, str]:
    caption_path = raw.get("caption_path")
    if not caption_path:
        return {"title": "", "body": ""}
    path = Path(str(caption_path))
    if not path.exists() or not path.is_file():
        return {"title": "", "body": ""}
    lines = path.read_text(encoding="utf-8").splitlines()
    title = next((line.strip() for line in lines if line.strip()), "")
    body_lines = lines[1:] if title else lines
    return {"title": title, "body": "\n".join(body_lines).strip()}


def run_probe_dry_run(candidate: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or load_config()
    command = [
        str(probe_python(config)),
        PROBE_SCRIPT_NAME,
        "dry-run",
        "--image",
        str(candidate["image"]),
        "--title",
        str(candidate["title"]),
        "--body",
        str(candidate["body"]),
        "--close-after",
    ]
    result = _run_probe(command, int(config.get("xhs_vision_dry_run_timeout_seconds", 600)), config)
    parsed = result["parsed"]
    return {
        "dry_run_completed": bool(parsed.get("dry_run_completed")),
        "image_uploaded": bool(parsed.get("image_uploaded")),
        "title_filled": bool(parsed.get("title_filled")),
        "body_filled": bool(parsed.get("body_filled")),
        "publish_button_visible": bool(parsed.get("publish_button_visible")),
        "risk_warning_found": bool(parsed.get("risk_warning_found")),
        "screenshot_path": str(parsed.get("screenshot_path") or ""),
        "glm_result_path": str(parsed.get("glm_result_path") or ""),
        "probe_run_id": result.get("probe_run_id") or "",
        "stdout_tail": result["stdout_tail"],
        "stderr_tail": result["stderr_tail"],
        "returncode": result["returncode"],
        "error": str(parsed.get("error") or (f"probe exited {result['returncode']}" if result["returncode"] else "")),
    }


def run_probe_publish_once(
    candidate: dict[str, Any],
    probe_context: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = config or load_config()
    if probe_context:
        dry_result = probe_context.get("dry_run_result") or probe_context
        if not dry_result.get("dry_run_completed"):
            return _publish_blocked_result("dry-run has not completed successfully")
        if dry_result.get("risk_warning_found"):
            return _publish_blocked_result("dry-run found risk warning")
    command = [
        str(probe_python(config)),
        PROBE_SCRIPT_NAME,
        "publish-once",
        "--image",
        str(candidate["image"]),
        "--title",
        str(candidate["title"]),
        "--body",
        str(candidate["body"]),
        "--close-after",
    ]
    result = _run_probe(command, int(config.get("xhs_vision_publish_timeout_seconds", 900)), config)
    parsed = result["parsed"]
    publish_clicked = bool(parsed.get("publish_clicked"))
    risk_warning = bool(parsed.get("risk_warning_found"))
    publish_completed = bool(parsed.get("publish_completed"))
    screenshot = str(
        parsed.get("after_screenshot_path")
        or parsed.get("screenshot_path")
        or parsed.get("before_screenshot_path")
        or ""
    )
    return {
        "publish_attempted": bool(publish_clicked or publish_completed),
        "submitted_or_reviewing": bool(publish_completed),
        "publish_completed": publish_completed,
        "publish_clicked": publish_clicked,
        "post_click_hint": str(parsed.get("post_click_hint") or ""),
        "risk_warning_found": risk_warning,
        "screenshot_path": screenshot,
        "before_screenshot_path": str(parsed.get("before_screenshot_path") or ""),
        "after_screenshot_path": str(parsed.get("after_screenshot_path") or ""),
        "glm_result_path": str(parsed.get("after_glm_result_path") or parsed.get("glm_result_path") or ""),
        "probe_run_id": result.get("probe_run_id") or "",
        "stdout_tail": result["stdout_tail"],
        "stderr_tail": result["stderr_tail"],
        "returncode": result["returncode"],
        "error": str(parsed.get("error") or ("" if publish_clicked else f"probe exited {result['returncode']}" if result["returncode"] else "")),
    }


def _publish_blocked_result(error: str) -> dict[str, Any]:
    return {
        "publish_attempted": False,
        "submitted_or_reviewing": False,
        "publish_completed": False,
        "publish_clicked": False,
        "risk_warning_found": False,
        "screenshot_path": "",
        "glm_result_path": "",
        "probe_run_id": "",
        "stdout_tail": "",
        "stderr_tail": "",
        "returncode": 0,
        "error": error,
    }


def normalize_candidate(raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get("body") and raw.get("title"):
        candidate = dict(raw)
    else:
        publish = raw.get("publish") if isinstance(raw.get("publish"), dict) else {}
        candidate = {
            "run_id": str(raw.get("run_id") or raw.get("batch_id") or "").strip(),
            "batch_id": raw.get("batch_id"),
            "image": str(raw.get("image") or "").strip(),
            "title": str(publish.get("title") or raw.get("title") or "图文").strip(),
            "body": _body_from_publish(publish) or _body_from_caption_path(raw).get("body", "") or "12345",
            "caption_path": raw.get("caption_path"),
            "metadata_path": raw.get("metadata_path"),
            "created_at": raw.get("created_at"),
        }
    if not candidate.get("run_id"):
        candidate["run_id"] = dt.datetime.now().strftime("xhs-%Y%m%d-%H%M%S")
    image_path = Path(str(candidate.get("image") or ""))
    if not image_path.exists() or not image_path.is_file():
        raise FileNotFoundError(f"candidate image missing: {image_path}")
    return candidate


def same_path(left: str, right: str) -> bool:
    try:
        return str(Path(left).resolve()).lower() == str(Path(right).resolve()).lower()
    except OSError:
        return str(left).lower() == str(right).lower()


def start_dry_run(config: dict[str, Any] | None = None, candidate: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or load_config()
    candidate = normalize_candidate(candidate) if candidate else load_latest_candidate(config)
    dry_result = run_probe_dry_run(candidate, config)
    if dry_result.get("risk_warning_found"):
        status = "blocked_by_risk_warning"
    elif dry_result.get("dry_run_completed"):
        status = "awaiting_confirm"
    else:
        status = "dry_run_failed"
    return write_publish_state(
        {
            "xhs_workflow_run_id": candidate["run_id"],
            "candidate": candidate,
            "candidate_image": candidate["image"],
            "title": candidate["title"],
            "status": status,
            "dry_run_result": dry_result,
            "publish_result": None,
            "screenshot_path": dry_result.get("screenshot_path") or "",
        },
        config,
    )


def confirm_publish(config: dict[str, Any] | None = None, run_id: str | None = None) -> dict[str, Any]:
    config = config or load_config()
    state = load_publish_state(config)
    if not state or state.get("status") != "awaiting_confirm":
        raise RuntimeError("没有等待确认的小红书发布任务。")
    expected_run_id = str(state.get("xhs_workflow_run_id") or state.get("run_id") or "")
    if run_id and run_id != expected_run_id:
        raise RuntimeError(f"确认 run_id 不匹配：当前等待确认的是 {expected_run_id}")
    latest = load_latest_candidate(config)
    state_candidate = state.get("candidate") if isinstance(state.get("candidate"), dict) else {}
    if (
        str(latest.get("run_id") or "") != expected_run_id
        or not same_path(str(latest.get("image") or ""), str(state_candidate.get("image") or ""))
    ):
        raise RuntimeError("当前候选已变化，请重新发送“发布小红书”预检后再确认。")
    dry_result = state.get("dry_run_result") if isinstance(state.get("dry_run_result"), dict) else {}
    if not dry_result.get("dry_run_completed"):
        raise RuntimeError("dry-run 未成功，禁止 publish-once。")
    if dry_result.get("risk_warning_found"):
        raise RuntimeError("dry-run 检测到风险提示，禁止 publish-once。")
    screenshot = Path(str(dry_result.get("screenshot_path") or ""))
    if not screenshot.exists() or not screenshot.is_file():
        raise RuntimeError("dry-run 截图不存在，请重新预检。")
    candidate = state_candidate or latest
    publish_result = run_probe_publish_once(candidate, state, config)
    if publish_result.get("submitted_or_reviewing"):
        status = "submitted"
    elif publish_result.get("publish_attempted"):
        status = "publish_attempted"
    else:
        status = "publish_failed"
    return write_publish_state(
        {
            "xhs_workflow_run_id": expected_run_id,
            "candidate": candidate,
            "candidate_image": candidate.get("image"),
            "title": candidate.get("title"),
            "status": status,
            "dry_run_result": dry_result,
            "publish_result": publish_result,
            "screenshot_path": publish_result.get("screenshot_path") or dry_result.get("screenshot_path") or "",
        },
        config,
    )


def load_publish_state(config: dict[str, Any] | None = None) -> dict[str, Any] | None:
    path = state_dir(config) / STATE_FILE_NAME
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_publish_state(event: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or load_config()
    directory = state_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / STATE_FILE_NAME
    attempts_path = directory / ATTEMPTS_FILE_NAME
    previous = load_publish_state(config) or {}
    same_run = str(previous.get("xhs_workflow_run_id") or previous.get("run_id") or "") == str(
        event.get("xhs_workflow_run_id") or ""
    )
    created_at = previous.get("created_at") if same_run else None
    publish_result = event.get("publish_result")
    if isinstance(publish_result, dict) and not publish_result.get("publish_completed"):
        publish_result = dict(publish_result)
        publish_result["submitted_or_reviewing"] = False
    state = {
        "run_id": event.get("xhs_workflow_run_id"),
        "xhs_workflow_run_id": event.get("xhs_workflow_run_id"),
        "candidate": event.get("candidate"),
        "candidate_image": event.get("candidate_image"),
        "title": event.get("title"),
        "status": event.get("status"),
        "dry_run_result": event.get("dry_run_result"),
        "publish_result": publish_result,
        "screenshot_path": event.get("screenshot_path"),
        "created_at": created_at or now_iso(),
        "updated_at": now_iso(),
    }
    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(state_path)
    with attempts_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(state, ensure_ascii=False) + "\n")
    return state


def _run_probe(command: list[str], timeout: int, config: dict[str, Any]) -> dict[str, Any]:
    cwd = probe_dir(config)
    if not cwd.exists():
        raise FileNotFoundError(f"probe dir missing: {cwd}")
    if not Path(command[0]).exists():
        raise FileNotFoundError(f"probe python missing: {command[0]}")
    log_path = probe_logs_path(config)
    log_start = log_path.stat().st_size if log_path.exists() else 0
    result = subprocess.run(
        command,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=_probe_env(),
        timeout=timeout,
        check=False,
    )
    events = _read_probe_events_after(log_path, log_start)
    parsed = _parse_stdout_json(result.stdout)
    if not parsed:
        parsed = _last_summary_from_events(events)
    return {
        "returncode": result.returncode,
        "parsed": parsed,
        "probe_run_id": _probe_run_id(events, parsed),
        "stdout_tail": _tail(result.stdout),
        "stderr_tail": _tail(result.stderr),
    }


def _parse_stdout_json(stdout: str) -> dict[str, Any]:
    text = (stdout or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        with contextlib_suppress_json():
            data = json.loads(text[start : end + 1])
            return data if isinstance(data, dict) else {}
    return {}


class contextlib_suppress_json:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return exc_type is json.JSONDecodeError


def _read_probe_events_after(path: Path, offset: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                events.append(data)
    return events


def _last_summary_from_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        if event.get("event") in {
            "dry_run_completed",
            "dry_run_blocked",
            "publish_completed",
            "publish_precheck_failed",
            "publish_blocked",
            "error",
        }:
            return event
    return {}


def _probe_run_id(events: list[dict[str, Any]], parsed: dict[str, Any]) -> str:
    for event in reversed(events):
        if event.get("run_id"):
            return str(event["run_id"])
    return str(parsed.get("run_id") or "")


def _tail(text: str, limit: int = 2000) -> str:
    value = _redact_sensitive_text(text or "")
    if len(value) <= limit:
        return value
    return value[-limit:]


def _redact_sensitive_text(value: str) -> str:
    value = SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", value)
    return URL_TOKEN_RE.sub(lambda m: f"{m.group(1)}[REDACTED]", value)


def _probe_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env["PYTHONIOENCODING"] = "utf-8"
    return env
