import json
import subprocess
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import workflow


class FailingFeishu:
    def send_post(self, image_path, text):
        raise workflow.WorkflowError("send failed")


class RecordingFeishu:
    def __init__(self):
        self.texts = []
        self.posts = []

    def send_text(self, text):
        self.texts.append(text)
        return {"message_id": "ack"}

    def send_post(self, image_path, text):
        self.posts.append((image_path, text))
        return {
            "image_key": "image",
            "image_message_id": "image-message",
            "text_message_id": "text-message",
            "caption_message_type": "text",
        }

    def download_message_image(self, message_id, image_key, output_path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(workflow.MockOpenAIWorkflowClient.PNG_BYTES)
        return output_path


class RecordingPublisher:
    def __init__(self):
        self.calls = []

    def publish(self, platform, candidate):
        self.calls.append((platform, candidate))
        return {"platform": platform, "ok": True}


class FakeIntentClassifier:
    def __init__(self, intent):
        self.intent = intent
        self.calls = []

    def classify(self, text, max_count):
        self.calls.append((text, max_count))
        if isinstance(self.intent, Exception):
            raise self.intent
        return self.intent


class FakeFeishuForMap:
    def __init__(self, config):
        self.config = config

    def chat_info(self):
        return {"name": "小红书发文专用", "bot_count": "3"}

    def chat_bots(self):
        return [{"bot_name": "Hermes Agent"}]

    def list_recent_messages(self, page_size=20):
        return [{"content": "生成今天文章", "message_position": "1"}]

    def event_inventory_summary(self):
        return {"event_count": 14, "groups": {"im": 11}}


class CapturingFeishuClient(workflow.FeishuClient):
    def __init__(self, config):
        super().__init__(config)
        self.calls = []
        self.call_cwds = []

    def _run(self, args, timeout=None, cwd=None):
        self.calls.append(args)
        self.call_cwds.append(cwd)
        return {"data": {"messages": []}}


class WorkflowTests(unittest.TestCase):
    def make_config(self, root: Path) -> dict:
        image_dir = root / "images"
        image_dir.mkdir()
        (image_dir / "001.jpg").write_bytes(b"fake")
        (image_dir / "002.jpg").write_bytes(b"fake")
        (image_dir / "2026-06-08-01__used.jpg").write_bytes(b"fake")
        pool = root / "pool.md"
        pool.write_text(
            "# pool\n\n"
            "1. 漩涡鸣人 —— 《火影忍者》\n"
            "2. 宇智波佐助 —— 《火影忍者》 [USED:2026-06-08-01]\n"
            "3. 春野樱 —— 《火影忍者》\n",
            encoding="utf-8",
        )
        return {
            "reference_image_dir": str(image_dir),
            "character_pool_path": str(pool),
            "api_key_path": str(root / "key.txt"),
            "output_root": str(root / "outputs"),
            "state_dir": str(root / "state"),
            "_workspace_root": str(root),
            "timezone": "Asia/Shanghai",
            "feishu_chat_id": "oc_test",
            "feishu_send_as": "user",
            "image_model": "gpt-image-2",
            "image_size": "1024x1536",
            "image_quality": "medium",
            "image_output_format": "png",
            "text_model": "gpt-5.4-mini",
            "max_count_per_request": 3,
            "lock_timeout_seconds": 2,
            "openai_timeout_seconds": 240,
            "lark_timeout_seconds": 60,
            "reference_extensions": [".jpg", ".jpeg", ".png", ".webp"],
            "image_prompt_template": "生成 {character}",
        }

    def test_upload_local_image_uses_image_directory_cwd(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(root)
            screenshot_dir = root / "external_screenshots"
            screenshot_dir.mkdir()
            screenshot = screenshot_dir / "shot.png"
            screenshot.write_bytes(b"fake-png")
            calls = []

            class LocalImageFeishu(workflow.FeishuClient):
                def _run(self, args, timeout=None, cwd=None):
                    calls.append({"args": args, "cwd": cwd})
                    return {"data": {"image_key": "img_test"}}

            client = LocalImageFeishu(config)
            self.assertEqual(client.upload_local_image(screenshot), "img_test")
            self.assertEqual(calls[0]["cwd"], screenshot_dir)
            file_value = calls[0]["args"][calls[0]["args"].index("--file") + 1]
            self.assertEqual(file_value, "image=shot.png")

    def test_parse_trigger_command(self):
        self.assertEqual(workflow.parse_trigger_command("生成今天文章"), 1)
        self.assertEqual(workflow.parse_trigger_command("生成2篇"), 2)
        self.assertEqual(workflow.parse_trigger_command("生成3张图"), 3)
        self.assertEqual(workflow.parse_trigger_command("生成四张图", 4), 4)
        self.assertEqual(workflow.parse_trigger_command("今天发3篇"), 3)
        self.assertEqual(workflow.parse_trigger_command("公众号来两篇", 3), 2)
        self.assertEqual(workflow.parse_trigger_command("小红书生图发文", 3), 1)
        self.assertEqual(workflow.parse_trigger_command("今天发9篇", 3), 3)
        self.assertIsNone(workflow.parse_trigger_command("随便聊聊"))

    def test_detect_bot_intent_routes_general_chat(self):
        self.assertEqual(workflow.detect_bot_intent("hello").kind, "chat")
        self.assertEqual(workflow.detect_bot_intent("公众号怎么发比较好").kind, "chat")
        self.assertEqual(workflow.detect_bot_intent("帮我生成一篇公众号图文").kind, "workflow")
        self.assertEqual(workflow.detect_bot_intent("重新生成").kind, "workflow")
        self.assertEqual(workflow.detect_bot_intent("我想把图片保存到电脑").kind, "image_inbox")

    def test_detect_publish_intent_routes_to_publish(self):
        intent = workflow.detect_bot_intent("发布到小红书")
        self.assertEqual(intent.kind, "publish")
        self.assertEqual(intent.platforms, ("xiaohongshu",))
        self.assertEqual(workflow.detect_bot_intent("发布到公众号").platforms, ("wechat",))
        self.assertEqual(
            workflow.detect_bot_intent("两个平台都发").platforms,
            ("xiaohongshu", "wechat"),
        )
        self.assertEqual(
            workflow.detect_bot_intent("帮我发在两个平台上").platforms,
            ("xiaohongshu", "wechat"),
        )
        self.assertEqual(workflow.detect_bot_intent("图 2 配文案 1 发公众号").kind, "publish")
        self.assertEqual(workflow.detect_bot_intent("发公众号").kind, "publish")
        self.assertEqual(workflow.detect_bot_intent("发小红书").kind, "publish")
        self.assertEqual(workflow.detect_bot_intent("帮我发公众号").platforms, ("wechat",))
        self.assertEqual(workflow.detect_bot_intent("帮我发小红书").platforms, ("xiaohongshu",))
        self.assertEqual(workflow.detect_bot_intent("我想发自己的图片").kind, "manual_publish")
        self.assertEqual(
            workflow.detect_bot_intent("我想把自己的图片发公众号").platforms,
            ("wechat",),
        )
        self.assertEqual(workflow.detect_bot_intent("帮我生成一篇公众号图文").kind, "workflow")
        self.assertEqual(workflow.detect_bot_intent("小红书生图发文").kind, "workflow")
        self.assertEqual(workflow.detect_bot_intent("帮我发布").platforms, ())
        self.assertIsNone(workflow.parse_trigger_command("发布到小红书"))

    def test_ai_intent_classifier_can_override_ambiguous_platform_task(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            wf = workflow.Workflow(config)
            classifier = FakeIntentClassifier(
                workflow.BotIntent(
                    "publish",
                    reason="ai",
                    platforms=("wechat",),
                )
            )
            intent = wf.detect_event_intent("公众号发一下", intent_client=classifier)
            self.assertEqual(intent.kind, "publish")
            self.assertEqual(intent.platforms, ("wechat",))
            self.assertEqual(len(classifier.calls), 1)

    def test_ai_intent_classifier_can_detect_manual_publish(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            wf = workflow.Workflow(config)
            classifier = FakeIntentClassifier(
                workflow.BotIntent(
                    "manual_publish",
                    reason="ai",
                    platforms=("wechat",),
                )
            )
            intent = wf.detect_event_intent("把我这张照片放到公众号", intent_client=classifier)
            self.assertEqual(intent.kind, "manual_publish")
            self.assertEqual(intent.platforms, ("wechat",))
            self.assertEqual(len(classifier.calls), 1)

    def test_ai_intent_classifier_failure_falls_back_to_rules_when_not_required(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            wf = workflow.Workflow(config)
            classifier = FakeIntentClassifier(workflow.WorkflowError("boom"))
            intent = wf.detect_event_intent("公众号发一下", intent_client=classifier)
            self.assertEqual(intent.kind, "workflow")
            self.assertEqual(len(classifier.calls), 1)

    def test_required_ai_publish_intent_failure_does_not_publish(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            config["intent_classifier_required_for_publish"] = True
            wf = workflow.Workflow(config)
            classifier = FakeIntentClassifier(workflow.WorkflowError("boom"))
            intent = wf.detect_event_intent("两个平台都发", intent_client=classifier)
            self.assertEqual(intent.kind, "ignore")
            self.assertEqual(intent.reason, "intent_classifier_failed_required")
            self.assertEqual(len(classifier.calls), 1)

    def test_parse_publish_selection(self):
        self.assertEqual(workflow.parse_publish_selection("用第 1 张图发")["image_number"], 1)
        selection = workflow.parse_publish_selection("图 2 配文案 1 发公众号")
        self.assertEqual(selection["image_number"], 2)
        self.assertEqual(selection["caption_number"], 1)
        selection = workflow.parse_publish_selection("第 3 张图和第 2 篇文案发两个平台")
        self.assertEqual(selection["image_number"], 3)
        self.assertEqual(selection["caption_number"], 2)

    def test_normalize_deepseek_model_alias(self):
        self.assertEqual(workflow.normalize_deepseek_model("v4-flash"), "deepseek-v4-flash")
        self.assertEqual(workflow.normalize_deepseek_model("v4-pro"), "deepseek-v4-pro")
        self.assertEqual(workflow.normalize_deepseek_model("deepseek-v4-flash"), "deepseek-v4-flash")

    def test_resolve_timezone_fallback(self):
        tz = workflow.resolve_timezone("Asia/Shanghai")
        sample = workflow.dt.datetime(2026, 6, 8, tzinfo=tz)
        self.assertEqual(sample.utcoffset().total_seconds(), 8 * 3600)

    def test_local_caption_plain_text_format(self):
        character = workflow.Character(
            ordinal=1,
            name="宇智波佐助",
            work="火影忍者",
            line_index=0,
            line="",
            used=False,
        )
        caption = workflow.local_caption(character)
        self.assertEqual(caption["title"], "宇智波佐助 | 火影忍者")
        self.assertLessEqual(len(caption["topics"]), 10)
        text = workflow.caption_text(caption)
        self.assertTrue(text.startswith("宇智波佐助 | 火影忍者\n\n"))
        self.assertIn("\n\n#宇智波佐助 #火影忍者", text)
        self.assertNotIn("标题：", text)
        self.assertNotIn("文案：", text)
        self.assertNotIn("话题：", text)
        self.assertNotIn("的记忆点", text.splitlines()[0])
        self.assertFalse(text.startswith("# "))

    def test_caption_text_normalizes_and_limits_topics(self):
        text = workflow.caption_text(
            {
                "title": "标题",
                "copy": "短文案",
                "topics": [f"话题{i}" for i in range(12)],
            }
        )
        self.assertEqual(text.count("#"), 10)
        self.assertIn("\n\n#话题0 #话题1", text)

    def test_ai_caption_prompt_asks_for_quote_only_copy(self):
        character = workflow.Character(
            ordinal=1,
            name="日向雏田",
            work="火影忍者",
            line_index=0,
            line="",
            used=False,
        )
        prompt = workflow.ai_caption_prompt(character)["content"]
        self.assertIn("只写这个角色", prompt)
        self.assertIn("经典原话", prompt)
        self.assertIn("不要写解释", prompt)
        self.assertIn("不要写剧情描述", prompt)
        self.assertNotIn("自然嵌入文案", prompt)

    def test_selection_preview_describes_ai_caption_without_local_copy(self):
        character = workflow.Character(
            ordinal=1,
            name="日向雏田",
            work="火影忍者",
            line_index=0,
            line="",
            used=False,
        )
        selection = workflow.Selection(
            run_id="2026-06-08-09",
            reference_image=Path("001.jpg"),
            character=character,
            prompt="生成 日向雏田",
        )
        preview = workflow.selection_to_dict(selection)["caption_preview"]
        self.assertEqual(preview["provider"], "ai_text")
        self.assertIn("AI", preview["copy_instruction"])
        self.assertNotIn("气质很适合这一张图", json.dumps(preview, ensure_ascii=False))

    def test_vs_plugin_caption_uses_ai_text_client(self):
        class FakeAITextCaptionClient:
            def __init__(self, config):
                self.config = config

            def generate_caption(self, character):
                return {
                    "title": f"{character.name} | {character.work}",
                    "copy": "我一定会成为火影。说到做到，这就是我的忍道。",
                    "topics": [f"#{character.name}", f"#{character.work}", "#经典台词"],
                }

        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            client = workflow.VSPluginWorkflowClient(config)
            character = workflow.Character(
                ordinal=1,
                name="漩涡鸣人",
                work="火影忍者",
                line_index=0,
                line="",
                used=False,
            )
            selection = workflow.Selection(
                run_id="2026-06-08-02",
                reference_image=Path(config["reference_image_dir"]) / "001.jpg",
                character=character,
                prompt="生成 漩涡鸣人",
            )
            with mock.patch.object(
                workflow, "AITextCaptionClient", FakeAITextCaptionClient, create=True
            ):
                caption = client.generate_caption(selection)

        self.assertIn("我一定会成为火影", caption["copy"])
        self.assertNotIn("气质很适合这一张图", caption["copy"])

    def test_vs_plugin_codex_exe_falls_forward_to_installed_extension(self):
        with tempfile.TemporaryDirectory() as td:
            extensions_root = Path(td) / "codex-official-b-extensions"
            old_exe = (
                extensions_root
                / "openai.chatgpt-26.602.71036-win32-x64"
                / "bin"
                / "windows-x86_64"
                / "codex.exe"
            )
            new_exe = (
                extensions_root
                / "openai.chatgpt-26.609.30741-win32-x64"
                / "bin"
                / "windows-x86_64"
                / "codex.exe"
            )
            new_exe.parent.mkdir(parents=True)
            new_exe.write_text("", encoding="utf-8")

            self.assertEqual(workflow.resolve_vs_plugin_codex_exe(str(old_exe)), str(new_exe))

    def test_text_message_content_preserves_multiline_text(self):
        text = "标题\n\n文案\n\n#话题"
        content = workflow.text_message_content(text)
        self.assertEqual(json.loads(content), {"text": text})
        self.assertNotIn("\\\\n", content)

    def test_lark_command_prefix_uses_node_for_cmd_launcher(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            launcher = root / "lark-cli.cmd"
            launcher.write_text("@ECHO off\n", encoding="utf-8")
            script = root / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js"
            script.parent.mkdir(parents=True)
            script.write_text("", encoding="utf-8")
            node = root / "node.exe"
            node.write_text("", encoding="utf-8")

            def fake_which(name):
                if name == "lark-cli":
                    return str(launcher)
                if name == "node":
                    return str(node)
                return None

            with mock.patch.object(workflow.shutil, "which", side_effect=fake_which):
                self.assertEqual(workflow.lark_command_prefix(), [str(node), str(script)])
                self.assertEqual(
                    workflow.lark_command_prefix("xhs-post-bot"),
                    [str(node), str(script), "--profile", "xhs-post-bot"],
                )

    def test_feishu_read_as_controls_poll_identity(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            config["feishu_read_as"] = "bot"
            client = CapturingFeishuClient(config)
            client.list_recent_messages()
            self.assertIn("--as", client.calls[0])
            self.assertEqual(client.calls[0][client.calls[0].index("--as") + 1], "bot")

    def test_parse_character_pool_skips_used(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            characters = workflow.parse_character_pool(Path(config["character_pool_path"]))
            self.assertEqual(len(characters), 3)
            self.assertEqual([c.name for c in characters if not c.used], ["漩涡鸣人", "春野樱"])

    def test_select_materials_uses_next_available(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            selections = workflow.select_materials(config, 2, "2026-06-08")
            self.assertEqual([s.run_id for s in selections], ["2026-06-08-02", "2026-06-08-03"])
            self.assertEqual([s.reference_image.name for s in selections], ["001.jpg", "002.jpg"])
            self.assertEqual([s.character.name for s in selections], ["漩涡鸣人", "春野樱"])

    def test_mock_run_commits_after_success(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            wf = workflow.Workflow(config)
            results = wf.run_batch(
                1,
                date_str="2026-06-08",
                openai_client=workflow.MockOpenAIWorkflowClient(),
                feishu_client=workflow.MockFeishuClient(),
            )
            self.assertEqual(results[0]["status"], "completed")
            self.assertTrue((Path(config["reference_image_dir"]) / "2026-06-08-02__001.jpg").exists())
            pool_text = Path(config["character_pool_path"]).read_text(encoding="utf-8")
            self.assertIn("[USED:2026-06-08-02]", pool_text)
            self.assertTrue((Path(config["output_root"]) / "2026-06-08-02" / "image.png").exists())
            self.assertEqual(results[0]["feishu"]["caption_message_type"], "text")
            caption_text = (
                Path(config["output_root"]) / "2026-06-08-02" / "caption.md"
            ).read_text(encoding="utf-8")
            self.assertNotIn("标题：", caption_text)
            self.assertNotIn("# 漩涡鸣人", caption_text)
            latest = json.loads(
                (Path(config["state_dir"]) / "latest_publish_candidate.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(latest["run_id"], "2026-06-08-02")
            self.assertEqual(latest["publish"]["title"], "漩涡鸣人 | 火影忍者")
            self.assertEqual(latest["publish"]["tags"][0], "漩涡鸣人")

    def test_no_commit_when_feishu_fails(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            wf = workflow.Workflow(config)
            with self.assertRaises(workflow.WorkflowError):
                wf.run_batch(
                    1,
                    date_str="2026-06-08",
                    openai_client=workflow.MockOpenAIWorkflowClient(),
                    feishu_client=FailingFeishu(),
                )
            self.assertTrue((Path(config["reference_image_dir"]) / "001.jpg").exists())
            pool_text = Path(config["character_pool_path"]).read_text(encoding="utf-8")
            self.assertNotIn("[USED:2026-06-08-02]", pool_text)
            metadata = json.loads(
                (Path(config["output_root"]) / "2026-06-08-02" / "metadata.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(metadata["status"], "failed")

    def test_no_commit_output_does_not_reserve_run_id(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            wf = workflow.Workflow(config)
            wf.run_batch(
                1,
                date_str="2026-06-08",
                openai_client=workflow.MockOpenAIWorkflowClient(),
                feishu_client=workflow.MockFeishuClient(),
                no_commit=True,
            )
            selections = workflow.select_materials(config, 1, "2026-06-08")
            self.assertEqual(selections[0].run_id, "2026-06-08-02")

    def test_multi_run_saves_numbered_candidate_pool_with_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            wf = workflow.Workflow(config)
            feishu = RecordingFeishu()
            wf.run_batch(
                2,
                date_str="2026-06-08",
                openai_client=workflow.MockOpenAIWorkflowClient(),
                feishu_client=feishu,
                no_commit=True,
            )
            pool = json.loads(
                (Path(config["state_dir"]) / "latest_candidate_pool.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(pool["default_image_number"], 2)
            self.assertEqual(pool["default_caption_number"], 1)
            self.assertEqual([item["number"] for item in pool["images"]], [1, 2])
            self.assertEqual([item["number"] for item in pool["captions"]], [1, 2])
            self.assertIn("图 1、图 2", feishu.texts[-1])
            latest = json.loads(
                (Path(config["state_dir"]) / "latest_publish_candidate.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(latest["selection"]["image_number"], 2)
            self.assertEqual(latest["selection"]["caption_number"], 1)
            self.assertIn("2026-06-08-03", latest["image"])
            self.assertEqual(latest["publish"]["title"], "漩涡鸣人 | 火影忍者")

    def test_handle_event_filters_chat_and_parses_count(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            wf = workflow.Workflow(config)
            ignored = wf.handle_event(
                {"chat_id": "oc_other", "content": "生成2篇"},
                openai_client=workflow.MockOpenAIWorkflowClient(),
                feishu_client=workflow.MockFeishuClient(),
                no_commit=True,
            )
            self.assertIsNone(ignored)
            results = wf.handle_event(
                {"chat_id": "oc_test", "content": "今天发2篇"},
                openai_client=workflow.MockOpenAIWorkflowClient(),
                feishu_client=workflow.MockFeishuClient(),
                no_commit=True,
            )
            self.assertEqual(len(results), 2)
            today = workflow.today_string(config["timezone"])
            self.assertEqual([item["run_id"] for item in results], [f"{today}-01", f"{today}-02"])

    def test_handle_event_sends_ack_before_batch(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            wf = workflow.Workflow(config)
            feishu = RecordingFeishu()
            results = wf.handle_event(
                {"chat_id": "oc_test", "content": "生成今天文章"},
                openai_client=workflow.MockOpenAIWorkflowClient(),
                feishu_client=feishu,
                no_commit=True,
            )
            self.assertEqual(len(results), 1)
            self.assertEqual(len(feishu.texts), 1)
            self.assertIn("开始生成 1 篇", feishu.texts[0])
            self.assertEqual(len(feishu.posts), 1)

    def test_xhs_vision_publish_command_runs_dry_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(root)
            config["xhs_vision_publish_enabled"] = True
            screenshot = root / "probe.png"
            screenshot.write_bytes(workflow.MockOpenAIWorkflowClient.PNG_BYTES)
            state = {
                "xhs_workflow_run_id": "run-1",
                "candidate_image": str(root / "image.png"),
                "title": "标题",
                "status": "awaiting_confirm",
                "dry_run_result": {
                    "dry_run_completed": True,
                    "publish_button_visible": True,
                    "risk_warning_found": False,
                    "screenshot_path": str(screenshot),
                },
                "screenshot_path": str(screenshot),
            }
            wf = workflow.Workflow(config)
            feishu = RecordingFeishu()
            with mock.patch.object(workflow.xhs_vision_bridge, "start_dry_run", return_value=state):
                result = wf.handle_event({"chat_id": "oc_test", "content": "发布小红书"}, feishu_client=feishu)
            self.assertEqual(result["kind"], "xhs_vision_dry_run")
            self.assertIn("确认发布 run-1", result["reply"])
            self.assertEqual(len(feishu.posts), 1)

    def test_xhs_vision_confirm_command_runs_publish_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(root)
            config["xhs_vision_publish_enabled"] = True
            screenshot = root / "publish.png"
            screenshot.write_bytes(workflow.MockOpenAIWorkflowClient.PNG_BYTES)
            state = {
                "xhs_workflow_run_id": "run-1",
                "candidate_image": str(root / "image.png"),
                "title": "标题",
                "status": "submitted",
                "dry_run_result": {"dry_run_completed": True, "risk_warning_found": False},
                "publish_result": {
                    "publish_attempted": True,
                    "submitted_or_reviewing": True,
                    "risk_warning_found": False,
                    "screenshot_path": str(screenshot),
                },
                "screenshot_path": str(screenshot),
            }
            wf = workflow.Workflow(config)
            feishu = RecordingFeishu()
            with mock.patch.object(workflow.xhs_vision_bridge, "confirm_publish", return_value=state) as mocked:
                result = wf.handle_event({"chat_id": "oc_test", "content": "确认发布 run-1"}, feishu_client=feishu)
            mocked.assert_called_once()
            self.assertEqual(result["kind"], "xhs_vision_confirm")
            self.assertIn("已提交", result["reply"])
            self.assertEqual(len(feishu.posts), 1)

    def test_xhs_vision_confirm_message_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(root)
            config["xhs_vision_publish_enabled"] = True
            state = {
                "xhs_workflow_run_id": "run-1",
                "status": "submitted",
                "publish_result": {"publish_attempted": True, "submitted_or_reviewing": True},
            }
            wf = workflow.Workflow(config)
            feishu = RecordingFeishu()
            event = {
                "chat_id": "oc_test",
                "message_id": "om_1",
                "content": "确认发布 run-1",
            }
            with mock.patch.object(workflow.xhs_vision_bridge, "confirm_publish", return_value=state) as mocked:
                first = wf.handle_event(event, feishu_client=feishu)
                second = wf.handle_event(event, feishu_client=feishu)
            self.assertEqual(first["kind"], "xhs_vision_confirm")
            self.assertEqual(second["kind"], "duplicate_command")
            mocked.assert_called_once()

    def test_wechat_mp_prepare_command_runs_prepare_and_sends_confirm_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(root)
            screenshot = root / "wechat-ready.png"
            screenshot.write_bytes(workflow.MockOpenAIWorkflowClient.PNG_BYTES)
            state = {
                "xhs_workflow_run_id": "run-1",
                "candidate_image": str(root / "image.png"),
                "title": "标题",
                "status": "awaiting_confirm",
                "prepare_result": {
                    "prepare_completed": True,
                    "publish_button_visible": True,
                    "risk_warning_found": False,
                    "screenshot_path": str(screenshot),
                },
                "screenshot_path": str(screenshot),
            }
            wf = workflow.Workflow(config)
            feishu = RecordingFeishu()
            with mock.patch.object(workflow.wechat_mp_bridge, "start_prepare", return_value=state):
                result = wf.handle_event({"chat_id": "oc_test", "content": "发布公众号"}, feishu_client=feishu)
            self.assertEqual(result["kind"], "wechat_mp_prepare")
            self.assertIn("可以发表 run-1", result["reply"])
            self.assertIn("公众号开始准备发布预检", feishu.texts[0])
            self.assertEqual(len(feishu.posts), 1)

    def test_wechat_mp_confirm_command_runs_final_publish(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(root)
            screenshot = root / "wechat-published.png"
            screenshot.write_bytes(workflow.MockOpenAIWorkflowClient.PNG_BYTES)
            awaiting = {"xhs_workflow_run_id": "run-1", "status": "awaiting_confirm"}
            published = {
                "xhs_workflow_run_id": "run-1",
                "status": "published",
                "publish_result": {
                    "published": True,
                    "published_clicks_completed": True,
                    "risk_warning_found": False,
                    "screenshot_path": str(screenshot),
                },
                "screenshot_path": str(screenshot),
            }
            wf = workflow.Workflow(config)
            feishu = RecordingFeishu()
            with mock.patch.object(workflow.wechat_mp_bridge, "load_publish_state", return_value=awaiting), mock.patch.object(
                workflow.wechat_mp_bridge, "confirm_publish", return_value=published
            ) as mocked:
                result = wf.handle_event({"chat_id": "oc_test", "content": "可以发表 run-1"}, feishu_client=feishu)
            mocked.assert_called_once()
            self.assertEqual(result["kind"], "wechat_mp_confirm")
            self.assertIn("公众号已验证发表", result["reply"])
            self.assertIn("开始执行最终发表", feishu.texts[0])
            self.assertEqual(len(feishu.posts), 1)

    def test_scheduled_daily_run_stops_at_confirmation_gates(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            config["xhs_vision_publish_enabled"] = True
            wf = workflow.Workflow(config)
            feishu = RecordingFeishu()
            candidate = {
                "run_id": "run-1",
                "image": str(Path(td) / "image.png"),
                "publish": {"title": "标题", "note": "正文", "tags": []},
            }
            with mock.patch.object(wf, "run_batch", return_value=[{"run_id": "run-1"}]) as run_batch, mock.patch.object(
                wf, "load_latest_publish_candidate", return_value=candidate
            ), mock.patch.object(wf, "handle_wechat_mp_prepare", return_value={"kind": "wechat_mp_prepare"}) as wechat_prepare, mock.patch.object(
                wf, "handle_xhs_vision_dry_run", return_value={"kind": "xhs_vision_dry_run"}
            ) as xhs_dry_run:
                result = wf.scheduled_daily_run(
                    platforms=("wechat", "xiaohongshu"),
                    feishu_client=feishu,
                    no_commit=True,
                )
            self.assertTrue(result["ok"])
            self.assertTrue(result["final_publish_requires_feishu_confirm"])
            self.assertEqual([action["kind"] for action in result["actions"]], ["wechat_mp_prepare", "xhs_vision_dry_run"])
            run_batch.assert_called_once()
            wechat_prepare.assert_called_once_with(feishu_client=feishu, candidate=candidate)
            xhs_dry_run.assert_called_once_with(feishu_client=feishu, candidate=candidate)
            self.assertIn("最终发布仍需飞书确认", feishu.texts[0])

    def test_real_publish_path_is_blocked_when_bridge_enabled(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(root)
            config["xhs_vision_publish_enabled"] = True
            image = root / "image.png"
            image.write_bytes(workflow.MockOpenAIWorkflowClient.PNG_BYTES)
            candidate = {
                "run_id": "run-1",
                "image": str(image),
                "publish": {"title": "标题", "note": "正文", "tags": []},
            }
            result = workflow.Workflow(config).publish_candidate(("xiaohongshu", "wechat"), candidate)
            self.assertEqual(result["kind"], "publish")
            self.assertIn("确认闭环", result["reply"])
            self.assertFalse(any(item.get("ok") for item in result["results"]))

    def test_handle_event_prompts_for_publish_platform_then_publishes(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            wf = workflow.Workflow(config)
            wf.run_batch(
                1,
                date_str="2026-06-08",
                openai_client=workflow.MockOpenAIWorkflowClient(),
                feishu_client=workflow.MockFeishuClient(),
                no_commit=True,
            )
            feishu = RecordingFeishu()
            publisher = RecordingPublisher()
            prompt = wf.handle_event(
                {"chat_id": "oc_test", "content": "帮我发布"},
                feishu_client=feishu,
                publish_client=publisher,
                no_commit=True,
            )
            self.assertEqual(prompt["kind"], "publish_prompt")
            self.assertEqual(feishu.texts[-1], workflow.PUBLISH_CONFIRM_TEXT)
            self.assertTrue((Path(config["state_dir"]) / "publish_pending.json").exists())

            result = wf.handle_event(
                {"chat_id": "oc_test", "content": "小红书"},
                feishu_client=feishu,
                publish_client=publisher,
                no_commit=True,
            )
            self.assertEqual(result["kind"], "publish")
            self.assertEqual(feishu.texts[-2], workflow.publish_ack_text())
            self.assertEqual(feishu.texts[-1], "提交完成，请以平台页面为准。")
            self.assertEqual(publisher.calls[0][0], "xiaohongshu")
            self.assertFalse((Path(config["state_dir"]) / "publish_pending.json").exists())

    def test_required_ai_blocks_pending_platform_fallback_when_classifier_fails(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            config["intent_classifier_required_for_publish"] = True
            wf = workflow.Workflow(config)
            wf.run_batch(
                1,
                date_str="2026-06-08",
                openai_client=workflow.MockOpenAIWorkflowClient(),
                feishu_client=workflow.MockFeishuClient(),
                no_commit=True,
            )
            feishu = RecordingFeishu()
            publisher = RecordingPublisher()
            wf.handle_event(
                {"chat_id": "oc_test", "content": "帮我发布"},
                feishu_client=feishu,
                publish_client=publisher,
                intent_client=FakeIntentClassifier(workflow.BotIntent("publish", reason="ai")),
                no_commit=True,
            )
            result = wf.handle_event(
                {"chat_id": "oc_test", "content": "小红书"},
                feishu_client=feishu,
                publish_client=publisher,
                intent_client=FakeIntentClassifier(workflow.WorkflowError("boom")),
                no_commit=True,
            )
            self.assertIsNone(result)
            self.assertEqual(publisher.calls, [])
            self.assertTrue((Path(config["state_dir"]) / "publish_pending.json").exists())

    def test_handle_event_publish_to_both_platforms(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            wf = workflow.Workflow(config)
            wf.run_batch(
                1,
                date_str="2026-06-08",
                openai_client=workflow.MockOpenAIWorkflowClient(),
                feishu_client=workflow.MockFeishuClient(),
                no_commit=True,
            )
            feishu = RecordingFeishu()
            publisher = RecordingPublisher()
            result = wf.handle_event(
                {"chat_id": "oc_test", "content": "两个平台都发"},
                feishu_client=feishu,
                publish_client=publisher,
                no_commit=True,
            )
            self.assertEqual(result["kind"], "publish")
            self.assertEqual([call[0] for call in publisher.calls], ["xiaohongshu", "wechat"])
            self.assertEqual(feishu.texts[-2], workflow.publish_ack_text())
            self.assertEqual(feishu.texts[-1], "提交完成，请以平台页面为准。")

    def test_handle_event_publishes_selected_image_and_caption(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            wf = workflow.Workflow(config)
            wf.run_batch(
                2,
                date_str="2026-06-08",
                openai_client=workflow.MockOpenAIWorkflowClient(),
                feishu_client=workflow.MockFeishuClient(),
                no_commit=True,
            )
            feishu = RecordingFeishu()
            publisher = RecordingPublisher()
            result = wf.handle_event(
                {"chat_id": "oc_test", "content": "图 2 配文案 1 发公众号"},
                feishu_client=feishu,
                publish_client=publisher,
                no_commit=True,
            )
            self.assertEqual(result["kind"], "publish")
            self.assertEqual(publisher.calls[0][0], "wechat")
            candidate = publisher.calls[0][1]
            self.assertEqual(candidate["selection"]["image_number"], 2)
            self.assertEqual(candidate["selection"]["caption_number"], 1)
            self.assertIn("2026-06-08-03", candidate["image"])
            self.assertEqual(candidate["publish"]["title"], "漩涡鸣人 | 火影忍者")

    def test_handle_event_asks_for_missing_caption_when_multiple_exist(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            wf = workflow.Workflow(config)
            wf.run_batch(
                2,
                date_str="2026-06-08",
                openai_client=workflow.MockOpenAIWorkflowClient(),
                feishu_client=workflow.MockFeishuClient(),
                no_commit=True,
            )
            feishu = RecordingFeishu()
            publisher = RecordingPublisher()
            result = wf.handle_event(
                {"chat_id": "oc_test", "content": "用第 1 张图发"},
                feishu_client=feishu,
                publish_client=publisher,
                no_commit=True,
            )
            self.assertEqual(result["kind"], "publish_prompt")
            self.assertEqual(feishu.texts[-1], "请选择文案编号。")
            self.assertEqual(publisher.calls, [])

    def test_handle_event_publish_to_both_uses_multi_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            wf = workflow.Workflow(config)
            wf.run_batch(
                2,
                date_str="2026-06-08",
                openai_client=workflow.MockOpenAIWorkflowClient(),
                feishu_client=workflow.MockFeishuClient(),
                no_commit=True,
            )
            feishu = RecordingFeishu()
            publisher = RecordingPublisher()
            result = wf.handle_event(
                {"chat_id": "oc_test", "content": "帮我发在两个平台上"},
                feishu_client=feishu,
                publish_client=publisher,
                no_commit=True,
            )
            self.assertEqual(result["kind"], "publish")
            self.assertEqual([call[0] for call in publisher.calls], ["xiaohongshu", "wechat"])
            for _, candidate in publisher.calls:
                self.assertEqual(candidate["selection"]["image_number"], 2)
                self.assertEqual(candidate["selection"]["caption_number"], 1)
                self.assertIn("2026-06-08-03", candidate["image"])
                self.assertEqual(candidate["publish"]["title"], "漩涡鸣人 | 火影忍者")

    def test_handle_event_manual_upload_publishes_known_platform(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            wf = workflow.Workflow(config)
            feishu = RecordingFeishu()
            publisher = RecordingPublisher()

            prompt = wf.handle_event(
                {"chat_id": "oc_test", "content": "我想发自己的图片发公众号"},
                feishu_client=feishu,
                publish_client=publisher,
                no_commit=True,
            )
            self.assertEqual(prompt["kind"], "manual_publish_prompt")
            self.assertEqual(feishu.texts[-1], workflow.manual_image_request_text())

            image_result = wf.handle_event(
                {
                    "chat_id": "oc_test",
                    "content": "[Image: img_manual_123]",
                    "msg_type": "image",
                    "message_id": "om_manual",
                },
                feishu_client=feishu,
                publish_client=publisher,
                no_commit=True,
            )
            self.assertEqual(image_result["kind"], "manual_publish_image")
            self.assertEqual(feishu.texts[-1], workflow.manual_caption_request_text())
            state = wf.load_manual_publish_state()
            self.assertIsNotNone(state)
            self.assertEqual(state.stage, "awaiting_caption")
            self.assertTrue(Path(state.image_path).exists())

            confirm = wf.handle_event(
                {
                    "chat_id": "oc_test",
                    "content": "手动标题\n\n这是一段手动文案。\n\n#手动标签",
                },
                feishu_client=feishu,
                publish_client=publisher,
                no_commit=True,
            )
            self.assertEqual(confirm["kind"], "manual_publish_confirm")
            self.assertEqual(feishu.texts[-1], workflow.manual_confirm_text())

            result = wf.handle_event(
                {"chat_id": "oc_test", "content": "确定"},
                feishu_client=feishu,
                publish_client=publisher,
                no_commit=True,
            )
            self.assertEqual(result["kind"], "publish")
            self.assertEqual(feishu.texts[-2], workflow.publish_ack_text())
            self.assertEqual(feishu.texts[-1], "提交完成，请以平台页面为准。")
            self.assertEqual(publisher.calls[0][0], "wechat")
            candidate = publisher.calls[0][1]
            self.assertTrue(Path(candidate["image"]).exists())
            self.assertEqual(candidate["publish"]["title"], "手动标题")
            self.assertEqual(candidate["publish"]["note"], "这是一段手动文案。")
            self.assertEqual(candidate["publish"]["tags"], ["手动标签"])
            self.assertFalse((Path(config["state_dir"]) / "manual_publish_state.json").exists())

    def test_handle_event_manual_upload_asks_platform_after_confirm(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            wf = workflow.Workflow(config)
            feishu = RecordingFeishu()
            publisher = RecordingPublisher()

            wf.handle_event(
                {"chat_id": "oc_test", "content": "我想发自己的图片"},
                feishu_client=feishu,
                publish_client=publisher,
                no_commit=True,
            )
            wf.handle_event(
                {
                    "chat_id": "oc_test",
                    "content": "[Image: img_manual_456]",
                    "msg_type": "image",
                    "message_id": "om_manual_2",
                },
                feishu_client=feishu,
                publish_client=publisher,
                no_commit=True,
            )
            wf.handle_event(
                {"chat_id": "oc_test", "content": "标题\n\n文案"},
                feishu_client=feishu,
                publish_client=publisher,
                no_commit=True,
            )
            prompt = wf.handle_event(
                {"chat_id": "oc_test", "content": "确定"},
                feishu_client=feishu,
                publish_client=publisher,
                no_commit=True,
            )
            self.assertEqual(prompt["kind"], "publish_prompt")
            self.assertEqual(feishu.texts[-1], workflow.PUBLISH_CONFIRM_TEXT)
            self.assertEqual(publisher.calls, [])
            state = wf.load_manual_publish_state()
            self.assertIsNotNone(state)
            self.assertEqual(state.stage, "awaiting_platform")

            result = wf.handle_event(
                {"chat_id": "oc_test", "content": "两个平台都发"},
                feishu_client=feishu,
                publish_client=publisher,
                no_commit=True,
            )
            self.assertEqual(result["kind"], "publish")
            self.assertEqual([call[0] for call in publisher.calls], ["xiaohongshu", "wechat"])
            self.assertEqual(feishu.texts[-2], workflow.publish_ack_text())
            self.assertEqual(feishu.texts[-1], "提交完成，请以平台页面为准。")
            self.assertFalse((Path(config["state_dir"]) / "manual_publish_state.json").exists())

    def test_handle_event_ignores_image_without_manual_upload_state(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            config["general_answer_enabled"] = True
            wf = workflow.Workflow(config)
            feishu = RecordingFeishu()
            result = wf.handle_event(
                {
                    "chat_id": "oc_test",
                    "content": "[Image: img_lonely]",
                    "msg_type": "image",
                    "message_id": "om_lonely",
                },
                feishu_client=feishu,
                general_client=workflow.MockGeneralAnswerClient(),
                no_commit=True,
            )
            self.assertIsNone(result)
            self.assertEqual(feishu.texts, [])

    def test_xiaohongshu_publisher_uses_configured_account(self):
        class FakeLocalPublisher(workflow.LocalPublisher):
            def __init__(self, config):
                super().__init__(config, lambda payload: None)
                self.commands = []

            def _run_command(self, command, cwd, timeout, event):
                self.commands.append(command)
                return subprocess.CompletedProcess(command, 0, "valid", "")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sau = root / "sau.exe"
            sau.write_text("", encoding="utf-8")
            image = root / "image.png"
            image.write_bytes(workflow.MockOpenAIWorkflowClient.PNG_BYTES)
            config = self.make_config(root)
            config["xiaohongshu_sau_exe"] = str(sau)
            config["xiaohongshu_sau_root"] = str(root)
            candidate = {
                "run_id": "r1",
                "image": str(image),
                "publish": {
                    "title": "标题",
                    "note": "文案",
                    "tags": ["话题"],
                    "text": "标题\n\n文案\n\n#话题",
                },
            }
            publisher = FakeLocalPublisher(config)
            result = publisher.publish_xiaohongshu(candidate)
            self.assertTrue(result["ok"])
            for command in publisher.commands:
                account = command[command.index("--account") + 1]
                self.assertEqual(account, workflow.XIAOHONGSHU_ACCOUNT)

    def test_xiaohongshu_publisher_can_be_disabled_by_config(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            image = root / "image.png"
            image.write_bytes(workflow.MockOpenAIWorkflowClient.PNG_BYTES)
            config = self.make_config(root)
            config["xiaohongshu_publish_enabled"] = False
            candidate = {
                "run_id": "r1",
                "image": str(image),
                "publish": {
                    "title": "标题",
                    "note": "文案",
                    "tags": ["话题"],
                    "text": "标题\n\n文案\n\n#话题",
                },
            }
            result = workflow.LocalPublisher(config, lambda payload: None).publish_xiaohongshu(candidate)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "小红书自动化发布已禁用。")

    def test_wechat_publisher_uses_browser_method_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            script = root / "wechat_browser.py"
            script.write_text(
                "import json, sys\n"
                "sys.stdout.reconfigure(encoding='utf-8', errors='replace')\n"
                "from pathlib import Path\n"
                "payload = json.loads(Path(sys.argv[sys.argv.index('--payload') + 1]).read_text(encoding='utf-8'))\n"
                "print(json.dumps({'ok': True, 'action': payload['action'], 'title': payload['title'], 'image_count': len(payload['images'])}, ensure_ascii=False))\n",
                encoding="utf-8",
            )
            image = root / "image.png"
            image.write_bytes(workflow.MockOpenAIWorkflowClient.PNG_BYTES)
            config = self.make_config(root)
            config["wechat_browser_script"] = str(script)
            config["wechat_browser_action"] = "publish"
            events = []
            candidate = {
                "run_id": "r1",
                "image": str(image),
                "publish": {
                    "title": "标题",
                    "note": "文案",
                    "tags": ["话题"],
                    "text": "标题\n\n文案\n\n#话题",
                },
            }
            publisher = workflow.LocalPublisher(config, events.append)
            result = publisher.publish_wechat_sticker(candidate)
            self.assertTrue(result["ok"])
            self.assertEqual(result["method"], "browser")
            self.assertEqual(result["result"]["action"], "publish")
            self.assertEqual(result["result"]["title"], "标题")
            command_event = next(event for event in events if event["event"] == "wechat_browser_publish")
            payload_path = Path(command_event["command"][command_event["command"].index("--payload") + 1])
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["action"], "publish")
            self.assertEqual(payload["title"], "标题")
            self.assertEqual(payload["content"], "文案\n\n#话题")
            self.assertEqual(payload["images"], [str(image)])
            event_names = [event["event"] for event in events]
            self.assertIn("wechat_browser_publish", event_names)
            self.assertIn("wechat_browser_publish_result", event_names)
            self.assertNotIn("wechat_sticker_publish_result", event_names)

    def test_wechat_browser_qr_event_is_sent_to_feishu(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            script = root / "wechat_browser.py"
            script.write_text(
                "import json, sys, time\n"
                "sys.stdout.reconfigure(encoding='utf-8', errors='replace')\n"
                "from pathlib import Path\n"
                "payload = json.loads(Path(sys.argv[sys.argv.index('--payload') + 1]).read_text(encoding='utf-8'))\n"
                "qr_path = Path(payload['qr_event_path'])\n"
                "screenshot = Path(payload['profile_dir']) / 'diagnostics' / 'qr.png'\n"
                "screenshot.parent.mkdir(parents=True, exist_ok=True)\n"
                "screenshot.write_bytes(b'fakepng')\n"
                "qr_path.write_text(json.dumps({'event': 'wechat_qr_required', 'screenshot': str(screenshot), 'message': '请扫码确认'}, ensure_ascii=False), encoding='utf-8')\n"
                "time.sleep(1.2)\n"
                "print(json.dumps({'ok': True, 'action': payload['action']}, ensure_ascii=False))\n",
                encoding="utf-8",
            )
            image = root / "image.png"
            image.write_bytes(workflow.MockOpenAIWorkflowClient.PNG_BYTES)
            config = self.make_config(root)
            config["wechat_browser_script"] = str(script)
            config["wechat_browser_action"] = "publish"
            events = []
            feishu = RecordingFeishu()
            candidate = {
                "run_id": "r1",
                "image": str(image),
                "publish": {
                    "title": "标题",
                    "note": "文案",
                    "tags": [],
                    "text": "标题\n\n文案",
                },
            }
            with mock.patch.object(workflow, "FeishuClient", return_value=feishu):
                result = workflow.LocalPublisher(config, events.append).publish_wechat_sticker(candidate)
            self.assertTrue(result["ok"])
            self.assertEqual(len(feishu.posts), 1)
            qr_image, qr_text = feishu.posts[0]
            self.assertEqual(Path(qr_image).name, "qr.png")
            self.assertEqual(qr_text, "请扫码确认")
            event_names = [event["event"] for event in events]
            self.assertIn("wechat_browser_qr_sent", event_names)

    def test_wechat_publisher_legacy_api_requires_explicit_method(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            publisher_dir = root / "wechat"
            publisher_dir.mkdir()
            (publisher_dir / "publisher.py").write_text(
                "class WeChatPublisher:\n"
                "    def publish_image_to_all(self, image_path, tag_id=None):\n"
                "        return {'image_path': image_path, 'tag_id': tag_id, 'send_result': {'msg_id': 123}}\n",
                encoding="utf-8",
            )
            image = root / "image.png"
            image.write_bytes(workflow.MockOpenAIWorkflowClient.PNG_BYTES)
            config = self.make_config(root)
            config["wechat_publish_method"] = "api"
            config["wechat_publisher_dir"] = str(publisher_dir)
            config["wechat_publish_tag_id"] = 9
            events = []
            candidate = {
                "run_id": "r1",
                "image": str(image),
                "publish": {
                    "title": "标题",
                    "note": "文案",
                    "tags": ["话题"],
                    "text": "标题\n\n文案\n\n#话题",
                },
            }
            publisher = workflow.LocalPublisher(config, events.append)
            result = publisher.publish_wechat_sticker(candidate)
            self.assertTrue(result["ok"])
            self.assertEqual(result["result"]["tag_id"], 9)
            self.assertEqual(events[-1]["event"], "wechat_sticker_publish_result")
            self.assertTrue(events[-1]["ok"])

    def test_wechat_browser_check_parses_script_result(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            script = root / "wechat_browser.py"
            script.write_text(
                "import json, sys\n"
                "sys.stdout.reconfigure(encoding='utf-8', errors='replace')\n"
                "from pathlib import Path\n"
                "payload = json.loads(Path(sys.argv[sys.argv.index('--payload') + 1]).read_text(encoding='utf-8'))\n"
                "print(json.dumps({'ok': True, 'action': payload['action'], 'logged_in': True, 'has_image_text_entry': True, 'menu_texts': ['贴图']}, ensure_ascii=False))\n",
                encoding="utf-8",
            )
            config = self.make_config(root)
            config["wechat_browser_script"] = str(script)
            events = []
            result = workflow.LocalPublisher(config, events.append).check_wechat_browser()
            self.assertTrue(result["ok"])
            self.assertTrue(result["logged_in"])
            self.assertTrue(result["has_image_text_entry"])
            self.assertEqual(result["menu_texts"], ["贴图"])
            self.assertEqual(events[0]["event"], "wechat_browser_check")

    def test_wechat_browser_login_parses_script_result(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            script = root / "wechat_browser.py"
            script.write_text(
                "import json, sys\n"
                "sys.stdout.reconfigure(encoding='utf-8', errors='replace')\n"
                "from pathlib import Path\n"
                "payload = json.loads(Path(sys.argv[sys.argv.index('--payload') + 1]).read_text(encoding='utf-8'))\n"
                "print(json.dumps({'ok': True, 'action': payload['action'], 'logged_in': True, 'storage_state': str(Path(payload['profile_dir']) / 'storage_state.json')}, ensure_ascii=False))\n",
                encoding="utf-8",
            )
            config = self.make_config(root)
            config["wechat_browser_script"] = str(script)
            events = []
            result = workflow.LocalPublisher(config, events.append).login_wechat_browser()
            self.assertTrue(result["ok"])
            self.assertEqual(result["action"], "login")
            self.assertTrue(result["logged_in"])
            self.assertTrue(result["storage_state"].endswith("storage_state.json"))
            self.assertEqual(events[0]["event"], "wechat_browser_login")

    def test_wechat_failure_classification_prioritizes_api_permission(self):
        detail = (
            "群发图片消息失败 (错误码48001): api功能未授权，请确认公众号类型\n"
            "✓ 配置加载成功 (AppID: wx1268***)\n"
            "✓ 获取access_token成功"
        )
        self.assertEqual(workflow.classify_wechat_failure(detail), "公众号接口未授权。")

    def test_wechat_failure_classification_detects_real_invalid_config(self):
        self.assertEqual(
            workflow.classify_wechat_failure("获取access_token失败 (错误码40125): 无效的appsecret"),
            "公众号配置无效。",
        )

    def test_handle_event_general_answer_when_enabled(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            config["general_answer_enabled"] = True
            wf = workflow.Workflow(config)
            feishu = RecordingFeishu()
            result = wf.handle_event(
                {"chat_id": "oc_test", "content": "hello"},
                feishu_client=feishu,
                general_client=workflow.MockGeneralAnswerClient(),
                no_commit=True,
            )
            self.assertEqual(result["kind"], "chat")
            self.assertEqual(len(feishu.texts), 1)
            self.assertIn("普通回答：hello", feishu.texts[0])
            self.assertEqual(len(feishu.posts), 0)

    def test_handle_event_image_inbox_redirect(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            config["image_inbox_chat_link"] = "https://example.test/image-inbox"
            wf = workflow.Workflow(config)
            feishu = RecordingFeishu()
            result = wf.handle_event(
                {"chat_id": "oc_test", "content": "我要保存图片到电脑"},
                feishu_client=feishu,
                no_commit=True,
            )
            self.assertEqual(result["kind"], "image_inbox")
            self.assertEqual(len(feishu.texts), 1)
            self.assertIn("图片收件箱", feishu.texts[0])
            self.assertIn("https://example.test/image-inbox", feishu.texts[0])
            self.assertEqual(len(feishu.posts), 0)

    def test_failure_notice_is_short_and_actionable(self):
        text = workflow.failure_notice_text(
            "VS plugin image generation did not produce an image; ServerError; "
            + "x" * 2000
        )
        self.assertIn("这次没有生成成功", text)
        self.assertIn("ServerError", text)
        self.assertIn("没有标记已用", text)
        self.assertLess(len(text), 500)

    def test_process_polled_messages_only_handles_new_user_text(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            wf = workflow.Workflow(config)
            messages = [
                {
                    "chat_id": "oc_test",
                    "content": "生成今天文章",
                    "msg_type": "text",
                    "message_position": "10",
                    "sender": {"sender_type": "app"},
                },
                {
                    "chat_id": "oc_test",
                    "content": "生成今天文章",
                    "msg_type": "text",
                    "message_position": "11",
                    "sender": {"sender_type": "user"},
                },
            ]
            max_seen, handled = wf.process_polled_messages(
                messages,
                last_position=9,
                openai_client=workflow.MockOpenAIWorkflowClient(),
                feishu_client=workflow.MockFeishuClient(),
                no_commit=True,
            )
            self.assertEqual(max_seen, 11)
            self.assertEqual(handled, 1)

    def test_process_polled_messages_handles_manual_upload_image(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            wf = workflow.Workflow(config)
            wf.save_manual_publish_state("awaiting_image", platforms=("wechat",))
            feishu = RecordingFeishu()
            messages = [
                {
                    "chat_id": "oc_test",
                    "content": "[Image: img_poll_123]",
                    "msg_type": "image",
                    "message_position": "12",
                    "message_id": "om_poll",
                    "sender": {"sender_type": "user"},
                },
            ]
            max_seen, handled = wf.process_polled_messages(
                messages,
                last_position=11,
                openai_client=workflow.MockOpenAIWorkflowClient(),
                feishu_client=feishu,
                no_commit=True,
            )
            self.assertEqual(max_seen, 12)
            self.assertEqual(handled, 1)
            self.assertEqual(feishu.texts[-1], workflow.manual_caption_request_text())
            state = wf.load_manual_publish_state()
            self.assertIsNotNone(state)
            self.assertEqual(state.stage, "awaiting_caption")
            self.assertTrue(Path(state.image_path).exists())

    def test_poll_position_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            wf = workflow.Workflow(config)
            self.assertIsNone(wf.load_poll_position())
            wf.save_poll_position(123)
            self.assertEqual(wf.load_poll_position(), 123)

    def test_poll_position_is_scoped_to_chat(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            wf = workflow.Workflow(config)
            wf.save_poll_position(123)
            config["feishu_chat_id"] = "oc_other"
            self.assertIsNone(workflow.Workflow(config).load_poll_position())

    def test_poll_survives_transient_read_failure(self):
        class FlakyFeishuReader:
            calls = 0

            def __init__(self, config):
                self.config = config

            def list_recent_messages(self, page_size=20):
                FlakyFeishuReader.calls += 1
                if FlakyFeishuReader.calls == 1:
                    raise workflow.WorkflowError("temporary feishu network failure")
                return []

        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            wf = workflow.Workflow(config)
            wf.save_poll_position(0)
            args = types.SimpleNamespace(
                mock_feishu=True,
                mock_openai=True,
                arm_latest=False,
                page_size=20,
                process_existing=False,
                timeout=0.01,
                interval=0,
                max_triggers=0,
                no_commit=True,
            )
            original = workflow.FeishuClient
            workflow.FeishuClient = FlakyFeishuReader
            try:
                self.assertEqual(workflow.poll(args, wf), 0)
            finally:
                workflow.FeishuClient = original
            self.assertGreaterEqual(FlakyFeishuReader.calls, 2)

    def test_file_lock_removes_stale_dead_pid_lock(self):
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "workflow.lock"
            lock_path.write_text(
                json.dumps({"pid": 999999, "created_at": "2026-06-08T13:00:00+08:00"}),
                encoding="utf-8",
            )
            with workflow.FileLock(lock_path, timeout_seconds=1):
                owner = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertEqual(owner["pid"], workflow.os.getpid())

    def test_file_lock_timeout_reports_owner(self):
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "workflow.lock"
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": workflow.os.getpid(),
                        "created_at": "2026-06-08T13:00:00+08:00",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(workflow.WorkflowError, "held_by_pid"):
                with workflow.FileLock(lock_path, timeout_seconds=0):
                    pass

    def test_status_report_includes_next_and_latest_run(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            wf = workflow.Workflow(config)
            workflow.json_line(wf.runs_path, {"run_id": "r1", "status": "failed"})
            report = wf.status_report()
            self.assertEqual(report["feishu_chat_id"], "oc_test")
            self.assertEqual(report["available_reference_images"], 2)
            self.assertEqual(report["available_characters"], 2)
            self.assertEqual(report["latest_run"]["status"], "failed")
            self.assertEqual(report["next"]["run_id"][:10], report["date"])

    def test_doctor_report_local_checks_without_openai(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            Path(config["api_key_path"]).write_text("sk-test", encoding="utf-8")
            wf = workflow.Workflow(config)
            report = wf.doctor_report(check_openai=False)
            checks = report["checks"]
            self.assertTrue(checks["reference_image_dir"]["ok"])
            self.assertTrue(checks["character_pool"]["ok"])
            self.assertTrue(checks["api_key_file"]["ok"])
            self.assertNotIn("openai", checks)

    def test_feishu_layer_report_exposes_current_route(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            original = workflow.FeishuClient
            workflow.FeishuClient = FakeFeishuForMap
            try:
                report = workflow.Workflow(config).feishu_layer_report()
            finally:
                workflow.FeishuClient = original
            self.assertEqual(report["active_chat_id"], "oc_test")
            self.assertEqual(report["chat_info"]["data"]["name"], "小红书发文专用")
            self.assertIn("--content", report["workflow_interfaces"]["send_caption"])
            self.assertEqual(report["event_inventory"]["data"]["event_count"], 14)

    def test_vs_plugin_route_uses_danger_full_access_sandbox(self):
        route = Path(__file__).resolve().parents[2] / "vs_plugin_route_probe.ps1"
        text = route.read_text(encoding="utf-8")
        self.assertIn('sandbox = "danger-full-access"', text)
        self.assertNotIn('sandbox = "workspace-write"', text)

    def test_poll_watchdog_scripts_are_available(self):
        root = Path(__file__).resolve().parents[1]
        for name in [
            "watchdog_poll.ps1",
            "start_watchdog.ps1",
            "status_watchdog.ps1",
            "stop_watchdog.ps1",
            "install_watchdog_startup_task.ps1",
            "status_watchdog_startup_task.ps1",
            "uninstall_watchdog_startup_task.ps1",
        ]:
            self.assertTrue((root / name).exists(), name)

        watchdog = (root / "watchdog_poll.ps1").read_text(encoding="utf-8")
        self.assertIn("poll.pid.json", watchdog)
        self.assertIn("workflow.py", watchdog)
        self.assertIn("*workflow.py*", watchdog)
        self.assertIn("[string]$Python", watchdog)
        self.assertIn("Get-Command python", watchdog)
        self.assertIn("Start-Process -FilePath $Python", watchdog)

        startup = (root / "install_watchdog_startup_task.ps1").read_text(encoding="utf-8")
        self.assertIn("XHSWorkflowPollWatchdog", startup)
        self.assertIn("New-ScheduledTaskTrigger -AtLogOn", startup)
        self.assertIn("Startup", startup)


if __name__ == "__main__":
    unittest.main()
