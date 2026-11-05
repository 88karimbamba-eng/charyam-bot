"""Tests SIMULÉS de la mesure d'usage PostHog : aucune requête réseau réelle n'est faite."""

import hashlib
import json
import logging
import unittest
from unittest import mock

import test_charyam_telegram as base
import usage_analytics
from usage_analytics import UsageAnalytics

bot = base.bot
OWNER = bot.USER_ID
FAKE_KEY = "phc_cle_fictive_pour_tests"
FAKE_SALT = "sel-de-test-independant-du-jeton-telegram"
ENV = {"POSTHOG_API_KEY": FAKE_KEY, "POSTHOG_SALT": FAKE_SALT}
LOGGER_NAME = "charyam.analytics"


class SyncExecutor:
    """Exécute tout de suite, dans le même fil : rend les tests déterministes."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


class FakeResponse:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}


class FakeHttp:
    """Répond avec les statuts donnés, dans l'ordre (le dernier se répète). Un statut « exc » lève une erreur réseau."""

    def __init__(self, *statuses, headers=None):
        self.statuses = list(statuses) or [200]
        self.headers = headers or {}
        self.calls = []

    def post(self, url, json=None):
        self.calls.append((url, json))
        status = self.statuses[min(len(self.calls), len(self.statuses)) - 1]
        if status == "exc":
            raise RuntimeError("réseau coupé : " + url)   # le message contient l'URL, qu'on ne doit pas journaliser
        return FakeResponse(status, self.headers)


def make(*statuses, env=None, headers=None, clock=None):
    http = FakeHttp(*statuses, headers=headers)
    sleeps = []
    analytics = UsageAnalytics(env=ENV if env is None else env, http=http, executor=SyncExecutor(),
                               sleep=sleeps.append, clock=clock or (lambda: 0.0))
    return analytics, http, sleeps


class ActivationTest(unittest.TestCase):
    def test_desactive_sans_cle_et_sans_reseau(self):
        analytics, http, _ = make(env={})
        self.assertFalse(analytics.enabled)
        self.assertFalse(analytics.capture("menu_used", 123, kind="aide"))
        self.assertEqual(http.calls, [])

    def test_cle_sans_sel_desactive_et_previent_une_fois(self):
        with self.assertLogs(LOGGER_NAME, level="WARNING") as logs:
            analytics, http, _ = make(env={"POSTHOG_API_KEY": FAKE_KEY})
        self.assertFalse(analytics.enabled)
        self.assertFalse(analytics.capture("menu_used", 123))
        self.assertEqual(http.calls, [])
        self.assertIn("POSTHOG_SALT", logs.output[0])
        self.assertNotIn(FAKE_KEY, logs.output[0])

    def test_sel_trop_court_refuse(self):
        analytics, http, _ = make(env={"POSTHOG_API_KEY": FAKE_KEY, "POSTHOG_SALT": "court"})
        self.assertFalse(analytics.enabled)
        self.assertFalse(analytics.capture("menu_used", 123))
        self.assertEqual(http.calls, [])

    def test_aucun_repli_sur_le_jeton_telegram(self):
        # Même avec un jeton Telegram présent dans l'environnement, sans POSTHOG_SALT la mesure reste désactivée
        env = {"POSTHOG_API_KEY": FAKE_KEY, "TELEGRAM_BOT_TOKEN": "123456:jeton-telegram-assez-long-pour-servir-de-sel"}
        analytics, http, _ = make(env=env)
        self.assertFalse(analytics.enabled)
        self.assertFalse(analytics.capture("menu_used", 123))
        self.assertEqual(http.calls, [])

    def test_active_avec_cle_et_sel(self):
        analytics, http, _ = make(200)
        self.assertTrue(analytics.enabled)
        self.assertTrue(analytics.capture("menu_used", 123, kind="aide"))
        self.assertEqual(len(http.calls), 1)

    def test_hote_non_https_remplace_par_le_defaut(self):
        analytics, _, _ = make(env={**ENV, "POSTHOG_HOST": "http://exemple.test"})
        self.assertEqual(analytics.host, usage_analytics.DEFAULT_HOST)

    def test_hote_personnalise_https(self):
        analytics, http, _ = make(env={**ENV, "POSTHOG_HOST": "https://eu.i.posthog.com/"})
        analytics.capture("menu_used", 1)
        self.assertEqual(http.calls[0][0], "https://eu.i.posthog.com" + usage_analytics.CAPTURE_PATH)


