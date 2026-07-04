from __future__ import annotations

import re

from .schemas import FeedbackCommand


CN_DIGITS = {
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
NUM_PATTERN = r"\d+|[一二两三四五六七八九十]+"
HELP_COMMANDS = {
    "反馈帮助",
    "反馈指令",
    "反馈说明",
    "反馈怎么用",
    "怎么反馈",
    "可以怎么反馈",
    "能反馈什么",
    "支持哪些反馈",
    "帮助反馈",
}


def parse_cn_index(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    if value in CN_DIGITS:
        return CN_DIGITS[value]
    if value.startswith("十") and len(value) == 2:
        tail = CN_DIGITS.get(value[1])
        return 10 + tail if tail else None
    if value.endswith("十") and len(value) == 2:
        head = CN_DIGITS.get(value[0])
        return head * 10 if head else None
    if "十" in value and len(value) == 3:
        head = CN_DIGITS.get(value[0])
        tail = CN_DIGITS.get(value[2])
        return head * 10 + tail if head and tail else None
    return None


def parse_feedback_command(text: str) -> FeedbackCommand | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    compact = re.sub(r"\s+", "", raw).strip("。.!！?？")

    command = _parse_help(compact, raw)
    if command:
        return command
    command = _parse_select(compact, raw)
    if command:
        return command
    command = _parse_reject(compact, raw)
    if command:
        return command
    command = _parse_queue_publish(compact, raw)
    if command:
        return command
    command = _parse_rewrite(compact, raw)
    if command:
        return command
    command = _parse_regenerate(compact, raw)
    if command:
        return command
    return _parse_preference(compact, raw)


def publish_command_from_intent(
    text: str,
    platforms: tuple[str, ...],
    image_number: int | None = None,
    caption_number: int | None = None,
) -> FeedbackCommand:
    return FeedbackCommand(
        intent="queue_publish",
        image_number=image_number,
        caption_number=caption_number,
        publish_targets=_normalize_publish_targets(platforms),
        instruction=text,
        reason_tags=("publish_intent",),
        source="publish_intent",
    )


def _parse_help(compact: str, raw: str) -> FeedbackCommand | None:
    if compact in HELP_COMMANDS:
        return FeedbackCommand(
            intent="help",
            instruction=raw,
            reason_tags=("feedback_help",),
        )
    return None


def _parse_select(compact: str, raw: str) -> FeedbackCommand | None:
    match = re.fullmatch(
        rf"用第(?P<image>{NUM_PATTERN})张图(?:[+＋和配搭加]第(?P<caption>{NUM_PATTERN})(?:段|篇)?文案)",
        compact,
    )
    if match:
        return FeedbackCommand(
            intent="select",
            image_number=parse_cn_index(match.group("image")),
            caption_number=parse_cn_index(match.group("caption")),
            instruction=raw,
            reason_tags=("select_image", "select_copy"),
        )
    match = re.fullmatch(rf"用第(?P<image>{NUM_PATTERN})张图", compact)
    if match:
        return FeedbackCommand(
            intent="select",
            image_number=parse_cn_index(match.group("image")),
            instruction=raw,
            reason_tags=("select_image",),
        )
    match = re.fullmatch(rf"用第(?P<caption>{NUM_PATTERN})(?:段|篇)?文案", compact)
    if match:
        return FeedbackCommand(
            intent="select",
            caption_number=parse_cn_index(match.group("caption")),
            instruction=raw,
            reason_tags=("select_copy",),
        )
    return None


def _parse_reject(compact: str, raw: str) -> FeedbackCommand | None:
    match = re.fullmatch(rf"第(?P<image>{NUM_PATTERN})张图(?:废掉|废了|不要了)", compact)
    if match:
        return FeedbackCommand(
            intent="reject",
            image_number=parse_cn_index(match.group("image")),
            instruction=raw,
            reason_tags=("reject_image",),
        )
    if compact in {"这张废掉", "这张图废掉", "当前这张废掉", "当前这张图废掉"}:
        return FeedbackCommand(
            intent="reject",
            instruction=raw,
            reason_tags=("reject_current_image",),
        )
    return None


def _parse_queue_publish(compact: str, raw: str) -> FeedbackCommand | None:
    if re.fullmatch(r"(加入|加到|放入)待发布(队列)?", compact):
        return FeedbackCommand(
            intent="queue_publish",
            instruction=raw,
            reason_tags=("queue_publish",),
        )
    targets = _publish_targets_from_text(compact)
    if targets:
        return FeedbackCommand(
            intent="queue_publish",
            publish_targets=targets,
            instruction=raw,
            reason_tags=("publish_intent",),
        )
    return None


def _parse_rewrite(compact: str, raw: str) -> FeedbackCommand | None:
    if "重写" not in compact:
        return None
    if not any(token in compact for token in ("文案", "正文", "标题", "口语")):
        return None
    tags = ["rewrite_copy"]
    if "太硬" in compact:
        tags.append("copy_too_stiff")
    if "口语" in compact:
        tags.append("more_spoken")
    if "标题保留" in compact:
        tags.append("keep_title")
    if "正文" in compact:
        tags.append("rewrite_body")
    return FeedbackCommand(
        intent="rewrite",
        instruction=raw,
        reason_tags=tuple(tags),
    )


def _parse_regenerate(compact: str, raw: str) -> FeedbackCommand | None:
    if not any(token in compact for token in ("重生成", "重新生成", "重生图", "换姿势")):
        return None
    tags = ["regenerate_image"]
    if "脸不像" in compact:
        tags.append("face_mismatch")
    if "风格保留" in compact:
        tags.append("keep_style")
    if "换姿势" in compact:
        tags.append("change_pose")
    return FeedbackCommand(
        intent="regenerate",
        instruction=raw,
        reason_tags=tuple(tags),
    )


def _parse_preference(compact: str, raw: str) -> FeedbackCommand | None:
    if "风格" in compact and "下次多用" in compact:
        return FeedbackCommand(
            intent="preference",
            instruction=raw,
            preference_key="image_likes",
            reason_tags=("style_like",),
        )
    if "标题" in compact and "夸张" in compact and any(token in compact for token in ("以后", "下次", "别")):
        return FeedbackCommand(
            intent="preference",
            instruction=raw,
            preference_key="copy_dislikes",
            reason_tags=("title_too_exaggerated",),
        )
    if "角色" in compact and "适合" in compact and "氛围" in compact:
        return FeedbackCommand(
            intent="preference",
            instruction=raw,
            preference_key="role_style_notes",
            reason_tags=("role_style_fit",),
        )
    return None


def _publish_targets_from_text(compact: str) -> tuple[str, ...]:
    if re.fullmatch(r"(请|帮我|现在|直接)?(发布|发)(到)?公众号", compact):
        return ("wechat",)
    if re.fullmatch(r"(请|帮我|现在|直接)?(发布|发)(到)?小红书", compact):
        return ("xhs",)
    return ()


def _normalize_publish_targets(platforms: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for platform in platforms:
        value = str(platform or "").strip().lower()
        if value in {"xiaohongshu", "xhs", "redbook"}:
            value = "xhs"
        elif value in {"wechat", "weixin", "gzh", "公众号"}:
            value = "wechat"
        if value and value not in normalized:
            normalized.append(value)
    return tuple(normalized)
