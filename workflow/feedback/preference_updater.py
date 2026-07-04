from __future__ import annotations

from typing import Any

from .schemas import FeedbackCommand


def update_preference_profile(
    profile: dict[str, Any],
    command: FeedbackCommand,
    context: dict[str, Any],
    ts: str,
) -> dict[str, Any]:
    key = command.preference_key
    if key not in {"image_likes", "image_dislikes", "copy_likes", "copy_dislikes", "role_style_notes"}:
        key = "role_style_notes"
    entry = {
        "ts": ts,
        "run_id": context.get("run_id", ""),
        "image_id": context.get("image_id"),
        "copy_id": context.get("copy_id"),
        "role": context.get("role", ""),
        "image_path": context.get("image_path", ""),
        "instruction": command.instruction,
        "reason_tags": list(command.reason_tags),
    }
    values = profile.get(key)
    if not isinstance(values, list):
        values = []
    values.append(entry)
    profile[key] = values
    profile["updated_at"] = ts
    return profile