class ReponsesHttpTest(unittest.TestCase):
    """200, 400, 401, 429, 500 : accepté, refus définitif ou nouvelle tentative limitée."""

    def test_200_accepte_sans_nouvelle_tentative(self):
        analytics, http, sleeps = make(200)
        self.assertEqual(analytics._send(analytics.build_payload("menu_used", 1)), "accepted")
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(sleeps, [])

    def test_400_refus_definitif_sans_nouvelle_tentative(self):
        analytics, http, sleeps = make(400)
        analytics.capture("menu_used", 1)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(sleeps, [])
        stats = analytics.snapshot()
        self.assertEqual((stats["accepted"], stats["rejected"], stats["failed"], stats["retried"]), (0, 1, 0, 0))
        self.assertFalse(analytics.suspended)       # un 400 ne suspend pas la mesure (ce n'est pas la clé)

    def test_401_refus_definitif_et_suspension(self):
        analytics, http, sleeps = make(401)
        analytics.capture("menu_used", 1)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(analytics.snapshot()["rejected"], 1)
        self.assertTrue(analytics.suspended)
        # Clé refusée : plus aucune requête jusqu'au redémarrage
        self.assertFalse(analytics.capture("menu_used", 2))
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(analytics.snapshot()["dropped"], 1)

    def test_403_traite_comme_401(self):
        analytics, http, _ = make(403)
        analytics.capture("menu_used", 1)
        self.assertEqual(len(http.calls), 1)
        self.assertTrue(analytics.suspended)

    def test_429_puis_200_reessaie_puis_accepte(self):
        analytics, http, sleeps = make(429, 200)
        analytics.capture("menu_used", 1)
        self.assertEqual(len(http.calls), 2)
        self.assertEqual(sleeps, [usage_analytics.BACKOFF_SECONDS[0]])
        stats = analytics.snapshot()
        self.assertEqual((stats["accepted"], stats["retried"], stats["failed"]), (1, 1, 0))

    def test_429_persistant_echoue_apres_les_tentatives_limitees(self):
        analytics, http, sleeps = make(429)
        analytics.capture("menu_used", 1)
        self.assertEqual(len(http.calls), usage_analytics.MAX_ATTEMPTS)
        self.assertEqual(len(sleeps), usage_analytics.MAX_ATTEMPTS - 1)
        stats = analytics.snapshot()
        self.assertEqual((stats["accepted"], stats["failed"], stats["rejected"]), (0, 1, 0))
        self.assertFalse(analytics.suspended)

    def test_retry_after_respecte_mais_plafonne(self):
        analytics, _, sleeps = make(429, 200, headers={"Retry-After": "3"})
        analytics.capture("menu_used", 1)
        self.assertEqual(sleeps, [3.0])
        analytics, _, sleeps = make(429, 200, headers={"Retry-After": "9999"})
        analytics.capture("menu_used", 1)
        self.assertEqual(sleeps, [usage_analytics.MAX_RETRY_AFTER_SECONDS])
        analytics, _, sleeps = make(429, 200, headers={"Retry-After": "abc"})
        analytics.capture("menu_used", 1)
        self.assertEqual(sleeps, [usage_analytics.BACKOFF_SECONDS[0]])

    def test_500_puis_200_reessaie_puis_accepte(self):
        analytics, http, _ = make(500, 200)
        analytics.capture("menu_used", 1)
        self.assertEqual(len(http.calls), 2)
        self.assertEqual(analytics.snapshot()["accepted"], 1)

    def test_500_persistant_echoue_apres_les_tentatives_limitees(self):
        analytics, http, sleeps = make(500)
        analytics.capture("menu_used", 1)
        self.assertEqual(len(http.calls), usage_analytics.MAX_ATTEMPTS)
        self.assertEqual(analytics.snapshot()["failed"], 1)
        self.assertEqual(analytics._pending, 0)

    def test_erreur_reseau_reessayee_puis_echec(self):
        analytics, http, _ = make("exc")
        self.assertTrue(analytics.capture("menu_used", 1))
        self.assertEqual(len(http.calls), usage_analytics.MAX_ATTEMPTS)
        self.assertEqual(analytics.snapshot()["failed"], 1)

    def test_mis_en_file_n_est_pas_accepte(self):
        """capture() True = mis en file ; l'acceptation réelle est dans snapshot()."""
        analytics, _, _ = make(400)
        self.assertTrue(analytics.capture("menu_used", 1))
        stats = analytics.snapshot()
        self.assertEqual((stats["queued"], stats["accepted"], stats["rejected"]), (1, 0, 1))


