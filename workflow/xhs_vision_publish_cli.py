from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import xhs_vision_publish_bridge as bridge


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safe XHS vision publish bridge CLI")
    parser.add_argument("--config", default=str(bridge.DEFAULT_CONFIG_PATH))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("dry-run", help="Run probe dry-run and enter awaiting_confirm on success.")
    confirm = sub.add_parser("confirm", help="Confirm current awaiting XHS vision publish task.")
    confirm.add_argument("--run-id", default="")
    return parser


def _summary(state: dict[str, Any]) -> dict[str, Any]:
    dry = state.get("dry_run_result") if isinstance(state.get("dry_run_result"), dict) else {}
    pub = state.get("publish_result") if isinstance(state.get("publish_result"), dict) else {}
    return {
        "run_id": state.get("xhs_workflow_run_id") or state.get("run_id"),
        "status": state.get("status"),
        "title": state.get("title"),
        "candidate_image": state.get("candidate_image"),
        "dry_run_completed": dry.get("dry_run_completed"),
        "publish_attempted": pub.get("publish_attempted"),
        "submitted_or_reviewing": pub.get("submitted_or_reviewing"),
        "risk_warning_found": bool(dry.get("risk_warning_found") or pub.get("risk_warning_found")),
        "screenshot_path": state.get("screenshot_path"),
        "dry_run_result": dry,
        "publish_result": pub,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = bridge.load_config(Path(args.config))
    try:
        if args.command == "dry-run":
            state = bridge.start_dry_run(config)
        elif args.command == "confirm":
            state = bridge.confirm_publish(config, run_id=args.run_id or None)
        else:
            raise RuntimeError(f"unknown command: {args.command}")
        print(json.dumps(_summary(state), ensure_ascii=False, indent=2))
        return 0 if state.get("status") in {"awaiting_confirm", "submitted", "publish_attempted"} else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
