"""Tests SIMULÉS de l'accès ouvert : limites par personne, plafond global, outils restreints."""

import unittest
from unittest import mock

import test_charyam_telegram as base
import access
from integrations.tool_router import RestrictedRouter, ToolRouter

bot = base.bot
OWNER = bot.USER_ID


def control(**env):
    return access.AccessControl(OWNER, env={"ALLOWED_USER_IDS": "777", **env})


class OpenAccessLogicTest(unittest.TestCase):
    def test_ouvert_par_defaut(self):
        c = control()
        self.assertTrue(c.open_access)
        self.assertTrue(c.is_allowed(123456))
        self.assertFalse(c.is_listed(123456))
        self.assertTrue(c.is_listed(777))

    def test_ferme_avec_open_access_0(self):
        for value in ("0", "false", "non", "No"):
            c = control(OPEN_ACCESS=value)
            self.assertFalse(c.is_allowed(123456), value)
            self.assertTrue(c.is_allowed(777))
            self.assertTrue(c.is_allowed(OWNER))

    def test_identifiants_invalides_refuses(self):
        c = control()
        for bad in (None, "12", True, 0, -5, 1.5):
            self.assertFalse(c.is_allowed(bad), bad)

    def test_plafond_global_pour_les_inconnus_seulement(self):
        c = control(OPEN_DAILY_CAP="3", USER_MESSAGES_PER_MINUTE="100", USER_MESSAGES_PER_DAY="100")
        results = [c.check_rate(1000 + i, now=5000.0 + i)[0] for i in range(5)]
        self.assertEqual(results, [True, True, True, False, False])
        self.assertEqual(c.check_rate(1000, now=5010.0), (False, "plafond"))
        # propriétaire et personnes listées ne sont pas concernés par le plafond
        self.assertTrue(c.check_rate(OWNER, now=5010.0)[0])
        self.assertTrue(c.check_rate(777, now=5010.0)[0])

    def test_plafond_remis_a_zero_le_lendemain(self):
        c = control(OPEN_DAILY_CAP="1", USER_MESSAGES_PER_MINUTE="100", USER_MESSAGES_PER_DAY="100")
        self.assertTrue(c.check_rate(5, now=86400.0 * 10)[0])
        self.assertFalse(c.check_rate(6, now=86400.0 * 10 + 5)[0])
        self.assertTrue(c.check_rate(6, now=86400.0 * 11 + 5)[0])

    def test_limite_par_personne_toujours_appliquee(self):
        c = control(USER_MESSAGES_PER_MINUTE="1", OPEN_DAILY_CAP="100")
        self.assertTrue(c.check_rate(9, now=1000.0)[0])
        self.assertEqual(c.check_rate(9, now=1001.0), (False, "minute"))

    def test_message_du_plafond(self):
        self.assertIn("sollicité", access.rate_limit_text("plafond"))


class RestrictedRouterTest(unittest.TestCase):
    ENV = {"PERPLEXITY_API_KEY": "pplx-fake"}

    def test_seul_lire_page_web_est_expose(self):
        router = RestrictedRouter(ToolRouter(env=self.ENV), frozenset({"lire_page_web"}))
        names = [d["name"] for d in router.definitions()]
        self.assertEqual(names, ["lire_page_web"])
        self.assertTrue(router.enabled())

    def test_outil_hors_liste_refuse_meme_si_demande(self):
        router = RestrictedRouter(ToolRouter(env=self.ENV), frozenset({"lire_page_web"}))
        result = router.execute("recherche_web", {"requete": "x"})
        self.assertFalse(result.ok)
        result = router.execute("action_application", {"outil": "X", "arguments": {}})
        self.assertFalse(result.ok)


class OpenAccessWebhookTest(unittest.TestCase):
    def setUp(self):
        base.TELEGRAM_CALLS.clear()
        base.CLAUDE_CALLS.clear()
        base.CLAUDE_REPLY.update({"text": "Réponse de test", "stop_reason": "end_turn"})
        bot.chat_memory.clear()
        bot.chat_modes.clear()
        self.client = bot.app.test_client()
        patcher = mock.patch.object(bot, "access_control", control())
        patcher.start()
        self.addCleanup(patcher.stop)

    def sent(self):
        return [p["text"] for m, p in base.TELEGRAM_CALLS if m == "sendMessage"]

    def test_inconnu_atteint_claude_sans_prenom_du_proprietaire(self):
        self.client.post("/webhook", json=base._update("Une question", user_id=424242, chat_id=424242))
        self.assertEqual(len(base.CLAUDE_CALLS), 1)
        self.assertNotIn("Karim", base.CLAUDE_CALLS[0]["system"])
        self.assertEqual(self.sent()[-1], "Réponse de test")

    def test_inconnu_ne_recoit_aucun_outil_sauf_lecture_de_page(self):
        full = ToolRouter(env={"PERPLEXITY_API_KEY": "pplx-fake"})
        public = RestrictedRouter(full, frozenset({"lire_page_web"}))
        with mock.patch.object(bot, "ROUTER", full), mock.patch.object(bot, "PUBLIC_ROUTER", public):
            self.client.post("/webhook", json=base._update("Une question", user_id=424242, chat_id=424242))
        names = [t["name"] for t in base.CLAUDE_CALLS[-1].get("tools", [])]
        self.assertEqual(names, ["lire_page_web"])

    def test_inconnu_ne_peut_pas_valider_un_paiement(self):
        self.client.post("/webhook", json=base._update("/valider 424242", user_id=424242, chat_id=424242))
        self.assertFalse(bot.payment_gate.can_export(424242, 424242))


if __name__ == "__main__":
    unittest.main()