class JournalTest(unittest.TestCase):
    def test_erreur_http_journalisee_sans_secret_ni_donnee(self):
        analytics, _, _ = make(401)
        with self.assertLogs(LOGGER_NAME, level="WARNING") as logs:
            analytics.capture("export_requested", 5608719781, format="pdf")
        text = "\n".join(logs.output)
        self.assertIn("401", text)
        self.assertIn("export_requested", text)
        for secret in (FAKE_KEY, FAKE_SALT, "5608719781", analytics.distinct_id(5608719781), "pdf", "posthog.com"):
            self.assertNotIn(secret, text)

    def test_erreur_reseau_journalisee_sans_url_ni_cle(self):
        analytics, _, _ = make("exc")
        with self.assertLogs(LOGGER_NAME, level="DEBUG") as logs:
            analytics.capture("menu_used", 42, kind="aide")
        text = "\n".join(logs.output)
        self.assertIn("erreur réseau", text)
        for secret in (FAKE_KEY, FAKE_SALT, "posthog.com", "réseau coupé", "https://"):
            self.assertNotIn(secret, text)

    def test_meme_echec_pas_repete_en_warning(self):
        now = [0.0]
        analytics, _, _ = make(500, clock=lambda: now[0])
        with self.assertLogs(LOGGER_NAME, level="DEBUG") as logs:
            analytics.capture("menu_used", 1)
            analytics.capture("menu_used", 2)           # même échec, même minute : seulement en DEBUG
            now[0] = usage_analytics.LOG_INTERVAL_SECONDS + 1
            analytics.capture("menu_used", 3)           # l'intervalle est passé : de nouveau en WARNING
        warnings = [r for r in logs.records if r.levelno == logging.WARNING]
        self.assertEqual(len(warnings), 2)
        self.assertEqual(analytics.snapshot()["failed"], 3)    # les compteurs, eux, comptent tout


