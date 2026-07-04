from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python always has zoneinfo here.
    ZoneInfo = None  # type: ignore

from .locks import StateFileLock


DEFAULT_PREFERENCE_PROFILE = {
    "image_likes": [],
    "image_dislikes": [],
    "copy_likes": [],
    "copy_dislikes": [],
    "role_style_notes": [],
    "updated_at": "",
}


class FeedbackStore:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.state_dir = Path(config["state_dir"])
        self.timezone = str(config.get("timezone") or "Asia/Shanghai")
        self.bot = str(config.get("feishu_profile") or "xhs-content-bot")
        self.lock_timeout_seconds = float(config.get("feedback_lock_timeout_seconds", 10))
        self.lock_path = self.state_dir / "feedback_state.lock"
        self.latest_candidate_pool_path = self.state_dir / "latest_candidate_pool.json"
        self.latest_publish_candidate_path = self.state_dir / "latest_publish_candidate.json"
        self.feedback_path = self.state_dir / "feedback.jsonl"
        self.publish_queue_path = self.state_dir / "publish_queue.jsonl"
        self.rejected_candidates_path = self.state_dir / "rejected_candidates.jsonl"
        self.processed_message_ids_path = self.state_dir / "processed_message_ids.json"
        self.rewrite_queue_path = self.state_dir / "rewrite_queue.jsonl"
        self.regen_queue_path = self.state_dir / "regen_queue.jsonl"
        self.preference_profile_path = self.state_dir / "preference_profile.json"

    @contextmanager
    def locked(self) -> Iterator[None]:
        with StateFileLock(self.lock_path, timeout_seconds=self.lock_timeout_seconds):
            yield

    def now_iso(self) -> str:
        tz = self._tzinfo()
        return datetime.now(tz).replace(microsecond=0).isoformat()

    def read_json(self, path: Path) -> Any:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def write_json(self, path: Path, payload: Any) -> None:
        self._atomic_write_text(
            path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )

    def append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        self._atomic_write_text(path, existing + line + "\n")

    def load_pool(self) -> dict[str, Any] | None:
        data = self.read_json(self.latest_candidate_pool_path)
        return data if isinstance(data, dict) else None

    def load_latest_candidate(self) -> dict[str, Any] | None:
        data = self.read_json(self.latest_publish_candidate_path)
        return data if isinstance(data, dict) else None

    def write_latest_candidate(self, candidate: dict[str, Any]) -> None:
        self.write_json(self.latest_publish_candidate_path, candidate)

    def append_feedback(self, payload: dict[str, Any]) -> None:
        self.append_jsonl(self.feedback_path, payload)

    def append_publish_queue(self, payload: dict[str, Any]) -> None:
        self.append_jsonl(self.publish_queue_path, payload)

    def append_rejected_candidate(self, payload: dict[str, Any]) -> None:
        self.append_jsonl(self.rejected_candidates_path, payload)

    def append_rewrite_queue(self, payload: dict[str, Any]) -> None:
        self.append_jsonl(self.rewrite_queue_path, payload)

    def append_regen_queue(self, payload: dict[str, Any]) -> None:
        self.append_jsonl(self.regen_queue_path, payload)

    def load_preference_profile(self) -> dict[str, Any]:
        data = self.read_json(self.preference_profile_path)
        profile = dict(DEFAULT_PREFERENCE_PROFILE)
        if isinstance(data, dict):
            for key in profile:
                if key in data:
                    profile[key] = data[key]
        for key in ("image_likes", "image_dislikes", "copy_likes", "copy_dislikes", "role_style_notes"):
            if not isinstance(profile.get(key), list):
                profile[key] = []
        return profile

    def write_preference_profile(self, profile: dict[str, Any]) -> None:
        self.write_json(self.preference_profile_path, profile)

    def is_processed(self, message_id: str) -> bool:
        if not message_id:
            return False
        data = self._load_processed()
        return message_id in data["message_ids"]

    def mark_processed(self, message_id: str, ts: str) -> None:
        if not message_id:
            return
        data = self._load_processed()
        data["message_ids"][message_id] = ts
        data["updated_at"] = ts
        self.write_json(self.processed_message_ids_path, data)

    def _load_processed(self) -> dict[str, Any]:
        data = self.read_json(self.processed_message_ids_path)
        if not isinstance(data, dict):
            return {"message_ids": {}, "updated_at": ""}
        ids = data.get("message_ids")
        if isinstance(ids, list):
            ids = {str(item): "" for item in ids}
        if not isinstance(ids, dict):
            ids = {}
        return {"message_ids": ids, "updated_at": str(data.get("updated_at") or "")}

    def _atomic_write_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp.open("w", encoding="utf-8", newline="\n") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            with contextlib_suppress_file_not_found():
                tmp.unlink()

    def _tzinfo(self) -> timezone:
        if ZoneInfo is not None:
            try:
                return ZoneInfo(self.timezone)  # type: ignore[return-value]
            except Exception:
                pass
        if self.timezone in {"Asia/Shanghai", "China Standard Time", "UTC+08:00", "+08:00"}:
            return timezone(timedelta(hours=8), name="Asia/Shanghai")
        return timezone.utc


@contextmanager
def contextlib_suppress_file_not_found() -> Iterator[None]:
    try:
        yield
    except FileNotFoundError:
        pass

