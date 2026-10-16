"""Accès réservé aux testeurs et limite de messages (Telegram et Claude simulés)."""

import unittest
from unittest import mock

import access
import test_charyam_telegram as base

bot = base.bot
OWNER = bot.USER_ID


class ParseTests(unittest.TestCase):
    def test_owner_always_included_and_garbage_ignored(self):
        ids = access.parse_allowed_ids("111, 222;abc  -5 0 333", OWNER)
        self.assertEqual(ids, frozenset({OWNER, 111, 222, 333}))

    def test_empty_means_owner_only(self):
        self.assertEqual(access.parse_allowed_ids("", OWNER), frozenset({OWNER}))
        self.assertEqual(access.parse_allowed_ids(None, OWNER), frozenset({OWNER}))

    def test_is_allowed_rejects_non_integers(self):
        control = access.AccessControl(OWNER, env={"ALLOWED_USER_IDS": "111"})
        self.assertTrue(control.is_allowed(111))
        self.assertFalse(control.is_allowed("111"))
        self.assertFalse(control.is_allowed(None))
        self.assertFalse(control.is_allowed(True))


class RateTests(unittest.TestCase):
    def control(self, **env):
        return access.AccessControl(OWNER, env={"ALLOWED_USER_IDS": "111", **env})

    def test_per_minute_limit_then_recovers(self):
        control = self.control(USER_MESSAGES_PER_MINUTE="2", USER_MESSAGES_PER_DAY="100")
        self.assertEqual(control.check_rate(111, now=1000.0), (True, ""))
        self.assertEqual(control.check_rate(111, now=1010.0), (True, ""))
        self.assertEqual(control.check_rate(111, now=1020.0), (False, "minute"))
        self.assertEqual(control.check_rate(111, now=1061.0), (True, ""))

    def test_refused_message_is_not_counted(self):
        control = self.control(USER_MESSAGES_PER_MINUTE="1", USER_MESSAGES_PER_DAY="2")
        self.assertTrue(control.check_rate(111, now=1000.0)[0])
        self.assertFalse(control.check_rate(111, now=1001.0)[0])
        self.assertFalse(control.check_rate(111, now=1002.0)[0])
        self.assertTrue(control.check_rate(111, now=1100.0)[0])       # 2e message de la journée
        self.assertEqual(control.check_rate(111, now=1200.0), (False, "jour"))

    def test_daily_window_slides(self):
        control = self.control(USER_MESSAGES_PER_MINUTE="10", USER_MESSAGES_PER_DAY="1")
        self.assertTrue(control.check_rate(111, now=1000.0)[0])
        self.assertEqual(control.check_rate(111, now=1000.0 + 3600), (False, "jour"))
        self.assertTrue(control.check_rate(111, now=1000.0 + 86401)[0])

    def test_owner_is_never_limited(self):
        control = self.control(USER_MESSAGES_PER_MINUTE="1", USER_MESSAGES_PER_DAY="1")
        for index in range(50):
            self.assertTrue(control.check_rate(OWNER, now=1000.0 + index)[0])

    def test_users_are_counted_separately(self):
        control = access.AccessControl(OWNER, env={"ALLOWED_USER_IDS": "111,222", "USER_MESSAGES_PER_MINUTE": "1"})
        self.assertTrue(control.check_rate(111, now=1000.0)[0])
        self.assertTrue(control.check_rate(222, now=1000.0)[0])
        self.assertFalse(control.check_rate(111, now=1001.0)[0])

    def test_invalid_limits_fall_back_to_defaults(self):
        control = self.control(USER_MESSAGES_PER_MINUTE="abc", USER_MESSAGES_PER_DAY="-3")
        self.assertEqual(control.per_minute, access.DEFAULT_PER_MINUTE)
        self.assertEqual(control.per_day, access.DEFAULT_PER_DAY)

    def test_notices_are_throttled(self):
        control = self.control()
        self.assertTrue(control.should_notify(5, "refus", now=1000.0))
        self.assertFalse(control.should_notify(5, "refus", now=1100.0))
        self.assertTrue(control.should_notify(5, "limite-jour", now=1100.0))
        self.assertTrue(control.should_notify(5, "refus", now=1000.0 + access.NOTICE_INTERVAL_SECONDS))


class WebhookAccessTests(unittest.TestCase):
    def setUp(self):
        base.TELEGRAM_CALLS.clear()
        base.CLAUDE_CALLS.clear()
        base.CLAUDE_REPLY.update({"text": "Réponse de test", "stop_reason": "end_turn"})
        bot.chat_memory.clear()
        bot.chat_modes.clear()
        self.client = bot.app.test_client()

    def use(self, **env):
        control = access.AccessControl(OWNER, env={"ALLOWED_USER_IDS": "777", **env})
        patcher = mock.patch.object(bot, "access_control", control)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, text, user_id, chat_id=None):
        return self.client.post("/webhook", json=base._update(text, user_id=user_id, chat_id=chat_id or user_id))

    def sent(self):
        return [p["text"] for m, p in base.TELEGRAM_CALLS if m == "sendMessage"]

    def test_listed_tester_reaches_claude(self):
        self.use()
        self.post("bonjour", 777)
        self.assertEqual(len(base.CLAUDE_CALLS), 1)
        self.assertEqual(self.sent()[-1], "Réponse de test")

    def test_tester_gets_no_owner_first_name(self):
        self.use()
        self.post("bonjour", 777)
        self.assertNotIn("Karim", base.CLAUDE_CALLS[0]["system"])

    def test_stranger_never_reaches_claude_and_is_told_once(self):
        self.use()
        self.post("bonjour", 999)
        self.post("encore moi", 999)
        self.assertEqual(base.CLAUDE_CALLS, [])
        self.assertEqual(len(self.sent()), 1)
        self.assertIn("identifiant Telegram est 999", self.sent()[0])

    def test_tester_over_limit_is_stopped_before_claude(self):
        self.use(USER_MESSAGES_PER_MINUTE="2", USER_MESSAGES_PER_DAY="50")
        for _ in range(4):
            self.post("question", 777)
        self.assertEqual(len(base.CLAUDE_CALLS), 2)
        self.assertEqual(self.sent().count(access.rate_limit_text("minute")), 1)

    def test_owner_is_not_limited(self):
        self.use(USER_MESSAGES_PER_MINUTE="1", USER_MESSAGES_PER_DAY="1")
        for _ in range(4):
            self.post("question", OWNER)
        self.assertEqual(len(base.CLAUDE_CALLS), 4)


if __name__ == "__main__":
    unittest.main()
