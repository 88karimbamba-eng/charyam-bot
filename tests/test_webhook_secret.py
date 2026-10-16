"""Tests SIMULÉS : le secret du webhook Telegram est exigé avant tout traitement.

Une requête sans en-tête, avec un mauvais secret ou avec un corps invalide doit recevoir 403 AVANT
toute lecture du message : aucun appel à Claude, aucun envoi Telegram, aucune mémoire modifiée.
Ces tests simulent Telegram et Claude : ils ne prouvent rien sur la production (voir les tests réels).
"""

import logging
import unittest
from unittest import mock

import test_charyam_telegram as base
from tests.test_router import Scripted, reply, text

bot = base.bot
SECRET = "secret_de_test-ABC123_xyz"
HEADER = "X-Telegram-Bot-Api-Secret-Token"


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class WebhookSecretTest(unittest.TestCase):
    def setUp(self):
        base.TELEGRAM_CALLS.clear()
        bot.chat_memory.clear()
        self.client = bot.app.test_client()
        self.capture = LogCapture()
        bot.logger.addHandler(self.capture)
        self.addCleanup(bot.logger.removeHandler, self.capture)
        for patcher in (mock.patch.object(bot, "WEBHOOK_SECRET", SECRET),
                        mock.patch.object(bot, "_secret_active", False)):   # enregistrement chez Telegram non confirmé
            patcher.start()
            self.addCleanup(patcher.stop)

    def post_owner(self, headers=None, script=None):
        script = script or Scripted(reply(text("réponse de Claude")))
        with mock.patch.object(bot.client.messages, "create", script):
            response = self.client.post("/webhook", json=base._update("Bonjour"), headers=headers or {})
        return response, script

    def assert_aucun_effet(self, script):
        self.assertEqual(script.calls, [])                                            # Claude jamais appelé
        self.assertEqual([m for m, _ in base.TELEGRAM_CALLS], [])                     # rien envoyé à Telegram
        self.assertEqual(bot.chat_memory, {})                                         # aucune mémoire modifiée

    def test_sans_en_tete_refuse_avant_tout_traitement(self):
        response, script = self.post_owner()
        self.assertEqual(response.status_code, 403)
        self.assert_aucun_effet(script)

    def test_mauvais_secret_refuse(self):
        for wrong in ("mauvais", "", SECRET[:-1], SECRET + "x", SECRET.upper(), "é" * 10):
            with self.subTest(wrong=wrong):
                response, script = self.post_owner({HEADER: wrong})
                self.assertEqual(response.status_code, 403)
                self.assert_aucun_effet(script)

    def test_corps_invalide_sans_secret_refuse_et_non_traite(self):
        for kwargs in ({"data": "pas du json", "content_type": "text/plain"}, {"json": {}}, {"json": [1, 2]}, {"data": ""}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.client.post("/webhook", **kwargs).status_code, 403)

    def test_bon_secret_accepte_et_claude_repond(self):
        script = Scripted(reply(text("réponse de Claude")))
        response, _ = self.post_owner({HEADER: SECRET}, script)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(script.calls), 1)

    def test_refus_meme_si_enregistrement_chez_telegram_a_echoue(self):
        # secret configuré mais _secret_active faux (setWebhook en échec) : on refuse quand même (fail-closed)
        self.assertFalse(bot._secret_active)
        response, script = self.post_owner()
        self.assertEqual(response.status_code, 403)
        self.assert_aucun_effet(script)

    def test_les_refus_ne_journalisent_jamais_le_secret(self):
        self.post_owner({HEADER: "mauvais"})
        self.post_owner()
        joined = "\n".join(self.capture.lines)
        self.assertIn("jeton secret invalide", joined)
        self.assertNotIn(SECRET, joined)

    def test_diagnostic_secret_present_sans_valeur(self):
        bot.log_secret_diagnostic()
        joined = "\n".join(self.capture.lines)
        self.assertIn("présent et non vide", joined)
        self.assertNotIn(SECRET, joined)
        self.assertNotIn(str(len(SECRET)), joined)

    def test_diagnostic_secret_absent_alerte(self):
        with mock.patch.object(bot, "WEBHOOK_SECRET", ""):
            bot.log_secret_diagnostic()
        self.assertIn("ABSENT ou vide", "\n".join(self.capture.lines))

    def test_enregistrement_reussi_active_le_secret_et_le_dit(self):
        with mock.patch.object(bot, "tg_call", lambda m, p=None: {"ok": True, "result": {"url": bot.WEBHOOK_URL}}):
            bot._configure_webhook()
        self.assertTrue(bot._secret_active)
        self.assertIn("secret enregistré chez Telegram : oui", "\n".join(self.capture.lines))
        self.assertNotIn(SECRET, "\n".join(self.capture.lines))

    def test_pages_publiques_nappellent_pas_claude(self):
        script = Scripted(reply(text("jamais")))
        with mock.patch.object(bot.client.messages, "create", script):
            self.assertEqual(self.client.get("/health").status_code, 200)
            self.assertEqual(self.client.get("/").status_code, 200)
            self.assertEqual(self.client.get("/webhook").status_code, 405)      # GET non accepté
        self.assertEqual(script.calls, [])


if __name__ == "__main__":
    unittest.main()