class ConfidentialiteTest(unittest.TestCase):
    IDS = (OWNER, 987654321, 123456789012, 1000000001)

    def test_identifiant_pseudonymise_et_stable(self):
        analytics, _, _ = make()
        first = analytics.distinct_id(OWNER)
        self.assertEqual(first, analytics.distinct_id(OWNER))
        self.assertNotEqual(first, analytics.distinct_id(OWNER + 1))
        self.assertRegex(first, r"^u_[0-9a-f]{16}$")

    def test_hmac_avec_sel_et_non_un_simple_hachage(self):
        analytics, _, _ = make()
        unsalted = "u_" + hashlib.sha256(str(OWNER).encode()).hexdigest()[:16]
        self.assertNotEqual(analytics.distinct_id(OWNER), unsalted)
        other = UsageAnalytics(env={**ENV, "POSTHOG_SALT": "un-autre-sel-tout-aussi-long-1234"})
        self.assertNotEqual(analytics.distinct_id(OWNER), other.distinct_id(OWNER))

    def test_aucun_identifiant_en_clair_dans_les_charges_utiles(self):
        analytics, http, _ = make(200)
        for user_id in self.IDS:
            analytics.capture("export_requested", user_id, format="pdf", is_owner=False)
            analytics.capture("menu_used", user_id, kind="aide")
        self.assertEqual(len(http.calls), 2 * len(self.IDS))
        for _, payload in http.calls:
            visible = json.dumps({k: v for k, v in payload.items() if k != "distinct_id"}, ensure_ascii=False)
            for user_id in self.IDS:
                self.assertNotIn(str(user_id), visible)
            self.assertRegex(payload["distinct_id"], r"^u_[0-9a-f]{16}$")
            self.assertNotIn(FAKE_SALT, json.dumps(payload))

    def test_identifiant_passe_en_propriete_est_bloque(self):
        analytics, http, _ = make(200)
        self.assertFalse(analytics.capture("menu_used", OWNER, kind=str(OWNER)))
        self.assertEqual(http.calls, [])
        self.assertEqual(analytics.snapshot()["dropped"], 1)
        self.assertEqual(analytics._pending, 0)

    def test_proprietes_hors_liste_blanche_ignorees(self):
        analytics, http, _ = make(200)
        analytics.capture("menu_used", 42, kind="aide", text="mon CV complet", first_name="Karim",
                          phone="+2250700000000", url="https://exemple.test", chat_id=777)
        props = http.calls[0][1]["properties"]
        self.assertEqual(props["kind"], "aide")
        for forbidden in ("text", "first_name", "phone", "url", "chat_id"):
            self.assertNotIn(forbidden, props)

    def test_valeurs_longues_tronquees_et_types_limites(self):
        analytics, http, _ = make(200)
        analytics.capture("menu_used", 42, kind="x" * 500, mode=["liste"], done=True)
        props = http.calls[0][1]["properties"]
        self.assertEqual(len(props["kind"]), usage_analytics.MAX_VALUE_CHARS)
        self.assertNotIn("mode", props)
        self.assertIs(props["done"], True)

    def test_pas_de_fiche_personne(self):
        analytics, http, _ = make(200)
        analytics.capture("menu_used", 1)
        self.assertIs(http.calls[0][1]["properties"]["$process_person_profile"], False)


class EvenementsTest(unittest.TestCase):
    def test_liste_fermee_des_evenements(self):
        self.assertEqual(usage_analytics.EVENT_NAMES, {
            "menu_used", "cv_questionnaire_reply", "ai_reply_sent", "export_requested", "payment_requested",
        })

    def test_paiement_confirme_et_livraison_non_mesures(self):
        """Pas de preuve fiable de paiement/livraison dans le code : ces événements sont refusés."""
        analytics, http, _ = make(200)
        for event in ("payment_confirmed", "delivery_completed"):
            self.assertNotIn(event, usage_analytics.EVENT_NAMES)
            self.assertFalse(analytics.capture(event, 1))
        self.assertEqual(http.calls, [])
        self.assertEqual(analytics.snapshot()["dropped"], 2)

    def test_le_bot_n_emet_pas_ces_evenements(self):
        source = open(bot.__file__, encoding="utf-8").read()
        self.assertNotIn("payment_confirmed", source)
        self.assertNotIn("delivery_completed", source)

    def test_file_d_attente_bornee(self):
        analytics, http, _ = make(200)
        analytics._pending = usage_analytics.MAX_PENDING
        self.assertFalse(analytics.capture("menu_used", 1))
        self.assertEqual(http.calls, [])

    def test_compteur_revient_a_zero_apres_envoi(self):
        analytics, _, _ = make(200)
        analytics.capture("menu_used", 1)
        self.assertEqual(analytics._pending, 0)


