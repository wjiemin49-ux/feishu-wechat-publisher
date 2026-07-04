from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
DEFAULT_PROMPT = ROOT / "caption_lab_prompt.txt"
DEFAULT_OUTPUT_DIR = ROOT / "state" / "caption_lab_runs"
PROMPT_START = "--- PROMPT START ---"
PROMPT_END = "--- PROMPT END ---"


class CaptionLabError(RuntimeError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_api_key(path: Path) -> str:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            value = line.strip()
            if value:
                return value
    raise CaptionLabError(f"empty API key file: {path}")


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


def extract_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
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
    return "\n\n".join(
        part for part in (normalized["title"], normalized["copy"], topics) if part
    ).rstrip()


def latest_character(root: Path) -> tuple[str, str]:
    path = root / "state" / "latest_publish_candidate.json"
    data = load_json(path)
    character = data.get("character") if isinstance(data.get("character"), dict) else {}
    name = str(character.get("name") or "").strip()
    work = str(character.get("work") or "").strip()
    if name and work:
        return name, work.replace("《", "").replace("》", "")
    publish = data.get("publish") if isinstance(data.get("publish"), dict) else {}
    title = str(publish.get("title") or data.get("title") or "").strip()
    if "|" in title:
        left, right = title.split("|", 1)
        return left.strip(), right.strip()
    raise CaptionLabError(f"cannot find character/work in {path}")


def render_prompt(template: str, character_name: str, work_name: str) -> str:
    return (
        template.replace("{{character_name}}", character_name)
        .replace("{{work_name}}", work_name)
        .strip()
    )


def load_prompt_template(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == PROMPT_START)
        end = next(i for i, line in enumerate(lines[start + 1 :], start + 1) if line.strip() == PROMPT_END)
    except StopIteration:
        return text.strip()
    return "\n".join(lines[start + 1 : end]).strip()


def choose_model(config: dict[str, Any], override: str | None) -> str:
    model = (
        override
        or config.get("caption_lab_model")
        or config.get("caption_model")
        or config.get("text_model")
        or config.get("general_answer_model")
    )
    if not model:
        raise CaptionLabError("missing model: set --model or text_model in config.json")
    return str(model)


def call_model(
    config: dict[str, Any],
    model: str,
    prompt: str,
    max_output_tokens: int,
    temperature: float,
    timeout: float,
) -> tuple[dict[str, Any], str]:
    from openai import OpenAI

    client = OpenAI(api_key=read_api_key(Path(config["api_key_path"])))
    response = client.responses.create(
        model=model,
        input=[{"role": "user", "content": prompt}],
        max_output_tokens=max_output_tokens,
        temperature=temperature,
        timeout=timeout,
    )
    raw = extract_response_text(response)
    parsed = extract_json_object(raw)
    if not parsed:
        raise CaptionLabError("caption lab model returned non-json text")
    caption = normalize_caption(parsed)
    missing = [key for key in ("title", "copy") if not caption.get(key)]
    if missing:
        raise CaptionLabError(f"caption lab model returned incomplete JSON: {missing}")
    return caption, raw


def save_result(output_dir: Path, payload: dict[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = output_dir / f"{stamp}_caption_lab.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Standalone caption lab. It does not modify workflow state."
    )
    parser.add_argument("--character", help="Character name. Defaults to latest candidate.")
    parser.add_argument("--work", help="Work/source title. Defaults to latest candidate.")
    parser.add_argument("--model", help="Override caption lab model.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Config JSON path.")
    parser.add_argument("--prompt-file", default=str(DEFAULT_PROMPT), help="Editable prompt file.")
    parser.add_argument("--prompt-only", action="store_true", help="Print rendered prompt only.")
    parser.add_argument("--save", action="store_true", help="Save output under state/caption_lab_runs.")
    parser.add_argument("--raw", action="store_true", help="Include raw model text in output.")
    parser.add_argument("--max-output-tokens", type=int, default=None)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--timeout", type=float, default=None)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config_path = Path(args.config)
    prompt_path = Path(args.prompt_file)
    config = load_json(config_path)

    if args.character and args.work:
        character_name, work_name = args.character.strip(), args.work.strip()
    elif not args.character and not args.work:
        character_name, work_name = latest_character(ROOT)
    else:
        raise CaptionLabError("set both --character and --work, or set neither to use latest")

    prompt = render_prompt(load_prompt_template(prompt_path), character_name, work_name)
    model = choose_model(config, args.model)
    payload: dict[str, Any] = {
        "ok": True,
        "mode": "prompt_only" if args.prompt_only else "generate",
        "model": model,
        "character": {"name": character_name, "work": work_name},
        "prompt_file": str(prompt_path),
    }

    if args.prompt_only:
        payload["prompt"] = prompt
    else:
        caption, raw = call_model(
            config=config,
            model=model,
            prompt=prompt,
            max_output_tokens=int(
                args.max_output_tokens
                or config.get("caption_lab_max_output_tokens")
                or config.get("caption_max_output_tokens", 500)
            ),
            temperature=float(
                args.temperature
                if args.temperature is not None
                else config.get("caption_lab_temperature", config.get("caption_temperature", 0.7))
            ),
            timeout=float(
                args.timeout
                if args.timeout is not None
                else config.get("caption_lab_timeout_seconds", config.get("caption_timeout_seconds", 60))
            ),
        )
        payload["caption"] = caption
        payload["caption_text"] = caption_text(caption)
        if args.raw:
            payload["raw_response"] = raw

    if args.save:
        payload["saved_path"] = str(save_result(DEFAULT_OUTPUT_DIR, payload))

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(1)
