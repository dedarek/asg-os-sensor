import unittest

from runtime import event_vocabulary as vocab


def codex_record(event, **payload):
    return {"event_type": event, "instance_id": "123:456.0", "timestamp_iso": "2026-09-14T10:00:00+00:00",
            "payload": {"event": event, **payload}}


def opencode_record(event, role, text):
    return {"event_type": event, "instance_id": "123:456.0", "timestamp_iso": "2026-09-14T10:00:00+00:00",
            "payload": {"event": event, "detail": {
                "session_id": "ses_1",
                "message": {"present": True, "value": '{"id":"msg_1","role":"%s"}' % role},
                "parts": {"present": True, "value": '[{"type":"text","text":"%s"}]' % text}}}}


class VocabularyTests(unittest.TestCase):
    def test_native_names_translate(self):
        self.assertEqual(vocab.canonical("UserPromptSubmit"), "user.input")
        self.assertEqual(vocab.canonical("user.prompt.submitted"), "user.input")
        self.assertEqual(vocab.canonical("Stop"), "assistant.output")
        self.assertEqual(vocab.canonical("SessionStart"), "hook.loaded")
        self.assertEqual(vocab.canonical("PreToolUse"), "tool.execute.before")
        self.assertEqual(vocab.canonical("post_tool_use"), "tool.execute.after")
        self.assertEqual(vocab.canonical("user.input"), "user.input")

    def test_unknown_names_are_reported_not_guessed(self):
        self.assertIsNone(vocab.canonical("brand.new.stage"))
        summary = vocab.vocabulary_summary([codex_record("brand.new.stage")])
        self.assertEqual(summary["unmapped_native_names"], ["brand.new.stage"])
        self.assertEqual(summary["mapped"], 0)

    def test_codex_user_prompt_is_visible_as_user_input(self):
        rows = [codex_record("user.prompt.submitted", prompt="请总结这个项目", session_id="s1")]
        conversation = vocab.conversation(rows)
        self.assertEqual([item["role"] for item in conversation], ["user"])
        self.assertEqual(conversation[0]["text"], "请总结这个项目")
        self.assertEqual(conversation[0]["text_source"], "prompt")

    def test_codex_stop_event_yields_assistant_text(self):
        rows = [codex_record("Stop", assistant_output="已经完成", session_id="s1")]
        conversation = vocab.conversation(rows)
        self.assertEqual(conversation[0]["role"], "assistant")
        self.assertEqual(conversation[0]["text"], "已经完成")

    def test_opencode_parts_are_unwrapped(self):
        rows = [opencode_record("user.input", "user", "你好"),
                opencode_record("assistant.output", "assistant", "你好，有什么可以帮你")]
        conversation = vocab.conversation(rows)
        self.assertEqual([item["role"] for item in conversation], ["user", "assistant"])
        self.assertEqual(conversation[1]["text"], "你好，有什么可以帮你")
        self.assertEqual(conversation[1]["message_id"], "msg_1")

    def test_role_mismatch_is_refused_instead_of_guessed(self):
        rows = [opencode_record("user.input", "assistant", "内部草稿")]
        self.assertEqual(vocab.conversation(rows), [])

    def test_duplicate_deliveries_collapse(self):
        rows = [codex_record("user.prompt.submitted", prompt="同一句话", session_id="s1"),
                codex_record("user.prompt.submitted", prompt="同一句话", session_id="s1")]
        self.assertEqual(len(vocab.conversation(rows)), 1)

    def test_missing_assistant_text_is_reported_not_invented(self):
        record = codex_record("Stop", assistant_output_missing=True)
        self.assertIsNone(vocab.text_for(record["payload"], "assistant"))
        self.assertEqual(vocab.conversation([record]), [])

    def test_nested_content_object_is_extracted(self):
        rows = [codex_record("user.input", session_id="s1", content={"prompt": "真实问题"}),
                codex_record("assistant.output", session_id="s1", content={"text": "真实回答"})]
        conversation = vocab.conversation(rows)
        self.assertEqual([item["text"] for item in conversation], ["真实问题", "真实回答"])
        self.assertEqual([item["text_source"] for item in conversation],
                         ["content.prompt", "content.text"])

    def test_json_content_messages_are_extracted(self):
        rows = [codex_record("user.input", session_id="s1", content=(
            '{"messages":[{"role":"user","content":'
            '[{"type":"text","text":"DSH 用户输入"}]}]}'))]
        conversation = vocab.conversation(rows)
        self.assertEqual(conversation[0]["text"], "DSH 用户输入")
        self.assertEqual(conversation[0]["text_source"], "content.messages")


if __name__ == "__main__":
    unittest.main()