class BotIntegrationTest(unittest.TestCase):
    """Le bot appelle la mesure aux bons endroits, sans y mettre de texte client, et sans jamais être bloqué."""

    CLIENT = 987654321
    CHAT = 424242

    def setUp(self):
        base.TELEGRAM_CALLS.clear()
        self.analytics, self.http, _ = make(200)
        patcher = mock.patch.object(bot, "usage", self.analytics)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_message(self, text, chat=None):
        bot.process_message(self.CLIENT, chat or self.CHAT, text, 7)

    def events(self):
        return [payload["event"] for _, payload in self.http.calls]

    def sent_messages(self):
        return [c for c in base.TELEGRAM_CALLS if c[0] == "sendMessage"]

    def test_menu_kind_ne_renvoie_jamais_le_texte_libre(self):
        self.assertEqual(bot._menu_kind("/start"), "start")
        self.assertEqual(bot._menu_kind("/aide@CHARYAMBOUTIQUEbot"), "aide")
        self.assertEqual(bot._menu_kind(bot.BUTTON_EBOOKS), "ebooks")
        self.assertEqual(bot._menu_kind(bot.BUTTON_EMPLOI), "mode_" + bot.BUTTON_MODES[bot.BUTTON_EMPLOI])
        self.assertEqual(bot._menu_kind("Voici mon numéro 0707070707 et mon histoire"), "autre")
        self.assertEqual(bot._menu_kind(None), "autre")      # ne lève jamais d'exception

    def test_track_marque_le_proprietaire(self):
        with mock.patch.object(bot.usage, "capture") as capture:
            bot._track("menu_used", OWNER, kind="aide")
            bot._track("menu_used", 999, kind="aide")
        self.assertIs(capture.call_args_list[0].kwargs["is_owner"], True)
        self.assertIs(capture.call_args_list[1].kwargs["is_owner"], False)

    def test_evenements_emis_sans_donnee_client(self):
        secret_text = "Rédige-moi une phrase secrète 0707070707"
        self.run_message("/aide")
        self.run_message(bot.BUTTON_WORD)           # client sans paiement validé : export refusé
        self.run_message(secret_text)
        self.assertEqual(self.events(), ["menu_used", "export_requested", "payment_requested", "ai_reply_sent"])
        self.assertEqual(self.http.calls[0][1]["properties"]["kind"], "aide")
        self.assertEqual(self.http.calls[1][1]["properties"]["format"], "docx")
        everything = json.dumps([{k: v for k, v in p.items() if k != "distinct_id"} for _, p in self.http.calls],
                                ensure_ascii=False)
        for forbidden in (str(self.CLIENT), str(self.CHAT), "secrète", "0707070707", FAKE_SALT):
            self.assertNotIn(forbidden, everything)

    def test_erreur_posthog_ne_bloque_pas_le_traitement(self):
        self.http.statuses = ["exc"]               # PostHog injoignable
        self.run_message("/aide")
        self.assertEqual(len(self.sent_messages()), 1)
        self.assertEqual(self.analytics.snapshot()["failed"], 1)

    def test_refus_posthog_ne_bloque_pas_le_traitement(self):
        self.http.statuses = [401]
        self.run_message("/aide")
        self.run_message("/menu")
        self.assertEqual(len(self.sent_messages()), 2)

    def test_exception_dans_la_mesure_ne_bloque_pas_le_traitement(self):
        with mock.patch.object(bot.usage, "capture", side_effect=RuntimeError("panne")):
            self.run_message("/aide")
            self.run_message(bot.BUTTON_WORD)
            self.run_message("Écris une phrase")
        self.assertGreaterEqual(len(self.sent_messages()), 3)

    def test_bot_inchange_sans_cle_posthog(self):
        with mock.patch.object(bot, "usage", UsageAnalytics(env={})):
            self.run_message("/aide")
        self.assertEqual(len(self.sent_messages()), 1)
        self.assertEqual(self.http.calls, [])


if __name__ == "__main__":
    unittest.main()
