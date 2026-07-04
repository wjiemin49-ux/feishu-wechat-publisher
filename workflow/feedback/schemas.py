from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FeedbackCommand:
    intent: str
    image_number: int | None = None
    caption_number: int | None = None
    publish_targets: tuple[str, ...] = ()
    instruction: str = ""
    preference_key: str = ""
    reason_tags: tuple[str, ...] = field(default_factory=tuple)
    source: str = "feedback"

    def target(self) -> dict[str, object]:
        payload: dict[str, object] = {}
        if self.image_number is not None:
            payload["image_number"] = self.image_number
        if self.caption_number is not None:
            payload["caption_number"] = self.caption_number
        if self.publish_targets:
            payload["publish_targets"] = list(self.publish_targets)
        if self.preference_key:
            payload["preference_key"] = self.preference_key
        return payload


@dataclass
class ActionPlan:
    reply: str
    action_taken: str
    run_id: str
    target: dict[str, object]
    apply: object

