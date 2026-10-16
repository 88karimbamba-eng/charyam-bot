"""Tests SIMULÉS : diagnostic du webhook (état Telegram, raisons des messages ignorés)."""

import logging
import unittest
from unittest import mock

import test_charyam_telegram as base

bot = base.bot


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(bot.redact(record.getMessage()))


class WebhookDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        base.TELEGRAM_CALLS.clear()
        bot.chat_memory.clear()
        self.client = bot.app.test_client()
        self.capture = LogCapture()
        bot.logger.addHandler(self.capture)
        self.addCleanup(bot.logger.removeHandler, self.capture)

    def logs(self):
        return "\n".join(self.capture.lines)

    def post(self, payload):
        return self.client.post("/webhook", json=payload)

    # -- état du webhook vu par Telegram ------------------------------------
    def test_etat_du_webhook_journalise_sans_secret(self):
        info = {"ok": True, "result": {
            "url": bot.WEBHOOK_URL, "pending_update_count": 3,
            "last_error_date": 1760000000, "last_error_message": "Wrong response from the webhook: 403 Forbidden",
            "allowed_updates": ["message"],
        }}
        with mock.patch.object(bot, "tg_call", return_value=info):
            bot._log_webhook_status()
        text = self.logs()
        self.assertIn("url_conforme=True", text)
        self.assertIn("en_attente=3", text)
        self.assertIn("403 Forbidden", text)
        self.assertIn("2025-10-09", text)                 # date convertie, lisible
        self.assertNotIn(bot.TELEGRAM_TOKEN, text)

    def test_url_differente_est_signalee(self):
        info = {"ok": True, "result": {"url": "https://autre.example/webhook", "pending_update_count": 0}}
        with mock.patch.object(bot, "tg_call", return_value=info):
            bot._log_webhook_status()
        self.assertIn("url_conforme=False", self.logs())
        self.assertIn("derniere_erreur=jamais", self.logs())

    def test_echec_du_diagnostic_ne_casse_rien(self):
        with mock.patch.object(bot, "tg_call", side_effect=RuntimeError("réseau")):
            bot._log_webhook_status()                      # ne doit pas lever
        self.assertIn("indisponible", self.logs())
        with mock.patch.object(bot, "tg_call", return_value={"ok": False, "description": "Unauthorized"}):
            bot._log_webhook_status()
        self.assertIn("illisible", self.logs())

    def _configure_with(self, registered_url):
        """Lance _configure_webhook avec un faux Telegram dont le webhook enregistré est registered_url"""
        calls = []

        def fake(method, payload=None):
            calls.append(method)
            if method == "getWebhookInfo":
                return {"ok": True, "result": {"url": registered_url if len(calls) == 1 else bot.WEBHOOK_URL}}
            return {"ok": True}

        with mock.patch.object(bot, "tg_call", side_effect=fake), mock.patch.object(bot, "WEBHOOK_SECRET", ""):
            bot._configure_webhook()
        return calls

    def test_webhook_a_reconfigurer_puis_diagnostic(self):
        calls = self._configure_with("https://ancienne-url.example/webhook")
        self.assertEqual(calls, ["getWebhookInfo", "setWebhook", "getWebhookInfo"])

    def test_webhook_deja_conforme_puis_diagnostic(self):
        calls = self._configure_with(bot.WEBHOOK_URL)
        self.assertEqual(calls, ["getWebhookInfo", "getWebhookInfo"])   # pas de setWebhook inutile

    def test_avec_secret_le_webhook_est_toujours_reenregistre_puis_diagnostic(self):
        calls = []
        with mock.patch.object(bot, "tg_call", side_effect=lambda m, p=None: calls.append(m) or {"ok": True, "result": {"url": bot.WEBHOOK_URL}}), \
             mock.patch.object(bot, "WEBHOOK_SECRET", "secret_de_test-123"):
            bot._configure_webhook()
        self.assertEqual(calls, ["setWebhook", "getWebhookInfo"])
        bot._secret_active = False                              # remet l'état global comme avant le test

    # -- messages ignorés : plus aucun silence ------------------------------
    def test_corps_non_json(self):
        response = self.client.post("/webhook", data="pas du json", content_type="text/plain")
        self.assertEqual(response.status_code, 200)
        self.assertIn("non JSON", self.logs())

    def test_mise_a_jour_sans_message(self):
        self.post({"update_id": 9001, "edited_message": {"text": "secret-contenu"}})
        self.assertIn("pas de message", self.logs())
        self.assertIn("edited_message", self.logs())       # nom du champ
        self.assertNotIn("secret-contenu", self.logs())    # jamais le contenu

    def test_message_sans_texte_photo(self):
        self.post({"update_id": 9002, "message": {"message_id": 1, "from": {"id": bot.USER_ID}, "chat": {"id": 42}, "photo": [{"file_id": "x"}]}})
        self.assertIn("sans texte", self.logs())
        self.assertIn("photo", self.logs())

    def test_message_d_espaces_n_appelle_plus_claude(self):
        before = len(base.CLAUDE_CALLS)
        self.post(base._update("     "))
        self.assertEqual(len(base.CLAUDE_CALLS), before)
        self.assertIn("sans texte", self.logs())

    def test_commande_non_geree(self):
        self.post(base._update("/pas_une_commande secret-argument"))
        self.assertIn("commande non gérée", self.logs())
        self.assertNotIn("secret-argument", self.logs())

    def test_non_regression_message_normal(self):
        before = len(base.CLAUDE_CALLS)
        self.post(base._update("bonjour"))
        self.assertEqual(len(base.CLAUDE_CALLS), before + 1)


if __name__ == "__main__":
    unittest.main()
