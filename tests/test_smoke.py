"""离线冒烟测试：不联网、不碰真实 Gmail 账号，验证解析/存储/标签/分类四条链路。

运行：  .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import classify  # noqa: E402
import store  # noqa: E402
from gmail_reader import GmailReader, html_to_text  # noqa: E402
from label import GmailLabels  # noqa: E402


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


class HTMLTest(unittest.TestCase):
    def test_strips_style_and_script(self):
        html = """
        <html><head><style>.a{color:red}</style></head>
        <body><p>选课截止日期：<b>10 月 30 日</b></p>
        <script>var x = 1;</script>
        <a href="https://ust.hk/enroll">点此报名</a></body></html>
        """
        text = html_to_text(html)
        self.assertIn("选课截止日期：10 月 30 日", text)
        self.assertIn("https://ust.hk/enroll", text)
        self.assertNotIn("color:red", text)
        self.assertNotIn("var x", text)

    def test_plain_text_passthrough(self):
        self.assertEqual(html_to_text("hello world"), "hello world")
        self.assertEqual(html_to_text(""), "")


class ParseMessageTest(unittest.TestCase):
    def test_multipart_with_attachment_and_html_only(self):
        payload = {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "Subject", "value": "Course enrollment"},
                {"name": "From", "value": "Registry <reg@ust.hk>"},
                {"name": "Date", "value": "Mon, 27 Oct 2025 09:00:00 +0800"},
            ],
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/html", "body": {"data": b64("<div>Add/drop <b>deadline</b> is Friday</div>")}},
                    ],
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "timetable.pdf",
                    "body": {"data": b64("should not appear")},
                },
            ],
        }
        msg = {"id": "abc123", "threadId": "t1", "labelIds": ["INBOX", "UNREAD"], "payload": payload}
        mail = GmailReader.parse_message(msg)

        self.assertEqual(mail["subject"], "Course enrollment")
        self.assertEqual(mail["from_email"], "reg@ust.hk")
        self.assertTrue(mail["has_attachment"])
        self.assertEqual(mail["attachment_names"], ["timetable.pdf"])
        self.assertIn("deadline is Friday", mail["body"])
        self.assertNotIn("should not appear", mail["body"])
        self.assertTrue(mail["is_unread"])

    def test_header_decoding_fallback(self):
        # 中文主题常见 =?UTF-8?B?...?= 编码，Gmail API 已经解码好；这里验证普通中文直通
        payload = {"mimeType": "text/plain", "headers": [{"name": "Subject", "value": "安全提醒"}],
                   "body": {"data": b64("辅助电话号码发生了变更")}}
        mail = GmailReader.parse_message({"id": "x", "payload": payload})
        self.assertEqual(mail["subject"], "安全提醒")
        self.assertIn("辅助电话号码", mail["body"])


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "mail.db"
        self.conn = store.init_db(self.db)
        self.mail = {
            "id": "m1", "thread_id": "t1", "subject": "Hello",
            "from_email": "a@ust.hk", "from_raw": "A <a@ust.hk>", "to": "me",
            "date": "2025-10-01T00:00:00+00:00", "snippet": "s", "body": "body",
            "web_link": "http://x", "label_ids": ["INBOX"], "is_unread": True,
            "is_important": False, "has_attachment": False, "size": 100,
        }

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_upsert_keeps_classification(self):
        store.save_emails(self.conn, [self.mail])
        store.update_classification(self.conn, "m1", "P1", "course", True)

        updated = {**self.mail, "subject": "Hello again", "body": ""}
        store.save_emails(self.conn, [updated])

        row = self.conn.execute("SELECT subject, body, priority, labeled FROM emails WHERE id='m1'").fetchone()
        self.assertEqual(row["subject"], "Hello again")
        self.assertEqual(row["body"], "body")  # 空正文不能覆盖旧正文
        self.assertEqual(row["priority"], "P1")
        self.assertEqual(row["labeled"], 1)

    def test_pending_and_retry_cap(self):
        store.save_emails(self.conn, [self.mail])
        self.assertEqual(len(store.get_unprocessed(self.conn)), 1)

        for _ in range(3):
            store.mark_failed(self.conn, "m1", "boom")
        self.assertEqual(store.get_unprocessed(self.conn), [])  # 超过上限不再重试

    def test_state_roundtrip(self):
        self.assertIsNone(store.get_state(self.conn, "k"))
        store.set_state(self.conn, "k", "123")
        self.assertEqual(store.get_state(self.conn, "k"), "123")
        store.set_state(self.conn, "k", "456")
        self.assertEqual(store.get_state(self.conn, "k"), "456")

    def test_migrates_legacy_schema(self):
        legacy = Path(self.tmp.name) / "legacy.db"
        conn = sqlite3.connect(legacy)
        conn.execute("""CREATE TABLE emails (
            id TEXT PRIMARY KEY, thread_id TEXT, subject TEXT, from_email TEXT,
            from_raw TEXT, to_field TEXT, date TEXT, snippet TEXT, body TEXT,
            web_link TEXT, label_ids TEXT, is_unread INTEGER, is_important INTEGER,
            priority TEXT, reason TEXT, labeled INTEGER, processed_at TEXT)""")
        conn.execute("INSERT INTO emails (id, subject, priority) VALUES ('old', '旧邮件', 'P4')")
        conn.commit()
        conn.close()

        migrated = store.init_db(legacy)
        cols = {r[1] for r in migrated.execute("PRAGMA table_info(emails)")}
        self.assertIn("error_count", cols)
        self.assertIn("has_attachment", cols)
        row = migrated.execute("SELECT subject, priority FROM emails WHERE id='old'").fetchone()
        self.assertEqual(row["subject"], "旧邮件")
        self.assertEqual(row["priority"], "P4")
        migrated.close()


class _FakeRequest:
    def __init__(self, calls, payload):
        self.calls, self.payload = calls, payload

    def execute(self):
        return {}


class _FakeMessages:
    def __init__(self, calls):
        self.calls = calls

    def modify(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeRequest(self.calls, kwargs)


class _FakeUsers:
    def __init__(self, calls):
        self._messages = _FakeMessages(calls)

    def messages(self):
        return self._messages


class _FakeService:
    def __init__(self):
        self.calls = []

    def users(self):
        return _FakeUsers(self.calls)


class LabelTest(unittest.TestCase):
    def setUp(self):
        self.service = _FakeService()
        self.labels = GmailLabels(service=self.service)
        self.labels._label_ids = {p: f"Label_{p}" for p in ("P0", "P1", "P2", "P3", "P4")}

    def test_applies_one_and_removes_others(self):
        self.assertTrue(self.labels.apply_priority("m1", "P1"))
        body = self.service.calls[-1]["body"]
        self.assertEqual(body["addLabelIds"], ["Label_P1"])
        self.assertCountEqual(body["removeLabelIds"], ["Label_P0", "Label_P2", "Label_P3", "Label_P4"])

    def test_unknown_priority_rejected(self):
        with self.assertRaises(ValueError):
            self.labels.apply_priority("m1", "P9")


class _FakeResponses:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _fake_client(response):
    return SimpleNamespace(responses=_FakeResponses(response))


class ClassifyTest(unittest.TestCase):
    def tearDown(self):
        classify.set_client(None)

    def test_reads_function_call(self):
        call = SimpleNamespace(
            type="function_call", name="apply_priority_label",
            arguments=json.dumps({"priority": "P0", "reason": "选课截止"}),
        )
        classify.set_client(_fake_client(SimpleNamespace(output=[call], output_text="")))
        result = classify.classify_email({"id": "m1", "subject": "Add/drop", "body": "deadline"})
        self.assertEqual(result["priority"], "P0")
        self.assertIsNone(result["error"])

    def test_falls_back_to_free_text(self):
        classify.set_client(_fake_client(SimpleNamespace(output=[], output_text="这封属于 P2，一般公告")))
        result = classify.classify_email({"id": "m1", "subject": "x"})
        self.assertEqual(result["priority"], "P2")

    def test_falls_back_when_tool_choice_rejected(self):
        class BadRequestError(Exception):
            pass

        call = SimpleNamespace(
            type="function_call", name="apply_priority_label",
            arguments=json.dumps({"priority": "P1", "reason": "实习"}),
        )
        response = SimpleNamespace(output=[call], output_text="")
        client = _fake_client(response)
        original = client.responses.create

        def strict_first(**kwargs):
            if kwargs.get("tool_choice") == "required":
                raise BadRequestError("tool_choice required not supported")
            return original(**kwargs)

        client.responses.create = strict_first
        classify.set_client(client)

        result = classify.classify_email({"id": "m1", "subject": "Internship"})
        self.assertEqual(result["priority"], "P1")
        self.assertEqual(client.responses.calls[-1]["tool_choice"], "auto")

    def test_missing_key_raises_config_error(self):
        classify.set_client(None)
        import config
        original = config.DEEPSEEK_API_KEY
        config.DEEPSEEK_API_KEY = ""
        try:
            with self.assertRaises(classify.ConfigError):
                classify.classify_email({"id": "m1", "subject": "x"})
        finally:
            config.DEEPSEEK_API_KEY = original


class PipelineTest(unittest.TestCase):
    """端到端串联测试：假 Gmail + 假模型，验证入库→分类→打标签→状态落库。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        import config
        self.db = Path(self.tmp.name) / "pipeline.db"
        self._orig_db = config.DB_PATH
        config.DB_PATH = self.db

        self.mail = {
            "id": "p1", "thread_id": "t1", "subject": "Internship opening",
            "from_email": "career@ust.hk", "from_raw": "Career <career@ust.hk>",
            "to": "me", "date": "2025-10-20T00:00:00+00:00", "snippet": "s",
            "body": "AI internship, apply before Nov 1", "web_link": "http://x",
            "label_ids": ["INBOX"], "is_unread": True, "is_important": True,
            "has_attachment": False, "size": 10,
        }
        self.label_calls = []

    def tearDown(self):
        import config
        config.DB_PATH = self._orig_db
        classify.set_client(None)
        self.tmp.cleanup()

    def test_main_writes_priority_and_history(self):
        import run_pipeline

        mail = self.mail
        label_calls = self.label_calls

        class FakeReader:
            def __init__(self, **kwargs):
                pass

            def fetch_recent(self, count, query):
                return [mail]

            def fetch_incremental(self, since):
                return [mail], "1001"

            def get_profile(self):
                return {"historyId": "1000"}

        class FakeLabels:
            def __init__(self, **kwargs):
                pass

            def ensure_priority_labels(self):
                return {p: p for p in ("P0", "P1", "P2", "P3", "P4")}

            def apply_priority(self, message_id, priority):
                label_calls.append((message_id, priority))
                return True

        call = SimpleNamespace(
            type="function_call", name="apply_priority_label",
            arguments=json.dumps({"priority": "P1", "reason": "实习机会"}),
        )
        classify.set_client(_fake_client(SimpleNamespace(output=[call], output_text="")))

        original_reader, original_labels = run_pipeline.GmailReader, run_pipeline.GmailLabels
        run_pipeline.GmailReader, run_pipeline.GmailLabels = FakeReader, FakeLabels
        try:
            code = run_pipeline.main(["--limit", "2", "--non-interactive"])
        finally:
            run_pipeline.GmailReader, run_pipeline.GmailLabels = original_reader, original_labels

        self.assertEqual(code, 0)
        self.assertEqual(label_calls, [("p1", "P1")])

        conn = store.init_db(self.db)
        row = conn.execute("SELECT priority, labeled, reason FROM emails WHERE id='p1'").fetchone()
        self.assertEqual(row["priority"], "P1")
        self.assertEqual(row["labeled"], 1)
        self.assertIn("实习", row["reason"])
        self.assertEqual(store.get_state(conn, "gmail_history_id"), "1000")
        conn.close()

    def test_dry_run_classifies_without_writing(self):
        import run_pipeline

        mail = self.mail

        class FakeReader:
            def __init__(self, **kwargs):
                pass

            def fetch_recent(self, count, query):
                return [mail]

            def get_profile(self):
                return {"historyId": "1000"}

        call = SimpleNamespace(
            type="function_call", name="apply_priority_label",
            arguments=json.dumps({"priority": "P1", "reason": "实习机会"}),
        )
        classify.set_client(_fake_client(SimpleNamespace(output=[call], output_text="")))

        original_reader = run_pipeline.GmailReader
        run_pipeline.GmailReader = FakeReader
        try:
            code = run_pipeline.main(["--limit", "1", "--dry-run", "--non-interactive"])
        finally:
            run_pipeline.GmailReader = original_reader

        self.assertEqual(code, 0)
        conn = store.init_db(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0], 0)
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
