from __future__ import annotations

import copy
import uuid
from pathlib import Path
from typing import Any, Callable

from .feedback_parser import parse_feedback_command, publish_command_from_intent
from .feedback_store import FeedbackStore
from .preference_updater import update_preference_profile
from .schemas import ActionPlan, FeedbackCommand


NO_CURRENT_CANDIDATE = "当前没有可操作候选。请先生成候选，或在候选生成后回复“用第几张图”。"
FEEDBACK_HELP_TEXT = (
    "现在可以反馈生成结果了。\n"
    "选择：用第2张图 / 用第1段文案 / 用第3张图 + 第1段文案\n"
    "调整：第2张图废掉 / 文案太硬，重写口语一点 / 这张脸不像，重生成\n"
    "偏好：这个风格好，下次多用 / 以后标题别太夸张\n"
    "入队：加入待发布 / 发布公众号 / 发布小红书\n"
    "说明：只更新本地状态和待发布队列，不会真实发布。"
)


class FeedbackError(Exception):
    def __init__(self, reply: str, error: str) -> None:
        super().__init__(error)
        self.reply = reply
        self.error = error


class FeedbackRouter:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.store = FeedbackStore(config)

    def handle_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        command = parse_feedback_command(str(event.get("content", "") or ""))
        if not command:
            return None
        return self.handle_command(event, command)

    def handle_publish_intent(
        self,
        event: dict[str, Any],
        platforms: tuple[str, ...],
        image_number: int | None = None,
        caption_number: int | None = None,
    ) -> dict[str, Any]:
        command = publish_command_from_intent(
            str(event.get("content", "") or ""),
            platforms,
            image_number=image_number,
            caption_number=caption_number,
        )
        return self.handle_command(event, command)

    def handle_command(self, event: dict[str, Any], command: FeedbackCommand) -> dict[str, Any]:
        text = str(event.get("content", "") or "")
        message_id = str(event.get("message_id") or "")
        ts = self.store.now_iso()
        with self.store.locked():
            if message_id and self.store.is_processed(message_id):
                return {
                    "kind": "feedback",
                    "intent": command.intent,
                    "status": "duplicate",
                    "reply": "这条反馈已经处理过了，不会重复写入。",
                }
            try:
                plan = self._plan(command, event, ts)
            except FeedbackError as exc:
                self.store.append_feedback(
                    self._feedback_record(
                        event,
                        command,
                        ts,
                        run_id="",
                        target=command.target(),
                        action_taken="none",
                        status="failed",
                        error=exc.error,
                    )
                )
                self.store.mark_processed(message_id, ts)
                return {
                    "kind": "feedback",
                    "intent": command.intent,
                    "status": "failed",
                    "reply": exc.reply,
                    "error": exc.error,
                }

            self.store.append_feedback(
                self._feedback_record(
                    event,
                    command,
                    ts,
                    run_id=plan.run_id,
                    target=plan.target,
                    action_taken=plan.action_taken,
                    status="ok",
                    error=None,
                )
            )
            apply_action = plan.apply
            if callable(apply_action):
                apply_action()
            self.store.mark_processed(message_id, ts)
            return {
                "kind": "feedback",
                "intent": command.intent,
                "status": "ok",
                "reply": plan.reply,
                "user_text": text,
                "action_taken": plan.action_taken,
            }

    def _plan(self, command: FeedbackCommand, event: dict[str, Any], ts: str) -> ActionPlan:
        if command.intent == "help":
            return self._plan_help(command)
        if command.intent == "select":
            return self._plan_select(command)
        if command.intent == "reject":
            return self._plan_reject(command, event, ts)
        if command.intent == "queue_publish":
            return self._plan_queue_publish(command, event, ts)
        if command.intent == "rewrite":
            return self._plan_rewrite(command, event, ts)
        if command.intent == "regenerate":
            return self._plan_regenerate(command, event, ts)
        if command.intent == "preference":
            return self._plan_preference(command, ts)
        raise FeedbackError("没有识别到可执行的反馈指令。", "unknown_feedback_intent")

    def _plan_help(self, command: FeedbackCommand) -> ActionPlan:
        return ActionPlan(
            FEEDBACK_HELP_TEXT,
            "show_feedback_help",
            "",
            command.target(),
            None,
        )

    def _plan_select(self, command: FeedbackCommand) -> ActionPlan:
        pool = self._pool_or_error()
        image_number = command.image_number
        caption_number = command.caption_number
        if image_number is not None and caption_number is not None:
            candidate = self._candidate_from_pool(pool, image_number, caption_number)
            reply = f"已更新当前候选：图 {image_number} + 文案 {caption_number}。可继续说“加入待发布”或“发布公众号”。"
            action = "select_image_and_copy"
        elif image_number is not None:
            image = self._image_by_number(pool, image_number)
            current = self.store.load_latest_candidate() or self._candidate_from_pool(pool, image_number, None)
            candidate = self._with_image(current, pool, image, image_number)
            reply = f"已更新当前候选图片为第 {image_number} 张；文案保持不变。"
            action = "select_image"
        elif caption_number is not None:
            caption = self._caption_by_number(pool, caption_number)
            current = self.store.load_latest_candidate() or self._candidate_from_pool(pool, None, caption_number)
            candidate = self._with_caption(current, pool, caption, caption_number)
            reply = f"已更新当前候选文案为第 {caption_number} 段；图片保持不变。"
            action = "select_copy"
        else:
            raise FeedbackError("请选择图片或文案编号。", "missing_selection_number")

        run_id = str(candidate.get("run_id") or "")
        target = dict(candidate.get("selection") or command.target())

        def apply() -> None:
            self.store.write_latest_candidate(candidate)

        return ActionPlan(reply, action, run_id, target, apply)

    def _plan_reject(self, command: FeedbackCommand, event: dict[str, Any], ts: str) -> ActionPlan:
        if command.image_number is not None:
            pool = self._pool_or_error()
            image = self._image_by_number(pool, command.image_number)
            context = self._image_context_from_pool_item(image, command.image_number)
        else:
            current = self._current_or_error()
            context = self._image_context_from_candidate(current)
        record = {
            "job_id": self._job_id("reject"),
            "ts": ts,
            "message_id": str(event.get("message_id") or ""),
            "run_id": context.get("run_id", ""),
            "image_id": context.get("image_id"),
            "role": context.get("role", ""),
            "image_path": context.get("image_path", ""),
            "instruction": command.instruction,
            "reason_tags": list(command.reason_tags),
            "status": "rejected",
        }
        image_label = context.get("image_id") or "当前"

        def apply() -> None:
            self.store.append_rejected_candidate(record)

        return ActionPlan(
            f"已记录废弃：第 {image_label} 张图，已写入废弃候选记录。",
            "reject_image",
            str(context.get("run_id") or ""),
            {"image_number": context.get("image_id"), "image_path": context.get("image_path", "")},
            apply,
        )

    def _plan_queue_publish(self, command: FeedbackCommand, event: dict[str, Any], ts: str) -> ActionPlan:
        candidate = self._candidate_for_queue(command)
        targets = command.publish_targets or ("unspecified",)
        records = []
        for target in targets:
            status = "pending" if target == "unspecified" else "pending_confirm"
            records.append(
                {
                    "job_id": self._job_id("publish"),
                    "ts": ts,
                    "message_id": str(event.get("message_id") or ""),
                    "run_id": candidate.get("run_id", ""),
                    "publish_target": target,
                    "status": status,
                    "candidate": candidate,
                    "instruction": command.instruction,
                }
            )
        if targets == ("wechat",):
            reply = "已加入公众号待发布队列，状态 pending_confirm，不会真实发布。"
        elif targets == ("xhs",):
            reply = "已加入小红书待发布队列，状态 pending_confirm，不会真实发布。"
        elif targets == ("unspecified",):
            reply = "已加入待发布队列，状态 pending，等待你确认目标平台。"
        else:
            reply = "已加入待发布队列，状态 pending_confirm，不会真实发布。"

        def apply() -> None:
            for record in records:
                self.store.append_publish_queue(record)

        return ActionPlan(
            reply,
            "queue_publish",
            str(candidate.get("run_id") or ""),
            {"publish_targets": list(targets), "selection": candidate.get("selection", {})},
            apply,
        )

    def _plan_rewrite(self, command: FeedbackCommand, event: dict[str, Any], ts: str) -> ActionPlan:
        current = self._current_or_error()
        context = self._copy_context_from_candidate(current)
        record = {
            "job_id": self._job_id("rewrite"),
            "ts": ts,
            "message_id": str(event.get("message_id") or ""),
            "run_id": current.get("run_id", ""),
            "copy_id": context.get("copy_id"),
            "original_copy": context.get("original_copy", ""),
            "instruction": command.instruction,
            "status": "pending",
        }

        def apply() -> None:
            self.store.append_rewrite_queue(record)

        return ActionPlan(
            "已加入文案重写队列，状态 pending，原文案和修改要求已记录。",
            "queue_rewrite",
            str(current.get("run_id") or ""),
            {"copy_id": context.get("copy_id")},
            apply,
        )

    def _plan_regenerate(self, command: FeedbackCommand, event: dict[str, Any], ts: str) -> ActionPlan:
        current = self._current_or_error()
        context = self._image_context_from_candidate(current)
        record = {
            "job_id": self._job_id("regen"),
            "ts": ts,
            "message_id": str(event.get("message_id") or ""),
            "run_id": current.get("run_id", ""),
            "image_id": context.get("image_id"),
            "role": context.get("role", ""),
            "original_image_path": context.get("image_path", ""),
            "instruction": command.instruction,
            "reason_tags": list(command.reason_tags),
            "status": "pending",
        }

        def apply() -> None:
            self.store.append_regen_queue(record)

        return ActionPlan(
            "已加入重生图队列，状态 pending，原图、角色和原因已记录。",
            "queue_regenerate",
            str(current.get("run_id") or ""),
            {"image_id": context.get("image_id"), "image_path": context.get("image_path", "")},
            apply,
        )

    def _plan_preference(self, command: FeedbackCommand, ts: str) -> ActionPlan:
        current = self._current_or_error()
        context = {
            **self._image_context_from_candidate(current),
            **self._copy_context_from_candidate(current),
        }
        profile = update_preference_profile(
            self.store.load_preference_profile(),
            command,
            context,
            ts,
        )

        def apply() -> None:
            self.store.write_preference_profile(profile)

        return ActionPlan(
            "已记录长期偏好，下次生成会有据可查。",
            "update_preference",
            str(current.get("run_id") or ""),
            {"preference_key": command.preference_key},
            apply,
        )

    def _candidate_for_queue(self, command: FeedbackCommand) -> dict[str, Any]:
        if command.image_number is not None or command.caption_number is not None:
            pool = self._pool_or_error()
            current = self.store.load_latest_candidate()
            image_number = command.image_number or self._selection_number(current, "image_number")
            caption_number = command.caption_number or self._selection_number(current, "caption_number")
            return self._candidate_from_pool(pool, image_number, caption_number)
        return self._current_or_error()

    def _current_or_error(self) -> dict[str, Any]:
        current = self.store.load_latest_candidate()
        if not current:
            raise FeedbackError(NO_CURRENT_CANDIDATE, "no_current_candidate")
        if not current.get("image") or not isinstance(current.get("publish"), dict):
            raise FeedbackError(NO_CURRENT_CANDIDATE, "invalid_current_candidate")
        return current

    def _pool_or_error(self) -> dict[str, Any]:
        pool = self.store.load_pool()
        if not pool:
            raise FeedbackError(NO_CURRENT_CANDIDATE, "no_candidate_pool")
        return pool

    def _candidate_from_pool(
        self,
        pool: dict[str, Any],
        image_number: int | None,
        caption_number: int | None,
    ) -> dict[str, Any]:
        images = pool.get("images") if isinstance(pool.get("images"), list) else []
        captions = pool.get("captions") if isinstance(pool.get("captions"), list) else []
        if not images or not captions:
            raise FeedbackError(NO_CURRENT_CANDIDATE, "empty_candidate_pool")
        image_number = int(image_number or pool.get("default_image_number") or images[-1]["number"])
        caption_number = int(caption_number or pool.get("default_caption_number") or captions[0]["number"])
        image = self._image_by_number(pool, image_number)
        caption = self._caption_by_number(pool, caption_number)
        return {
            "run_id": image.get("run_id"),
            "batch_id": pool.get("batch_id"),
            "image": image.get("path"),
            "caption_path": caption.get("caption_path"),
            "metadata_path": image.get("metadata_path"),
            "created_at": pool.get("created_at") or image.get("created_at") or caption.get("created_at"),
            "character": image.get("character") or caption.get("character"),
            "publish": caption.get("publish") or self._publish_from_content(str(caption.get("content") or "")),
            "selection": {
                "image_number": image_number,
                "caption_number": caption_number,
                "image_run_id": image.get("run_id"),
                "caption_run_id": caption.get("run_id"),
            },
        }

    def _with_image(
        self,
        current: dict[str, Any],
        pool: dict[str, Any],
        image: dict[str, Any],
        image_number: int,
    ) -> dict[str, Any]:
        candidate = copy.deepcopy(current)
        candidate["run_id"] = image.get("run_id")
        candidate["batch_id"] = pool.get("batch_id") or candidate.get("batch_id")
        candidate["image"] = image.get("path")
        candidate["metadata_path"] = image.get("metadata_path")
        candidate["created_at"] = image.get("created_at") or candidate.get("created_at")
        candidate["character"] = image.get("character") or candidate.get("character")
        selection = dict(candidate.get("selection") or {})
        selection["image_number"] = image_number
        selection["image_run_id"] = image.get("run_id")
        candidate["selection"] = selection
        return candidate

    def _with_caption(
        self,
        current: dict[str, Any],
        pool: dict[str, Any],
        caption: dict[str, Any],
        caption_number: int,
    ) -> dict[str, Any]:
        candidate = copy.deepcopy(current)
        candidate["batch_id"] = pool.get("batch_id") or candidate.get("batch_id")
        candidate["caption_path"] = caption.get("caption_path")
        candidate["publish"] = caption.get("publish") or self._publish_from_content(str(caption.get("content") or ""))
        selection = dict(candidate.get("selection") or {})
        selection["caption_number"] = caption_number
        selection["caption_run_id"] = caption.get("run_id")
        candidate["selection"] = selection
        return candidate

    def _image_by_number(self, pool: dict[str, Any], number: int) -> dict[str, Any]:
        images = pool.get("images") if isinstance(pool.get("images"), list) else []
        for image in images:
            if int(image.get("number") or 0) == int(number):
                return image
        raise FeedbackError(f"没有第 {number} 张图。", "image_number_not_found")

    def _caption_by_number(self, pool: dict[str, Any], number: int) -> dict[str, Any]:
        captions = pool.get("captions") if isinstance(pool.get("captions"), list) else []
        for caption in captions:
            if int(caption.get("number") or 0) == int(number):
                return caption
        raise FeedbackError(f"没有第 {number} 段文案。", "caption_number_not_found")

    def _image_context_from_pool_item(self, image: dict[str, Any], image_number: int) -> dict[str, Any]:
        return {
            "run_id": image.get("run_id", ""),
            "image_id": image_number,
            "image_path": image.get("path", ""),
            "role": self._role_label(image.get("character")),
        }

    def _image_context_from_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        selection = candidate.get("selection") if isinstance(candidate.get("selection"), dict) else {}
        return {
            "run_id": candidate.get("run_id", ""),
            "image_id": selection.get("image_number"),
            "image_path": candidate.get("image", ""),
            "role": self._role_label(candidate.get("character")),
        }

    def _copy_context_from_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        selection = candidate.get("selection") if isinstance(candidate.get("selection"), dict) else {}
        publish = candidate.get("publish") if isinstance(candidate.get("publish"), dict) else {}
        original = publish.get("text") or self._compose_copy(publish)
        return {
            "copy_id": selection.get("caption_number") or selection.get("caption_run_id") or candidate.get("caption_path"),
            "original_copy": original,
        }

    def _feedback_record(
        self,
        event: dict[str, Any],
        command: FeedbackCommand,
        ts: str,
        run_id: str,
        target: dict[str, object],
        action_taken: str,
        status: str,
        error: str | None,
    ) -> dict[str, Any]:
        return {
            "ts": ts,
            "message_id": str(event.get("message_id") or ""),
            "bot": self.store.bot,
            "run_id": run_id,
            "user_text": str(event.get("content", "") or ""),
            "intent": command.intent,
            "target": target,
            "reason_tags": list(command.reason_tags),
            "action_taken": action_taken,
            "status": status,
            "error": error,
        }

    def _selection_number(self, candidate: dict[str, Any] | None, key: str) -> int | None:
        if not candidate:
            return None
        selection = candidate.get("selection") if isinstance(candidate.get("selection"), dict) else {}
        value = selection.get(key)
        return int(value) if value is not None else None

    def _publish_from_content(self, content: str) -> dict[str, Any]:
        lines = [line.rstrip() for line in content.splitlines()]
        title = lines[0] if lines else ""
        tags: list[str] = []
        for line in lines:
            tags.extend(token.lstrip("#") for token in line.split() if token.startswith("#"))
        note_lines = [line for line in lines[1:] if line and not line.startswith("#")]
        return {
            "title": title,
            "note": "\n".join(note_lines).strip(),
            "tags": tags,
            "text": content,
        }

    def _compose_copy(self, publish: dict[str, Any]) -> str:
        title = str(publish.get("title") or "").strip()
        note = str(publish.get("note") or "").strip()
        tags = publish.get("tags") if isinstance(publish.get("tags"), list) else []
        tag_text = " ".join(f"#{tag}" for tag in tags)
        return "\n\n".join(part for part in (title, note, tag_text) if part)

    def _role_label(self, character: Any) -> str:
        if not isinstance(character, dict):
            return ""
        name = str(character.get("name") or "").strip()
        work = str(character.get("work") or "").strip()
        return " | ".join(part for part in (name, work) if part)

    def _job_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"
