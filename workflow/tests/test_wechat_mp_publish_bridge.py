import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wechat_mp_publish_bridge as bridge


class WechatMpPublishBridgeTests(unittest.TestCase):
    def test_prepare_and_confirm_state_flow(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_dir = root / "state"
            state_dir.mkdir()
            image = root / "image.png"
            image.write_bytes(b"png")
            ready = root / "ready.png"
            ready.write_bytes(b"png")
            after = root / "after.png"
            after.write_bytes(b"png")
            pending = root / "pending.json"
            pending.write_text('{"run_id":"wechat-prepare"}', encoding="utf-8")
            config = {
                "state_dir": str(state_dir),
                "wechat_mp_publisher_dir": str(root),
                "wechat_mp_python": sys.executable,
                "wechat_mp_python_args": [],
            }
            candidate = {
                "run_id": "run-1",
                "image": str(image),
                "publish": {"title": "标题", "note": "正文", "tags": ["tag"]},
            }
            (state_dir / "latest_publish_candidate.json").write_text(
                bridge.json.dumps(candidate, ensure_ascii=False),
                encoding="utf-8",
            )
            prepare_result = {
                "returncode": 0,
                "stdout_tail": "{}",
                "stderr_tail": "",
                "parsed": {
                    "run_id": "wechat-prepare",
                    "stopped_before_final_publish": True,
                    "publish_button_visible": True,
                    "risk_warning_found": False,
                    "screenshot_path": str(ready),
                    "state_path": str(pending),
                },
            }
            publish_result = {
                "returncode": 0,
                "stdout_tail": "{}",
                "stderr_tail": "",
                "parsed": {
                    "run_id": "wechat-confirm",
                    "published_clicks_completed": True,
                    "after_publish_risk_words": [],
                    "after_publish_screenshot_path": str(after),
                    "source_state_run_id": "wechat-prepare",
                },
            }
            status_result = {
                "returncode": 0,
                "stdout_tail": "{}",
                "stderr_tail": "",
                "parsed": {
                    "run_id": "wechat-status",
                    "visible_risk_words": [],
                    "text_sample": ["标题", "已发表"],
                    "screenshot_path": str(after),
                },
            }
            with mock.patch.object(
                bridge,
                "run_command",
                side_effect=[status_result, prepare_result, publish_result, status_result],
            ):
                prepared = bridge.start_prepare(config, candidate)
                self.assertEqual(prepared["status"], "awaiting_confirm")
                confirmed = bridge.confirm_publish(config, run_id="run-1")
            self.assertEqual(confirmed["status"], "published")
            self.assertTrue((state_dir / bridge.STATE_FILE).exists())
            self.assertTrue((state_dir / bridge.ATTEMPTS_FILE).exists())


if __name__ == "__main__":
    unittest.main()
