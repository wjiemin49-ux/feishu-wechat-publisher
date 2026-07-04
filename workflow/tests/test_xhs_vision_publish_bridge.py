import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import xhs_vision_publish_bridge as bridge


class XhsVisionPublishBridgeTests(unittest.TestCase):
    def test_publish_clicked_without_completed_is_not_submitted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            image = root / "image.png"
            image.write_bytes(b"png")
            config = {
                "state_dir": str(root / "state"),
                "xhs_vision_probe_dir": str(root / "probe"),
                "xhs_vision_probe_python": str(root / "probe" / ".venv" / "Scripts" / "python.exe"),
            }
            parsed = {
                "publish_clicked": True,
                "publish_completed": False,
                "risk_warning_found": False,
                "after_screenshot_path": str(root / "after.png"),
            }
            with mock.patch.object(
                bridge,
                "_run_probe",
                return_value={
                    "parsed": parsed,
                    "probe_run_id": "probe-1",
                    "stdout_tail": "{}",
                    "stderr_tail": "",
                    "returncode": 0,
                },
            ):
                result = bridge.run_probe_publish_once(
                    {"image": str(image), "title": "标题", "body": "正文"},
                    {"dry_run_result": {"dry_run_completed": True, "risk_warning_found": False}},
                    config,
                )
            self.assertTrue(result["publish_attempted"])
            self.assertFalse(result["submitted_or_reviewing"])
            self.assertFalse(result["publish_completed"])

    def test_confirm_rejects_stale_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_dir = root / "state"
            state_dir.mkdir()
            old_image = root / "old.png"
            new_image = root / "new.png"
            old_image.write_bytes(b"old")
            new_image.write_bytes(b"new")
            config = {
                "state_dir": str(state_dir),
                "xhs_vision_probe_dir": str(root / "probe"),
                "xhs_vision_probe_python": str(root / "probe" / ".venv" / "Scripts" / "python.exe"),
            }
            dry_screenshot = root / "dry.png"
            dry_screenshot.write_bytes(b"png")
            bridge.write_publish_state(
                {
                    "xhs_workflow_run_id": "old-run",
                    "candidate": {
                        "run_id": "old-run",
                        "image": str(old_image),
                        "title": "旧标题",
                        "body": "旧正文",
                    },
                    "candidate_image": str(old_image),
                    "title": "旧标题",
                    "status": "awaiting_confirm",
                    "dry_run_result": {
                        "dry_run_completed": True,
                        "risk_warning_found": False,
                        "screenshot_path": str(dry_screenshot),
                    },
                    "publish_result": None,
                    "screenshot_path": str(dry_screenshot),
                },
                config,
            )
            (state_dir / "latest_publish_candidate.json").write_text(
                """
{
  "run_id": "new-run",
  "image": "%s",
  "publish": {"title": "新标题", "note": "新正文", "tags": []}
}
"""
                % str(new_image).replace("\\", "\\\\"),
                encoding="utf-8",
            )
            with mock.patch.object(bridge, "run_probe_publish_once") as publish_once:
                with self.assertRaisesRegex(RuntimeError, "当前候选已变化"):
                    bridge.confirm_publish(config, run_id="old-run")
            publish_once.assert_not_called()


if __name__ == "__main__":
    unittest.main()
