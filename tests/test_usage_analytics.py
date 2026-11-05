"""Tests SIMULÉS de la mesure d'usage PostHog : aucune requête réseau réelle n'est faite."""

import datetime
import hashlib
import json
import logging
import threading
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


class ManualExecutor:
    """Garde les envois en attente jusqu'à `run_all()` : permet de tester une vraie file d'attente."""

    def __init__(self):
        self.tasks = []

    def submit(self, fn, *args, **kwargs):
        self.tasks.append((fn, args, kwargs))

    def run_all(self):
        while self.tasks:
            fn, args, kwargs = self.tasks.pop(0)
            fn(*args, **kwargs)


def make_manual(*statuses, headers=None):
    http = FakeHttp(*statuses, headers=headers)
    executor = ManualExecutor()
    analytics = UsageAnalytics(env=ENV, http=http, executor=executor, sleep=lambda s: None, clock=lambda: 0.0)
    return analytics, http, executor


class RetryAfterTest(unittest.TestCase):
    """Retry-After sous ses deux formes (secondes ou date HTTP), plafond, valeurs absurdes."""

    NOW = datetime.datetime(2026, 11, 5, 12, 0, 0, tzinfo=datetime.timezone.utc)

    def sleeps_for(self, header, statuses=(429, 200)):
        http = FakeHttp(*statuses, headers={"Retry-After": header} if header is not None else {})
        sleeps = []
        analytics = UsageAnalytics(env=ENV, http=http, executor=SyncExecutor(), sleep=sleeps.append,
                                   clock=lambda: 0.0, wall_clock=lambda: self.NOW)
        analytics.capture("menu_used", 1)
        return sleeps

    def test_secondes_entieres_et_decimales(self):
        self.assertEqual(self.sleeps_for("3"), [3.0])
        self.assertEqual(self.sleeps_for("2.5"), [2.5])

    def test_date_http_dans_le_futur(self):
        self.assertEqual(self.sleeps_for("Thu, 05 Nov 2026 12:00:03 GMT"), [3.0])

    def test_date_http_lointaine_plafonnee(self):
        self.assertEqual(self.sleeps_for("Fri, 06 Nov 2026 12:00:00 GMT"), [usage_analytics.MAX_RETRY_AFTER_SECONDS])

    def test_date_http_deja_passee_utilise_le_delai_de_base(self):
        self.assertEqual(self.sleeps_for("Thu, 05 Nov 2026 11:00:00 GMT"), [usage_analytics.BACKOFF_SECONDS[0]])

    def test_date_http_avec_fuseau_decale(self):
        # 13:00:03 +0100 = 12:00:03 UTC
        self.assertEqual(self.sleeps_for("Thu, 05 Nov 2026 13:00:03 +0100"), [3.0])

    def test_valeurs_illisibles_ou_absurdes_utilisent_le_delai_de_base(self):
        base = [usage_analytics.BACKOFF_SECONDS[0]]
        for header in ("abc", "", "   ", "nan", "NaN", "inf", "-inf", "Infinity", "-5", "0",
                       "Thu, 99 Foo 2026 99:99:99 GMT", "1e999"):
            with self.subTest(header=header):
                self.assertEqual(self.sleeps_for(header), base)

    def test_en_tete_absent(self):
        self.assertEqual(self.sleeps_for(None), [usage_analytics.BACKOFF_SECONDS[0]])

    def test_plafond_respecte_a_chaque_tentative_et_attente_totale_bornee(self):
        sleeps = self.sleeps_for("9999", statuses=(429,))
        self.assertEqual(len(sleeps), usage_analytics.MAX_ATTEMPTS - 1)
        self.assertTrue(all(delay <= usage_analytics.MAX_RETRY_AFTER_SECONDS for delay in sleeps))
        self.assertLessEqual(sum(sleeps), (usage_analytics.MAX_ATTEMPTS - 1) * usage_analytics.MAX_RETRY_AFTER_SECONDS)

    def test_delais_de_base_croissants_sans_en_tete(self):
        self.assertEqual(self.sleeps_for(None, statuses=(500,)), list(usage_analytics.BACKOFF_SECONDS))

    def test_nombre_maximal_de_tentatives(self):
        for status in (429, 500, 503, 408, "exc"):
            with self.subTest(status=status):
                analytics, http, _ = make(status)
                analytics.capture("menu_used", 1)
                self.assertEqual(len(http.calls), usage_analytics.MAX_ATTEMPTS)

    def test_delai_d_attente_http_configure(self):
        self.assertEqual(usage_analytics.REQUEST_TIMEOUT_SECONDS, 3.0)
        analytics = UsageAnalytics(env=ENV)
        client = analytics._get_http()
        try:
            self.assertEqual(client.timeout.read, 3.0)
            self.assertEqual(client.timeout.connect, 3.0)
        finally:
            client.close()

    def test_aucune_exception_ne_sort_de_la_mesure_meme_si_http_leve(self):
        class Boom:
            def post(self, *args, **kwargs):
                raise ValueError("boom " + FAKE_KEY)
        analytics = UsageAnalytics(env=ENV, http=Boom(), executor=SyncExecutor(), sleep=lambda s: None)
        self.assertTrue(analytics.capture("menu_used", 1))
        self.assertEqual(analytics.snapshot()["failed"], 1)


