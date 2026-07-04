import json
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import workflow


class RecordingFeishu:
    def __init__(self):
        self.texts = []

    def send_text(self, text):
        self.texts.append(text)
        return {"message_id": "ack"}


class RecordingPublisher:
    def __init__(self):
        self.calls = []

    def publish(self, platform, candidate):
        self.calls.append((platform, candidate))
        return {"platform": platform, "ok": True}


class FeedbackWorkflowTests(unittest.TestCase):
    def make_config(self, root: Path) -> dict:
        return {
            "state_dir": str(root / "state"),
            "output_root": str(root / "outputs"),
            "timezone": "Asia/Shanghai",
            "feishu_chat_id": "oc_test",
            "feishu_profile": "xhs-content-bot",
            "max_count_per_request": 3,
            "general_answer_enabled": True,
            "image_inbox_chat_link": "https://example.test/image-inbox",
            "wechat_mp_publish_enabled": False,
        }

    def seed_candidates(self, config: dict) -> None:
        state_dir = Path(config["state_dir"])
        output_root = Path(config["output_root"])
        state_dir.mkdir(parents=True, exist_ok=True)
        images = []
        captions = []
        for number in range(1, 4):
            run_id = f"run-{number}"
            out_dir = output_root / run_id
            out_dir.mkdir(parents=True, exist_ok=True)
            image_path = out_dir / "image.png"
            image_path.write_bytes(b"png")
            caption_path = out_dir / "caption.md"
            text = f"标题{number}\n\n正文{number}\n\n#标签{number}\n"
            caption_path.write_text(text, encoding="utf-8")
            character = {
                "ordinal": number,
                "name": f"角色{number}",
                "work": "作品",
                "line_index": number,
                "line": "",
                "used": False,
            }
            images.append(
                {
                    "number": number,
                    "run_id": run_id,
                    "path": str(image_path),
                    "metadata_path": str(out_dir / "metadata.json"),
                    "created_at": "2026-06-18T23:31:45+08:00",
                    "character": character,
                }
            )
            captions.append(
                {
                    "number": number,
                    "run_id": run_id,
                    "caption_path": str(caption_path),
                    "content": text,
                    "publish": {
                        "title": f"标题{number}",
                        "note": f"正文{number}",
                        "tags": [f"标签{number}"],
                        "text": text,
                    },
                    "created_at": "2026-06-18T23:31:45+08:00",
                    "character": character,
                }
            )
        pool = {
            "batch_id": "batch",
            "created_at": "2026-06-18T23:31:45+08:00",
            "images": images,
            "captions": captions,
            "default_image_number": 3,
            "default_caption_number": 1,
        }
        latest = {
            "run_id": "run-3",
            "batch_id": "batch",
            "image": images[2]["path"],
            "caption_path": captions[0]["caption_path"],
            "metadata_path": images[2]["metadata_path"],
            "created_at": "2026-06-18T23:31:45+08:00",
            "character": images[2]["character"],
            "publish": captions[0]["publish"],
            "selection": {
                "image_number": 3,
                "caption_number": 1,
                "image_run_id": "run-3",
                "caption_run_id": "run-1",
            },
        }
        (state_dir / "latest_candidate_pool.json").write_text(
            json.dumps(pool, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (state_dir / "latest_publish_candidate.json").write_text(
            json.dumps(latest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def send_feedback(self, wf, feishu, text, message_id):
        return wf.handle_event(
            {
                "chat_id": "oc_test",
                "content": text,
                "msg_type": "text",
                "message_id": message_id,
            },
            feishu_client=feishu,
            general_client=workflow.MockGeneralAnswerClient(),
            no_commit=True,
        )

    def read_json(self, config: dict, name: str):
        return json.loads((Path(config["state_dir"]) / name).read_text(encoding="utf-8"))

    def read_jsonl(self, config: dict, name: str):
        path = Path(config["state_dir"]) / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def test_candidate_pool_summary_explains_feedback_timing_and_examples(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            self.seed_candidates(config)

            pool = self.read_json(config, "latest_candidate_pool.json")
            text = workflow.candidate_pool_summary_text(pool)

            self.assertIn("可以直接反馈", text)
            self.assertIn("用第3张图 + 第1段文案", text)
            self.assertIn("加入待发布", text)
            self.assertIn("反馈帮助", text)

    def test_feedback_help_returns_examples_without_current_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            wf = workflow.Workflow(config)
            feishu = RecordingFeishu()

            result = self.send_feedback(wf, feishu, "怎么反馈？", "m-help")

            self.assertEqual(result["kind"], "feedback")
            self.assertEqual(result["intent"], "help")
            self.assertIn("现在可以反馈生成结果了", feishu.texts[-1])
            self.assertIn("用第2张图", feishu.texts[-1])
            self.assertIn("不会真实发布", feishu.texts[-1])
            feedback = self.read_jsonl(config, "feedback.jsonl")
            self.assertEqual(feedback[-1]["intent"], "help")
            self.assertEqual(feedback[-1]["action_taken"], "show_feedback_help")
            self.assertFalse((Path(config["state_dir"]) / "latest_publish_candidate.json").exists())

    def test_feedback_acceptance_flow_updates_state_and_queues(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            self.seed_candidates(config)
            wf = workflow.Workflow(config)
            feishu = RecordingFeishu()

            self.send_feedback(wf, feishu, "用第2张图", "m1")
            latest = self.read_json(config, "latest_publish_candidate.json")
            self.assertEqual(latest["selection"]["image_number"], 2)
            self.assertEqual(latest["selection"]["caption_number"], 1)
            self.assertIn("run-2", latest["image"])
            self.assertEqual(latest["publish"]["title"], "标题1")
            self.assertIn("文案保持不变", feishu.texts[-1])

            self.send_feedback(wf, feishu, "用第3张图 + 第1段文案", "m2")
            latest = self.read_json(config, "latest_publish_candidate.json")
            self.assertEqual(latest["selection"]["image_number"], 3)
            self.assertEqual(latest["selection"]["caption_number"], 1)
            self.assertIn("加入待发布", feishu.texts[-1])

            self.send_feedback(wf, feishu, "第2张图废掉", "m3")
            rejected = self.read_jsonl(config, "rejected_candidates.jsonl")
            self.assertEqual(rejected[-1]["image_id"], 2)
            self.assertEqual(rejected[-1]["status"], "rejected")
            self.assertIn("废弃候选", feishu.texts[-1])

            self.send_feedback(wf, feishu, "加入待发布", "m4")
            publish_queue = self.read_jsonl(config, "publish_queue.jsonl")
            self.assertEqual(publish_queue[-1]["publish_target"], "unspecified")
            self.assertEqual(publish_queue[-1]["status"], "pending")
            self.assertIn("状态 pending", feishu.texts[-1])

            before_duplicate = len(publish_queue)
            self.send_feedback(wf, feishu, "发布公众号", "m5")
            self.assertIn("不会真实发布", feishu.texts[-1])
            self.send_feedback(wf, feishu, "发布公众号", "m5")
            publish_queue = self.read_jsonl(config, "publish_queue.jsonl")
            self.assertEqual(len(publish_queue), before_duplicate + 1)
            self.assertEqual(publish_queue[-1]["publish_target"], "wechat")
            self.assertEqual(publish_queue[-1]["status"], "pending_confirm")
            self.assertEqual(feishu.texts[-1], "这条反馈已经处理过了，不会重复写入。")

            self.send_feedback(wf, feishu, "文案太硬，重写口语一点", "m6")
            rewrite_queue = self.read_jsonl(config, "rewrite_queue.jsonl")
            self.assertEqual(rewrite_queue[-1]["status"], "pending")
            self.assertEqual(rewrite_queue[-1]["copy_id"], 1)
            self.assertIn("标题1", rewrite_queue[-1]["original_copy"])
            self.assertIn("原文案和修改要求已记录", feishu.texts[-1])

            self.send_feedback(wf, feishu, "这张脸不像，重生成", "m7")
            regen_queue = self.read_jsonl(config, "regen_queue.jsonl")
            self.assertEqual(regen_queue[-1]["status"], "pending")
            self.assertIn("face_mismatch", regen_queue[-1]["reason_tags"])
            self.assertIn("原图、角色和原因已记录", feishu.texts[-1])

            self.send_feedback(wf, feishu, "这个风格好，下次多用", "m8")
            preferences = self.read_json(config, "preference_profile.json")
            self.assertEqual(len(preferences["image_likes"]), 1)
            self.assertTrue(preferences["updated_at"])
            self.assertIn("长期偏好", feishu.texts[-1])

            feedback = self.read_jsonl(config, "feedback.jsonl")
            required = {
                "ts",
                "message_id",
                "bot",
                "run_id",
                "user_text",
                "intent",
                "target",
                "reason_tags",
                "action_taken",
                "status",
                "error",
            }
            for entry in feedback:
                self.assertTrue(required.issubset(entry), entry)
            processed = self.read_json(config, "processed_message_ids.json")
            self.assertIn("m5", processed["message_ids"])

    def test_publish_intent_safety_switch_queues_without_real_publish(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            config["feedback_queue_all_publish"] = True
            self.seed_candidates(config)
            wf = workflow.Workflow(config)
            feishu = RecordingFeishu()
            publisher = RecordingPublisher()

            result = wf.handle_event(
                {
                    "chat_id": "oc_test",
                    "content": "图 2 配文案 1 发公众号",
                    "msg_type": "text",
                    "message_id": "m-publish-intent",
                },
                feishu_client=feishu,
                publish_client=publisher,
                no_commit=True,
            )

            self.assertEqual(result["kind"], "feedback")
            self.assertEqual(publisher.calls, [])
            queue = self.read_jsonl(config, "publish_queue.jsonl")
            self.assertEqual(queue[-1]["publish_target"], "wechat")
            self.assertEqual(queue[-1]["status"], "pending_confirm")
            self.assertEqual(queue[-1]["candidate"]["selection"]["image_number"], 2)
            self.assertEqual(queue[-1]["candidate"]["selection"]["caption_number"], 1)

    def test_plain_question_still_uses_general_answer(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            self.seed_candidates(config)
            wf = workflow.Workflow(config)
            feishu = RecordingFeishu()

            result = wf.handle_event(
                {
                    "chat_id": "oc_test",
                    "content": "hello",
                    "msg_type": "text",
                    "message_id": "m-chat",
                },
                feishu_client=feishu,
                general_client=workflow.MockGeneralAnswerClient(),
                no_commit=True,
            )

            self.assertEqual(result["kind"], "chat")
            self.assertIn("普通回答：hello", feishu.texts[-1])
            self.assertFalse((Path(config["state_dir"]) / "feedback.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
