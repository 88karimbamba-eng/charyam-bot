"""Tests SIMULÉS : niveaux 1 (code) et 2 (application) du contrôle d'authentification du webhook.

Matrice : secret absent / vide / espaces / valide, en production et hors production, avec en-tête
absent / vide / incorrect / valide. Toute requête refusée : 403 ou 503, sans appel à Claude, sans envoi
Telegram, sans mémoire modifiée. Hors production sans secret, le contrôle reste désactivé (postes de test).
"""

import importlib.util
import itertools
import pathlib
import unittest
from unittest import mock

import test_charyam_telegram as base
from tests.test_router import Scripted, reply, text

bot = base.bot
SECRET = "secret_de_test-ABC123_xyz"
HEADER = "X-Telegram-Bot-Api-Secret-Token"

spec = importlib.util.spec_from_file_location("verify_webhook", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "verify_webhook.py")
verify_webhook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify_webhook)


class WebhookAuthFunctionTest(unittest.TestCase):
    """Niveau 1 : fonction pure."""

    def test_matrice_complete(self):
        for secret, production in itertools.product(("", "   ", None, SECRET), (True, False)):
            for received in ("", None, "mauvais", SECRET, SECRET + " "):
                with self.subTest(secret=secret, production=production, received=received):
                    verdict = bot.webhook_auth(received, secret, production)
                    if not (secret or "").strip():
                        self.assertEqual(verdict, "mal_configure" if production else "ouvert_dev")
                    elif received == SECRET:
                        self.assertEqual(verdict, "accepte")
                    else:
                        self.assertEqual(verdict, "refuse")

    def test_secret_vide_et_en_tete_vide_ne_sont_jamais_acceptes_en_production(self):
        self.assertEqual(bot.webhook_auth("", "", True), "mal_configure")
        self.assertEqual(bot.webhook_auth("", "   ", True), "mal_configure")


class WebhookAppTest(unittest.TestCase):
    """Niveau 2 : application Flask."""

    def setUp(self):
        base.TELEGRAM_CALLS.clear()
        bot.chat_memory.clear()
        self.client = bot.app.test_client()

    def post_owner(self, headers=None):
        script = Scripted(reply(text("réponse de Claude")))
        with mock.patch.object(bot.client.messages, "create", script):
            response = self.client.post("/webhook", json=base._update("Bonjour"), headers=headers or {})
        return response, script

    def assert_aucun_effet(self, script):
        self.assertEqual(script.calls, [])
        self.assertEqual([m for m, _ in base.TELEGRAM_CALLS], [])
        self.assertEqual(bot.chat_memory, {})

    def test_production_sans_secret_ferme_tout_en_503(self):
        for secret in ("", "   "):
            for headers in ({}, {HEADER: ""}, {HEADER: "mauvais"}, {HEADER: SECRET}):
                with self.subTest(secret=secret, headers=headers), \
                     mock.patch.object(bot, "WEBHOOK_SECRET", secret), mock.patch.object(bot, "IS_PRODUCTION", True):
                    response, script = self.post_owner(headers)
                    self.assertEqual(response.status_code, 503)
                    self.assert_aucun_effet(script)

    def test_production_avec_secret_matrice(self):
        with mock.patch.object(bot, "WEBHOOK_SECRET", SECRET), mock.patch.object(bot, "IS_PRODUCTION", True):
            for headers, code in (({}, 403), ({HEADER: ""}, 403), ({HEADER: "mauvais"}, 403)):
                with self.subTest(headers=headers):
                    response, script = self.post_owner(headers)
                    self.assertEqual(response.status_code, code)
                    self.assert_aucun_effet(script)
            response, script = self.post_owner({HEADER: SECRET})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(script.calls), 1)

    def test_hors_production_sans_secret_le_controle_reste_desactive(self):
        with mock.patch.object(bot, "WEBHOOK_SECRET", ""), mock.patch.object(bot, "IS_PRODUCTION", False):
            response, script = self.post_owner()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(script.calls), 1)

    def test_les_refus_ne_journalisent_pas_le_secret(self):
        import logging

        lines = []

        class H(logging.Handler):
            def emit(self, record):
                lines.append(record.getMessage())

        handler = H()
        bot.logger.addHandler(handler)
        self.addCleanup(bot.logger.removeHandler, handler)
        with mock.patch.object(bot, "WEBHOOK_SECRET", SECRET), mock.patch.object(bot, "IS_PRODUCTION", True):
            self.post_owner({HEADER: "mauvais"})
        with mock.patch.object(bot, "WEBHOOK_SECRET", ""), mock.patch.object(bot, "IS_PRODUCTION", True):
            self.post_owner()
        joined = "\n".join(lines)
        self.assertIn("jeton secret invalide", joined)
        self.assertIn("absent ou vide en production", joined)
        self.assertNotIn(SECRET, joined)

    def test_diagnostic_production_sans_secret_est_critique(self):
        import logging

        records = []

        class H(logging.Handler):
            def emit(self, record):
                records.append((record.levelno, record.getMessage()))

        handler = H()
        bot.logger.addHandler(handler)
        self.addCleanup(bot.logger.removeHandler, handler)
        with mock.patch.object(bot, "WEBHOOK_SECRET", ""), mock.patch.object(bot, "IS_PRODUCTION", True):
            bot.log_secret_diagnostic()
        self.assertTrue(any(level == logging.CRITICAL and "ABSENT ou vide en PRODUCTION" in msg for level, msg in records))

    def test_get_sante_reste_disponible_en_production_sans_secret(self):
        with mock.patch.object(bot, "WEBHOOK_SECRET", ""), mock.patch.object(bot, "IS_PRODUCTION", True):
            self.assertEqual(self.client.get("/health").status_code, 200)   # le déploiement doit pouvoir réussir


class VerifyScriptTest(unittest.TestCase):
    """Le script de niveau 3 détecte un contrôle inactif et un contrôle actif (sur l'application simulée)."""

    def run_script(self, secret, production, valid):
        client = bot.app.test_client()
        with mock.patch.object(bot, "WEBHOOK_SECRET", secret), mock.patch.object(bot, "IS_PRODUCTION", production):
            return verify_webhook.run_checks(
                lambda headers: client.post("/webhook", json=verify_webhook.BODY, headers=headers).status_code, valid)

    def test_controle_actif_conforme(self):
        self.assertTrue(all(ok for *_, ok in self.run_script(SECRET, True, SECRET)))

    def test_controle_inactif_detecte(self):
        results = self.run_script("", False, None)       # situation observée en production : tout passe en 200
        self.assertFalse(any(ok for *_, ok in results))

    def test_corps_du_script_est_sans_effet_meme_si_le_controle_est_inactif(self):
        script = Scripted(reply(text("jamais")))
        client = bot.app.test_client()
        with mock.patch.object(bot, "WEBHOOK_SECRET", ""), mock.patch.object(bot, "IS_PRODUCTION", False), \
             mock.patch.object(bot.client.messages, "create", script):
            client.post("/webhook", json=verify_webhook.BODY)
        self.assertEqual(script.calls, [])
        self.assertEqual([m for m, _ in base.TELEGRAM_CALLS], [])


if __name__ == "__main__":
    unittest.main()