class FileEtSuspensionTest(unittest.TestCase):
    def test_plusieurs_evenements_en_attente_sont_envoyes_dans_l_ordre(self):
        analytics, http, executor = make_manual(200)
        for user in (1, 2, 3):
            self.assertTrue(analytics.capture("menu_used", user, kind="aide"))
        self.assertEqual(http.calls, [])
        self.assertEqual(analytics._pending, 3)
        executor.run_all()
        self.assertEqual(len(http.calls), 3)
        self.assertEqual([call[1]["distinct_id"] for call in http.calls],
                         [analytics.distinct_id(user) for user in (1, 2, 3)])
        stats = analytics.snapshot()
        self.assertEqual((stats["queued"], stats["accepted"], stats["dropped"]), (3, 3, 0))
        self.assertEqual(analytics._pending, 0)

    def test_401_bloque_les_nouveaux_envois_et_les_evenements_deja_en_attente(self):
        analytics, http, executor = make_manual(401)
        for user in (1, 2, 3):
            analytics.capture("menu_used", user)
        executor.run_all()
        self.assertEqual(len(http.calls), 1)                # le 2e et le 3e ne partent pas
        stats = analytics.snapshot()
        self.assertEqual((stats["rejected"], stats["dropped"], stats["accepted"]), (1, 2, 0))
        self.assertEqual(analytics._pending, 0)             # rien ne reste bloqué en mémoire
        self.assertFalse(analytics.capture("menu_used", 4))
        self.assertEqual(len(http.calls), 1)

    def test_403_pendant_les_nouvelles_tentatives_arrete_tout(self):
        analytics, http, _ = make(500, 403, 200)
        analytics.capture("menu_used", 1)
        self.assertEqual(len(http.calls), 2)
        self.assertTrue(analytics.suspended)

    def test_suspension_decidee_pendant_l_attente_entre_deux_tentatives(self):
        http = FakeHttp(429, 200)
        holder = {}

        def sleep(seconds):
            holder["analytics"]._auth_failed = True     # un autre envoi vient de recevoir un 401

        analytics = UsageAnalytics(env=ENV, http=http, executor=SyncExecutor(), sleep=sleep)
        holder["analytics"] = analytics
        analytics.capture("menu_used", 1)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(analytics.snapshot()["dropped"], 1)
        self.assertEqual(analytics._pending, 0)

    def test_suspension_sans_journal_bruyant_pour_les_evenements_abandonnes(self):
        analytics, _, executor = make_manual(401)
        for user in (1, 2, 3):
            analytics.capture("menu_used", user)
        with self.assertLogs(LOGGER_NAME, level="WARNING") as logs:
            executor.run_all()
        warnings = [line for line in logs.output if line.startswith("WARNING")]
        self.assertEqual(len(warnings), 1)                  # un seul avertissement pour toute la salve

    def test_file_bornee_sans_accumulation_et_reprise_apres_envoi(self):
        analytics, http, executor = make_manual(200)
        results = [analytics.capture("menu_used", n) for n in range(usage_analytics.MAX_PENDING + 25)]
        self.assertEqual(results.count(True), usage_analytics.MAX_PENDING)
        self.assertEqual(len(executor.tasks), usage_analytics.MAX_PENDING)
        stats = analytics.snapshot()
        self.assertEqual((stats["queued"], stats["dropped"]), (usage_analytics.MAX_PENDING, 25))
        executor.run_all()
        self.assertEqual(len(http.calls), usage_analytics.MAX_PENDING)
        self.assertEqual(analytics._pending, 0)
        self.assertTrue(analytics.capture("menu_used", 999))      # la file se vide et accepte de nouveau

    def test_file_reelle_avec_envois_bloques(self):
        """Vrai ThreadPoolExecutor, PostHog qui ne répond pas : au plus 2 envois simultanés, file bornée."""
        release = threading.Event()
        lock = threading.Lock()
        state = {"running": 0, "peak": 0, "calls": 0}

        class SlowHttp:
            def post(self, url, json=None):
                with lock:
                    state["running"] += 1
                    state["calls"] += 1
                    state["peak"] = max(state["peak"], state["running"])
                release.wait(5)
                with lock:
                    state["running"] -= 1
                return FakeResponse(200)

        analytics = UsageAnalytics(env=ENV, http=SlowHttp(), sleep=lambda s: None)
        try:
            accepted = sum(analytics.capture("menu_used", n) for n in range(usage_analytics.MAX_PENDING + 30))
            self.assertEqual(accepted, usage_analytics.MAX_PENDING)
            self.assertLessEqual(analytics._pending, usage_analytics.MAX_PENDING)
            release.set()
            analytics._executor.shutdown(wait=True)
            self.assertEqual(state["calls"], usage_analytics.MAX_PENDING)
            self.assertLessEqual(state["peak"], 2)
            self.assertEqual(analytics._pending, 0)
            self.assertEqual(analytics.snapshot()["accepted"], usage_analytics.MAX_PENDING)
        finally:
            release.set()

    def test_creation_unique_de_l_executeur_et_du_client_http_en_concurrence(self):
        analytics = UsageAnalytics(env=ENV)
        executors, clients = [], []
        barrier = threading.Barrier(16)

        def grab():
            barrier.wait()
            executors.append(analytics._get_executor())
            clients.append(analytics._get_http())

        threads = [threading.Thread(target=grab) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        try:
            self.assertEqual(len({id(e) for e in executors}), 1)
            self.assertEqual(len({id(c) for c in clients}), 1)
        finally:
            analytics._executor.shutdown(wait=False)
            analytics._http.close()


class AuditConfidentialiteTest(unittest.TestCase):
    """Contrôles statiques et dynamiques de ce qui peut partir vers PostHog."""

    def test_commande_inconnue_jamais_envoyee_telle_quelle(self):
        for text in ("/0612345678", "/jean.dupont", "/https://exemple.test/x", "/valeurinconnue extra"):
            with self.subTest(text=text):
                self.assertEqual(bot._menu_kind(text), "commande_autre")

    def test_menu_kind_reste_dans_un_ensemble_ferme(self):
        allowed = {c[1:] for c in bot.MENU_COMMANDS} | {
            "commande_autre", "salutation", "ebooks", "boutique", "lettre", "tarifs", "aide", "volet_cv", "autre",
        } | {"mode_" + mode for mode in bot.BUTTON_MODES.values()}
        samples = ["", "   ", "Bonjour", "Salut", "/", "/@bot", "0707070707", "mon CV: Jean Dupont, Abidjan",
                   "https://exemple.test", "/start", "/cv", "/paye 0707070707 REF123", "/valider 42", "/OUBLI",
                   "é" * 500, *bot.BUTTON_MODES, bot.BUTTON_EBOOKS, bot.BUTTON_AIDE]
        for text in samples:
            with self.subTest(text=text[:30]):
                self.assertIn(bot._menu_kind(text), allowed)

    def test_appels_de_mesure_du_bot_limites_aux_evenements_et_proprietes_autorises(self):
        import ast
        tree = ast.parse(open(bot.__file__, encoding="utf-8").read())
        calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_track":
                calls.append(node)
        self.assertGreaterEqual(len(calls), 5)
        seen = set()
        for node in calls:
            event = node.args[0]
            self.assertIsInstance(event, ast.Constant)          # nom d'événement écrit en clair dans le code
            self.assertIn(event.value, usage_analytics.EVENT_NAMES)
            seen.add(event.value)
            self.assertEqual(len(node.args), 2)                 # (événement, user_id) : rien d'autre en positionnel
            for keyword in node.keywords:
                self.assertIn(keyword.arg, usage_analytics.ALLOWED_PROPERTIES)
        self.assertEqual(seen, usage_analytics.EVENT_NAMES)

    def test_un_seul_point_d_entree_vers_la_mesure(self):
        import ast
        tree = ast.parse(open(bot.__file__, encoding="utf-8").read())
        captures = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute) and n.func.attr == "capture"
                    and getattr(n.func.value, "id", None) == "usage"]
        self.assertEqual(len(captures), 1)                      # uniquement dans _track

    def test_valeurs_de_propriete_du_bot_sont_booleens_ou_codes_fixes(self):
        analytics, http, _ = make(200)
        with mock.patch.object(bot, "usage", analytics):
            bot._track("export_requested", 5, format="docx", nom="Jean Dupont", telephone="0707070707", texte="CV")
        props = http.calls[0][1]["properties"]
        self.assertEqual(set(props), {"format", "is_owner", "$lib", "$process_person_profile"})

    def test_les_secrets_posthog_sont_masques_dans_les_journaux_du_bot(self):
        key, salt = "phc_" + "k" * 30, "sel-secret-" + "s" * 20
        with mock.patch.dict("os.environ", {"POSTHOG_API_KEY": key, "POSTHOG_SALT": salt}):
            masked = bot.redact("erreur %s puis %s" % (key, salt))
        self.assertNotIn(key, masked)
        self.assertNotIn(salt, masked)

    def test_aucun_journal_ne_contient_cle_sel_ou_identifiant_en_clair(self):
        user_id = 987654321
        for statuses in ((500,), (401,), (400,), ("exc",), (429, 200)):
            analytics, _, _ = make(*statuses)
            with self.assertLogs(level="DEBUG") as logs:
                logging.getLogger("test").debug("amorce")
                analytics.capture("menu_used", user_id, kind="aide")
            joined = "\n".join(logs.output)
            for forbidden in (FAKE_KEY, FAKE_SALT, str(user_id)):
                self.assertNotIn(forbidden, joined)

    def test_le_sel_n_apparait_dans_aucune_charge_utile_ni_dans_la_representation(self):
        analytics, http, _ = make(200)
        analytics.capture("menu_used", 42, kind="aide")
        sent = json.dumps(http.calls[0][1])
        self.assertNotIn(FAKE_SALT, sent)
        self.assertNotIn(FAKE_SALT, repr(analytics.snapshot()))

    def test_documentation_distingue_pseudonymisation_et_anonymisation(self):
        import os
        doc = open(os.path.join(os.path.dirname(bot.__file__), "docs", "MESURE_USAGE.md"), encoding="utf-8").read().lower()
        self.assertIn("pseudonymis", doc)
        self.assertIn("pas anonyme", doc)


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
