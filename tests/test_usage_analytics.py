"""Tests SIMULÉS de la mesure d'usage PostHog : aucune requête réseau réelle n'est faite."""

import json
import unittest
from unittest import mock

import test_charyam_telegram as base
import usage_analytics
from usage_analytics import UsageAnalytics

bot = base.bot
OWNER = bot.USER_ID
FAKE_KEY = "phc_cle_fictive_pour_tests"


class SyncExecutor:
    """Exécute tout de suite, dans le même fil : rend les tests déterministes."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


class FakeHttp:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def post(self, url, json=None):
        self.calls.append((url, json))
        if self.fail:
            raise RuntimeError("réseau coupé")


def make(env=None, **kwargs):
    http = kwargs.pop("http", FakeHttp())
    analytics = UsageAnalytics(salt="secret-de-test", env=env if env is not None else {"POSTHOG_API_KEY": FAKE_KEY},
                               http=http, executor=SyncExecutor())
    return analytics, http


class ActivationTest(unittest.TestCase):
    def test_desactive_sans_cle_et_sans_reseau(self):
        analytics, http = make(env={})
        self.assertFalse(analytics.enabled)
        self.assertFalse(analytics.capture("menu_used", 123, kind="aide"))
        self.assertEqual(http.calls, [])

    def test_active_avec_cle(self):
        analytics, http = make()
        self.assertTrue(analytics.enabled)
        self.assertTrue(analytics.capture("menu_used", 123, kind="aide"))
        self.assertEqual(len(http.calls), 1)

    def test_hote_non_https_remplace_par_le_defaut(self):
        analytics, _ = make(env={"POSTHOG_API_KEY": FAKE_KEY, "POSTHOG_HOST": "http://exemple.test"})
        self.assertEqual(analytics.host, usage_analytics.DEFAULT_HOST)

    def test_hote_personnalise_https(self):
        analytics, http = make(env={"POSTHOG_API_KEY": FAKE_KEY, "POSTHOG_HOST": "https://eu.i.posthog.com/"})
        analytics.capture("menu_used", 1)
        self.assertEqual(http.calls[0][0], "https://eu.i.posthog.com" + usage_analytics.CAPTURE_PATH)


class ConfidentialiteTest(unittest.TestCase):
    def test_identifiant_pseudonymise_et_stable(self):
        analytics, _ = make()
        first = analytics.distinct_id(5608719781)
        self.assertEqual(first, analytics.distinct_id(5608719781))
        self.assertNotEqual(first, analytics.distinct_id(5608719782))
        self.assertNotIn("5608719781", first)

    def test_le_sel_change_l_identifiant(self):
        a = UsageAnalytics(salt="sel-1", env={"POSTHOG_API_KEY": FAKE_KEY})
        b = UsageAnalytics(salt="sel-2", env={"POSTHOG_API_KEY": FAKE_KEY})
        self.assertNotEqual(a.distinct_id(42), b.distinct_id(42))

    def test_proprietes_hors_liste_blanche_ignorees(self):
        analytics, http = make()
        analytics.capture("menu_used", 42, kind="aide", text="mon CV complet", first_name="Karim",
                          phone="+2250700000000", url="https://exemple.test")
        props = http.calls[0][1]["properties"]
        self.assertEqual(props["kind"], "aide")
        for forbidden in ("text", "first_name", "phone", "url"):
            self.assertNotIn(forbidden, props)

    def test_valeurs_longues_tronquees_et_types_limites(self):
        analytics, http = make()
        analytics.capture("menu_used", 42, kind="x" * 500, mode=["liste"], done=True)
        props = http.calls[0][1]["properties"]
        self.assertEqual(len(props["kind"]), usage_analytics.MAX_VALUE_CHARS)
        self.assertNotIn("mode", props)
        self.assertIs(props["done"], True)

    def test_aucun_numero_dans_la_charge_utile(self):
        analytics, http = make()
        analytics.capture("export_requested", 5608719781, format="pdf")
        self.assertNotIn("5608719781", json.dumps(http.calls[0][1]))

    def test_pas_de_fiche_personne(self):
        analytics, http = make()
        analytics.capture("menu_used", 1)
        self.assertIs(http.calls[0][1]["properties"]["$process_person_profile"], False)


class RobustesseTest(unittest.TestCase):
    def test_erreur_reseau_jamais_propagee(self):
        analytics, http = make(http=FakeHttp(fail=True))
        self.assertTrue(analytics.capture("menu_used", 1))   # l'envoi échoue en silence
        self.assertEqual(len(http.calls), 1)

    def test_file_d_attente_bornee(self):
        analytics, http = make()
        analytics._pending = usage_analytics.MAX_PENDING
        self.assertFalse(analytics.capture("menu_used", 1))
        self.assertEqual(http.calls, [])

    def test_compteur_revient_a_zero_apres_envoi(self):
        analytics, _ = make()
        analytics.capture("menu_used", 1)
        self.assertEqual(analytics._pending, 0)


class BotIntegrationTest(unittest.TestCase):
    """Le bot appelle la mesure aux bons endroits, sans y mettre de texte client."""

    def test_menu_kind_ne_renvoie_jamais_le_texte_libre(self):
        self.assertEqual(bot._menu_kind("/start"), "start")
        self.assertEqual(bot._menu_kind("/aide@CHARYAMBOUTIQUEbot"), "aide")
        self.assertEqual(bot._menu_kind(bot.BUTTON_EBOOKS), "ebooks")
        self.assertEqual(bot._menu_kind(bot.BUTTON_EMPLOI), "mode_" + bot.BUTTON_MODES[bot.BUTTON_EMPLOI])
        self.assertEqual(bot._menu_kind("Voici mon numéro 0707070707 et mon histoire"), "autre")

    def test_track_marque_le_proprietaire(self):
        with mock.patch.object(bot.usage, "capture") as capture:
            bot._track("menu_used", OWNER, kind="aide")
            bot._track("menu_used", 999, kind="aide")
        self.assertIs(capture.call_args_list[0].kwargs["is_owner"], True)
        self.assertIs(capture.call_args_list[1].kwargs["is_owner"], False)

    def test_bot_inchange_sans_cle_posthog(self):
        self.assertFalse(bot.usage.enabled)   # les tests tournent sans POSTHOG_API_KEY


if __name__ == "__main__":
    unittest.main()
