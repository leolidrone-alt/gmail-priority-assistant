"""settings.py 的离线测试：邮箱/Key 校验、.env 原地更新。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import settings  # noqa: E402


class EmailTest(unittest.TestCase):
    def test_accepts_normal_addresses(self):
        for raw in (
            "alice@gmail.com",
            "a.b+tag@ust.hk",
            "x_y-z@sub.domain.co.uk",
            "  Alice@Gmail.com  ",
        ):
            value, err = settings.validate_email(raw)
            self.assertIsNone(err, raw)
            self.assertEqual(value, raw.strip().lower())

    def test_rejects_broken_addresses(self):
        for raw in ("", "   ", "abc", "a@@b.com", "a b@c.com", ".a@b.com",
                    "a.@b.com", "a..b@c.com", "a@b", "a@.com", "a@b..com"):
            value, err = settings.validate_email(raw)
            self.assertIsNone(value, raw)
            self.assertIsNotNone(err, raw)

    def test_rejects_numeric_tld(self):
        _, err = settings.validate_email("a@b.123")
        self.assertIsNotNone(err)

    def test_gmail_detection(self):
        self.assertTrue(settings.is_gmail("a@gmail.com"))
        self.assertTrue(settings.is_gmail("a@googlemail.com"))
        self.assertFalse(settings.is_gmail("a@ust.hk"))


class ApiKeyTest(unittest.TestCase):
    def test_accepts_deepseek_shape(self):
        key = "sk-" + "a" * 32
        value, err = settings.validate_api_key(key)
        self.assertEqual(value, key)
        self.assertIsNone(err)

    def test_rejects_bad_shapes(self):
        for raw in ("", "   ", "abc", "sk-", "sk-short", "sk-" + "a" * 20 + " b"):
            _, err = settings.validate_api_key(raw)
            self.assertIsNotNone(err, repr(raw))

    def test_mask(self):
        self.assertEqual(settings.mask_key(""), "not set")
        self.assertEqual(settings.mask_key("sk-1234567890abcd")[-4:], "abcd")
        self.assertNotIn("1234", settings.mask_key("sk-1234567890abcd"))


class EnvFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".env"
        self.path.write_text(
            "# 注释保留\nDEEPSEEK_API_KEY=old\nDEEPSEEK_MODEL=deepseek-flash\n", encoding="utf-8"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_updates_in_place_and_appends(self):
        settings.upsert_env(self.path, {"DEEPSEEK_API_KEY": "sk-new", "GMAIL_ADDRESS": "a@gmail.com"})
        text = self.path.read_text(encoding="utf-8")

        self.assertIn("# 注释保留", text)
        self.assertIn("DEEPSEEK_API_KEY=sk-new", text)
        self.assertNotIn("DEEPSEEK_API_KEY=old", text)
        self.assertIn("GMAIL_ADDRESS=a@gmail.com", text)
        # 顺序：已有键原位，新键追加到末尾
        self.assertLess(text.index("DEEPSEEK_API_KEY"), text.index("DEEPSEEK_MODEL"))
        self.assertLess(text.index("DEEPSEEK_MODEL"), text.index("GMAIL_ADDRESS"))

    def test_idempotent(self):
        settings.upsert_env(self.path, {"DEEPSEEK_API_KEY": "sk-new"})
        first = self.path.read_text(encoding="utf-8")
        settings.upsert_env(self.path, {"DEEPSEEK_API_KEY": "sk-new"})
        self.assertEqual(first, self.path.read_text(encoding="utf-8"))

    def test_creates_missing_file(self):
        target = Path(self.tmp.name) / "new" / ".env"
        settings.upsert_env(target, {"GMAIL_ADDRESS": "a@gmail.com"})
        self.assertEqual(target.read_text(encoding="utf-8").strip(), "GMAIL_ADDRESS=a@gmail.com")


class AccountCheckTest(unittest.TestCase):
    """用注入的 profile_getter 模拟，不联网。"""

    def test_match(self):
        result = settings.check_gmail_account(
            "Alice@Gmail.com", profile_getter=lambda: "alice@gmail.com"
        )
        self.assertEqual(result["state"], "match")
        self.assertEqual(result["actual"], "alice@gmail.com")

    def test_mismatch(self):
        result = settings.check_gmail_account(
            "bob@gmail.com", profile_getter=lambda: "alice@gmail.com"
        )
        self.assertEqual(result["state"], "mismatch")
        self.assertIn("alice", result["message"])

    def test_not_authorized(self):
        from auth import AuthError

        def boom():
            raise AuthError("没有可用的 Gmail 凭据")

        result = settings.check_gmail_account("a@gmail.com", profile_getter=boom)
        self.assertEqual(result["state"], "not_authorized")
        self.assertIn("Gmail 凭据", result["message"])

    def test_error(self):
        def boom():
            raise RuntimeError("network down")

        result = settings.check_gmail_account("a@gmail.com", profile_getter=boom)
        self.assertEqual(result["state"], "error")
        self.assertIn("network down", result["message"])

    def test_empty_profile_is_error(self):
        result = settings.check_gmail_account("a@gmail.com", profile_getter=lambda: "")
        self.assertEqual(result["state"], "error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
