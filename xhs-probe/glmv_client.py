from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path
from typing import Any


class GlmVisionError(RuntimeError):
    pass


def load_dotenv_if_present(project_root: Path) -> bool:
    env_path = project_root / ".env.local"
    if not env_path.exists():
        return False
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    load_dotenv(env_path)
    return True


def _first_non_empty_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def load_api_key(provider_config: dict[str, Any], project_root: Path) -> tuple[str, bool]:
    load_dotenv_if_present(project_root)

    env_name = str(provider_config.get("api_key_env") or "").strip()
    if env_name:
        value = os.getenv(env_name, "").strip()
        if value:
            return value, True

    file_name = str(provider_config.get("api_key_file") or "").strip()
    if not file_name:
        return "", False
    key_file = Path(file_name).expanduser()
    if not key_file.exists() or not key_file.is_file():
        return "", False
    value = _first_non_empty_line(key_file.read_text(encoding="utf-8-sig"))
    return value, bool(value)


def load_prompt(prompt_path: Path) -> str:
    return prompt_path.read_text(encoding="utf-8")


def _image_data_url(image_path: Path) -> str:
    mime_type = mimetypes.guess_type(str(image_path))[0] or "image/png"
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return str(content)


def _extract_json_text(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start < 0:
        raise GlmVisionError("GLM response did not contain a JSON object.")

    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise GlmVisionError("GLM response JSON object was incomplete.")


def parse_json_response(text: str) -> dict[str, Any]:
    return json.loads(_extract_json_text(text))


def _redact(text: str, api_key: str) -> str:
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    for marker in ("Bearer ", "Authorization:"):
        if marker in text:
            head, _, _tail = text.partition(marker)
            return f"{head}{marker}[REDACTED]"
    return text


class GlmVisionClient:
    def __init__(self, config: dict[str, Any], api_key: str, provider: str = "glm") -> None:
        self.endpoint = str(config.get("api_endpoint") or "").strip()
        self.model = str(config.get("model") or "").strip()
        self.timeout_seconds = int(config.get("timeout_seconds") or 60)
        self.temperature = float(config.get("temperature") or 0.1)
        self.max_tokens = int(config.get("max_tokens") or 1200)
        self.api_key = api_key
        self.provider = provider

    def analyze_image(self, image_path: Path, prompt: str) -> dict[str, Any]:
        if not self.api_key:
            raise GlmVisionError("GLM credential is not loaded.")
        if not self.endpoint:
            raise GlmVisionError("GLM api_endpoint is empty.")
        if not self.model:
            raise GlmVisionError("GLM model is empty.")

        try:
            import requests
        except ImportError as exc:
            raise GlmVisionError("The requests package is not installed.") from exc

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": _image_data_url(image_path)}},
                    ],
                }
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(
                self.endpoint,
                headers=headers,
                json=payload,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise GlmVisionError(f"GLM request failed: {_redact(str(exc), self.api_key)}") from exc

        if response.status_code >= 400:
            safe_body = _redact(response.text[:800], self.api_key)
            raise GlmVisionError(f"GLM HTTP {response.status_code}: {safe_body}")

        try:
            data = response.json()
        except ValueError as exc:
            raise GlmVisionError("GLM response was not JSON.") from exc

        try:
            message = data["choices"][0]["message"]
            raw_text = _message_content_to_text(message.get("content"))
        except (KeyError, IndexError, TypeError) as exc:
            raise GlmVisionError("GLM response did not match chat completions format.") from exc

        parsed = parse_json_response(raw_text)
        return {
            "provider": self.provider,
            "model": self.model,
            "parsed": parsed,
            "raw_text": raw_text,
            "usage": data.get("usage"),
        }
