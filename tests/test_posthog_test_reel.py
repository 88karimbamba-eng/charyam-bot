"""Tests SIMULÉS du script scripts/posthog_test_reel.py : aucun envoi réel, aucune requête réseau."""

import ast
import contextlib
import importlib.util
import io
import logging
import os
import unittest

import test_charyam_telegram as base
import usage_analytics

bot = base.bot
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("posthog_test_reel", os.path.join(ROOT, "scripts", "posthog_test_reel.py"))
reel = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reel)

CLE = "phc_" + "TestCleSecreteFictive1234567890"
SEL = "sel-de-test-secret-fictif-98765"
HOTE = "https://eu.i.posthog.com"
ENV = {"POSTHOG_API_KEY": CLE, "POSTHOG_SALT": SEL, "POSTHOG_HOST": HOTE}
CONFIRME = ["--confirm-projet-de-test"]


class FakeResponse:
    def __init__(self, status):
        self.status_code = status
        self.headers = {}


class FakeHttp:
    def __init__(self, status=200, erreur=None):
        self.status = status
        self.erreur = erreur
        self.calls = []

    def post(self, url, json=None):
        self.calls.append((url, json))
        if self.erreur:
            raise self.erreur
        return FakeResponse(self.status)


class InterdiHttp:
    def post(self, *args, **kwargs):
        raise AssertionError("aucune requête réseau ne doit partir ici")


def lancer(argv, env=None, reponse="charyam-test", http=None):
    """Exécute le script ; retourne (code, sortie standard + erreurs, journaux)."""
    sortie = io.StringIO()
    journal = io.StringIO()
    handler = logging.StreamHandler(journal)
    logging.getLogger("charyam.analytics").addHandler(handler)
    try:
        with contextlib.redirect_stdout(sortie), contextlib.redirect_stderr(sortie):
            if isinstance(reponse, BaseException):
                def saisie(_):
                    raise reponse
            else:
                saisie = lambda _: reponse   # noqa: E731
            code = reel.executer(argv, env=ENV if env is None else env, saisie=saisie, http=http, sleep=lambda _s: None)
    finally:
        logging.getLogger("charyam.analytics").removeHandler(handler)
    return code, sortie.getvalue() + journal.getvalue()


class ConfigurationTest(unittest.TestCase):
    def assertRefuse(self, argv, env, reponse="charyam-test", motif=None):
        http = FakeHttp()
        code, sortie = lancer(argv, env=env, reponse=reponse, http=http)
        self.assertEqual(code, 2, sortie)
        self.assertIn("REFUSÉ", sortie)
        if motif:
            self.assertIn(motif, sortie)       # refusé pour la BONNE raison, pas par un garde-fou en aval
        self.assertEqual(http.calls, [], "rien ne doit partir quand la configuration est refusée")
        for secret in (CLE, SEL):
            self.assertNotIn(secret, sortie)

    def test_refuse_sans_confirmation_de_projet_de_test(self):
        self.assertRefuse([], ENV)

    def test_hote_obligatoire_sans_region_par_defaut(self):
        env = {k: v for k, v in ENV.items() if k != "POSTHOG_HOST"}
        self.assertRefuse(CONFIRME, env, motif="POSTHOG_HOST est obligatoire")
        self.assertRefuse(CONFIRME, {**ENV, "POSTHOG_HOST": "   "}, motif="POSTHOG_HOST est obligatoire")

    def test_refuse_hote_http(self):
        self.assertRefuse(CONFIRME, {**ENV, "POSTHOG_HOST": "http://us.i.posthog.com"}, motif="HTTPS")

    def test_refuse_hotes_non_autorises(self):
        for hote in ("https://app.posthog.com", "https://exemple.test", "https://us.i.posthog.com.evil.test",
                     "https://evil.test/us.i.posthog.com", "https://us.i.posthog.com@evil.test",
                     "https://us.i.posthog.com:8443", "https://us.i.posthog.com/autre", "ftp://us.i.posthog.com",
                     "us.i.posthog.com", "https://eu.posthog.com"):
            with self.subTest(hote=hote):
                self.assertRefuse(CONFIRME, {**ENV, "POSTHOG_HOST": hote})

    def test_accepte_les_deux_regions_et_normalise(self):
        for hote, attendu in (("https://us.i.posthog.com", "https://us.i.posthog.com"),
                              ("https://EU.i.posthog.com/", "https://eu.i.posthog.com")):
            with self.subTest(hote=hote):
                http = FakeHttp(200)
                code, sortie = lancer(CONFIRME, env={**ENV, "POSTHOG_HOST": hote}, http=http)
                self.assertEqual(code, 0, sortie)
                self.assertTrue(all(url == attendu + usage_analytics.CAPTURE_PATH for url, _ in http.calls))

    def test_refuse_configuration_incomplete(self):
        self.assertRefuse(CONFIRME, {k: v for k, v in ENV.items() if k != "POSTHOG_API_KEY"}, motif="POSTHOG_API_KEY")
        self.assertRefuse(CONFIRME, {**ENV, "POSTHOG_API_KEY": "pas_une_cle_projet_posthog"})
        self.assertRefuse(CONFIRME, {**ENV, "POSTHOG_API_KEY": "phc_"})
        self.assertRefuse(CONFIRME, {k: v for k, v in ENV.items() if k != "POSTHOG_SALT"}, motif="16 caractères minimum")
        self.assertRefuse(CONFIRME, {**ENV, "POSTHOG_SALT": "court"}, motif="16 caractères minimum")

    def test_confirmation_manuelle_obligatoire(self):
        for reponse in ("", "oui", "CHARYAM-TEST", "charyam", "charyam-prod", EOFError(), KeyboardInterrupt()):
            with self.subTest(reponse=repr(reponse)):
                self.assertRefuse(CONFIRME, ENV, reponse=reponse)

    def test_affiche_environnement_hote_et_ne_montre_jamais_les_secrets(self):
        http = FakeHttp(200)
        code, sortie = lancer(CONFIRME, http=http)
        self.assertEqual(code, 0, sortie)
        self.assertIn("ENVIRONNEMENT : TEST", sortie)
        self.assertIn(HOTE, sortie)
        self.assertIn("charyam-test", sortie)
        self.assertNotIn(CLE, sortie)
        self.assertNotIn(SEL, sortie)

    def test_l_hote_est_affiche_avant_la_demande_de_confirmation(self):
        ordre = []
        sortie = io.StringIO()
        with contextlib.redirect_stdout(sortie):
            reel.executer(CONFIRME, env=ENV, saisie=lambda _: ordre.append(sortie.getvalue()) or "non", http=FakeHttp())
        self.assertIn(HOTE, ordre[0])
        self.assertIn("ENVIRONNEMENT : TEST", ordre[0])


class EvenementsDuScriptTest(unittest.TestCase):
    def test_cinq_evenements_exactement_ceux_de_la_liste_fermee(self):
        http = FakeHttp(200)
        code, sortie = lancer(CONFIRME, http=http)
        self.assertEqual(code, 0, sortie)
        noms = [payload["event"] for _, payload in http.calls]
        self.assertEqual(len(noms), 5)
        self.assertEqual(set(noms), usage_analytics.EVENT_NAMES)
        self.assertIn("5 événements acceptés", sortie)

    def test_proprietes_limitees_a_la_liste_blanche_avec_is_owner_partout(self):
        http = FakeHttp(200)
        lancer(CONFIRME, http=http)
        autorisees = usage_analytics.ALLOWED_PROPERTIES | {"$lib", "$process_person_profile"}
        for _, payload in http.calls:
            proprietes = payload["properties"]
            self.assertLessEqual(set(proprietes), autorisees, payload["event"])
            self.assertIn("is_owner", proprietes, payload["event"])
            self.assertIsInstance(proprietes["is_owner"], bool)
            self.assertIs(proprietes["$process_person_profile"], False)

    def test_is_owner_vrai_seulement_pour_le_faux_proprietaire(self):
        http = FakeHttp(200)
        lancer(CONFIRME, http=http)
        for _, payload in http.calls:
            attendu = payload["distinct_id"] == usage_analytics.UsageAnalytics(
                env={"POSTHOG_API_KEY": CLE, "POSTHOG_SALT": SEL}).distinct_id(reel.FAUX_PROPRIETAIRE)
            self.assertEqual(payload["properties"]["is_owner"], attendu)

    def test_memes_proprietes_que_le_bot_pour_chaque_evenement(self):
        """Les propriétés du script = celles que le bot envoie réellement (arguments de _track + is_owner)."""
        tree = ast.parse(open(bot.__file__, encoding="utf-8").read())
        du_bot = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_track":
                du_bot[node.args[0].value] = {kw.arg for kw in node.keywords} | {"is_owner"}
        du_script = {nom: set(props) | {"is_owner"} for nom, _, props in reel.EVENEMENTS}
        self.assertEqual(du_script, du_bot)

    def test_identifiants_pseudonymises_et_aucune_donnee_personnelle(self):
        http = FakeHttp(200)
        lancer(CONFIRME, http=http)
        for _, payload in http.calls:
            texte = repr(payload).replace(CLE, "")
            self.assertRegex(payload["distinct_id"], r"^u_[0-9a-f]{16}$")
            self.assertNotIn(str(reel.FAUX_UTILISATEUR), repr({k: v for k, v in payload.items() if k != "distinct_id"}))
            self.assertNotIn(SEL, texte)

    def test_la_cle_de_test_est_utilisee_pour_l_envoi_normal(self):
        http = FakeHttp(200)
        lancer(CONFIRME, http=http)
        self.assertTrue(all(payload["api_key"] == CLE for _, payload in http.calls))


class CleInvalideTest(unittest.TestCase):
    def test_une_seule_requete_avec_une_fausse_cle_et_les_autres_abandonnees(self):
        http = FakeHttp(401)
        code, sortie = lancer(CONFIRME + ["--cle-invalide"], http=http)
        self.assertEqual(code, 0, sortie)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(http.calls[0][1]["api_key"], reel.FAUSSE_CLE)
        self.assertNotIn(CLE, repr(http.calls))                 # la vraie clé n'est jamais utilisée
        self.assertIn("RÉSULTAT clé invalide : OK", sortie)
        self.assertIn("'rejected': 1", sortie)
        self.assertIn("'dropped': 4", sortie)

    def test_403_est_traite_comme_401(self):
        http = FakeHttp(403)
        code, _ = lancer(CONFIRME + ["--cle-invalide"], http=http)
        self.assertEqual((code, len(http.calls)), (0, 1))

    def test_reponse_inattendue_signalee_comme_anomalie(self):
        http = FakeHttp(200)      # PostHog accepterait la fausse clé : ce n'est pas le comportement attendu
        code, sortie = lancer(CONFIRME + ["--cle-invalide"], http=http)
        self.assertEqual(code, 1)
        self.assertIn("ANOMALIE", sortie)

    def test_la_cle_invalide_ne_necessite_pas_la_vraie_cle_mais_exige_hote_et_sel(self):
        sans_cle = {k: v for k, v in ENV.items() if k != "POSTHOG_API_KEY"}
        code, _ = lancer(CONFIRME + ["--cle-invalide"], env=sans_cle, http=FakeHttp(401))
        self.assertEqual(code, 0)
        sans_hote = {k: v for k, v in ENV.items() if k != "POSTHOG_HOST"}
        code, _ = lancer(CONFIRME + ["--cle-invalide"], env=sans_hote, http=FakeHttp(401))
        self.assertEqual(code, 2)

    def test_les_secrets_n_apparaissent_ni_a_l_ecran_ni_dans_les_journaux(self):
        for http in (FakeHttp(401), FakeHttp(500), FakeHttp(400), FakeHttp(erreur=RuntimeError("coupé " + CLE + SEL))):
            for argv in (CONFIRME, CONFIRME + ["--cle-invalide"]):
                code, sortie = lancer(argv, http=http)
                self.assertIn(code, (0, 1, 2))
                self.assertNotIn(CLE, sortie)
                self.assertNotIn(SEL, sortie)


class Strict:
    """Remplace UsageAnalytics et coupe le réseau : prouve qu'un contrôle en échec n'ouvre aucune connexion."""

    def __init__(self):
        self.instances = 0
        self.connexions = 0
        self.invites = []

    def __enter__(self):
        import socket
        from unittest import mock
        strict = self

        def interdit(*args, **kwargs):
            strict.connexions += 1
            raise RuntimeError("RÉSEAU INTERDIT")

        def fabrique(*args, **kwargs):
            strict.instances += 1
            return self._vraie(*args, **kwargs)

        self._vraie = usage_analytics.UsageAnalytics
        self._patchs = [mock.patch.object(socket.socket, "connect", interdit),
                        mock.patch.object(socket, "getaddrinfo", interdit),
                        mock.patch.object(socket, "create_connection", interdit),
                        mock.patch.object(usage_analytics, "UsageAnalytics", fabrique)]
        for patch in self._patchs:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in self._patchs:
            patch.stop()


def lancer_strict(argv, env, reponse="charyam-test", http=None):
    with Strict() as strict:
        def saisie(invite):
            strict.invites.append(invite)
            return reponse
        sortie = io.StringIO()
        with contextlib.redirect_stdout(sortie), contextlib.redirect_stderr(sortie):
            code = reel.executer(argv, env=env, saisie=saisie, http=http, sleep=lambda _s: None)
    return code, sortie.getvalue(), strict


class TableauDeControleTest(unittest.TestCase):
    """Chaque contrôle local est visible dans la console, avant toute connexion et avant toute confirmation."""

    def ligne(self, sortie, variable):
        lignes = [l for l in sortie.splitlines() if l.startswith(variable)]
        self.assertEqual(len(lignes), 1, "ligne du tableau manquante pour " + variable)
        return lignes[0]

    def assertEchec(self, env, variable, fragment, argv=CONFIRME):
        code, sortie, strict = lancer_strict(argv, env)
        self.assertEqual(code, 2, sortie)
        ligne = self.ligne(sortie, variable)
        self.assertTrue(ligne.rstrip().endswith("FAIL"), ligne)
        self.assertIn(fragment, ligne)
        self.assertIn("REFUSÉ", sortie)
        self.assertEqual(strict.invites, [], "aucune confirmation demandée tant qu'un contrôle échoue")
        self.assertEqual(strict.instances, 0, "le moteur d'envoi ne doit même pas être créé")
        self.assertEqual(strict.connexions, 0, "aucune connexion réseau")
        for secret in (CLE, SEL):
            self.assertNotIn(secret, sortie)
        return sortie

    def test_variables_absentes(self):
        for variable, fragment in (("POSTHOG_API_KEY", "absente"), ("POSTHOG_SALT", "absente"), ("POSTHOG_HOST", "absente")):
            with self.subTest(variable=variable):
                self.assertEchec({k: v for k, v in ENV.items() if k != variable}, variable, fragment)

    def test_variables_vides(self):
        for variable in ("POSTHOG_API_KEY", "POSTHOG_SALT", "POSTHOG_HOST"):
            for vide in ("", "   "):
                with self.subTest(variable=variable, valeur=repr(vide)):
                    self.assertEchec({**ENV, variable: vide}, variable, "vide")

    def test_cle_sans_prefixe_phc(self):
        for cle in ("sk_live_" + "x" * 30, "PHC_" + "x" * 30, "phx_" + "x" * 30, "phc_court"):
            with self.subTest(cle=cle[:6]):
                sortie = self.assertEchec({**ENV, "POSTHOG_API_KEY": cle}, "POSTHOG_API_KEY", "NON respecté")
                self.assertNotIn(cle, sortie)

    def test_sel_de_15_caracteres_refuse_avec_sa_longueur(self):
        self.assertEchec({**ENV, "POSTHOG_SALT": "a" * 15}, "POSTHOG_SALT", "longueur 15 (minimum 16 : non atteint)")

    def test_sel_de_16_caracteres_accepte_avec_sa_longueur(self):
        sel = "Zq" * 8
        code, sortie, strict = lancer_strict(CONFIRME, {**ENV, "POSTHOG_SALT": sel}, http=FakeHttp(200))
        self.assertEqual(code, 0, sortie)
        ligne = self.ligne(sortie, "POSTHOG_SALT")
        self.assertIn("longueur 16 (minimum 16 : atteint)", ligne)
        self.assertTrue(ligne.rstrip().endswith("PASS"))
        self.assertNotIn(sel, sortie)

    def test_longueur_reelle_du_sel_affichee(self):
        for longueur in (17, 32, 44):
            with self.subTest(longueur=longueur):
                sel = ("Xy9" * 30)[:longueur]
                _, sortie, _ = lancer_strict(CONFIRME, {**ENV, "POSTHOG_SALT": sel}, http=FakeHttp(200))
                self.assertIn("longueur %d (minimum 16 : atteint)" % longueur, self.ligne(sortie, "POSTHOG_SALT"))
                self.assertNotIn(sel, sortie)

    def test_hotes_non_autorises_refuses_sans_afficher_la_valeur(self):
        for hote in ("http://us.i.posthog.com", "https://app.posthog.com", "https://exemple.test",
                     "https://us.i.posthog.com.evil.test", "https://utilisateur:motdepasse@eu.i.posthog.com", CLE):
            with self.subTest(hote=hote[:25]):
                sortie = self.assertEchec({**ENV, "POSTHOG_HOST": hote}, "POSTHOG_HOST", "REFUSÉ")
                self.assertNotIn(hote, sortie)

    def test_hotes_autorises_affiches_et_acceptes(self):
        for hote in ("https://us.i.posthog.com", "https://eu.i.posthog.com"):
            with self.subTest(hote=hote):
                code, sortie, _ = lancer_strict(CONFIRME, {**ENV, "POSTHOG_HOST": hote}, http=FakeHttp(200))
                self.assertEqual(code, 0, sortie)
                ligne = self.ligne(sortie, "POSTHOG_HOST")
                self.assertIn(hote + " : autorisé", ligne)
                self.assertTrue(ligne.rstrip().endswith("PASS"))

    def test_toutes_les_lignes_sont_visibles_meme_quand_plusieurs_controles_echouent(self):
        code, sortie, strict = lancer_strict(CONFIRME, {})
        self.assertEqual(code, 2)
        for variable in ("POSTHOG_API_KEY", "POSTHOG_SALT", "POSTHOG_HOST"):
            self.assertTrue(self.ligne(sortie, variable).rstrip().endswith("FAIL"))
        self.assertIn("CONTRÔLE DES VARIABLES LOCALES (avant toute connexion réseau)", sortie)
        self.assertEqual((strict.invites, strict.instances, strict.connexions), ([], 0, 0))

    def test_tableau_complet_et_affiche_avant_la_confirmation(self):
        affiche_avant = []
        sortie = io.StringIO()
        with Strict() as strict, contextlib.redirect_stdout(sortie):
            reel.executer(CONFIRME, env=ENV, saisie=lambda _: affiche_avant.append(sortie.getvalue()) or "non", http=FakeHttp())
        vu = affiche_avant[0]
        for variable in ("POSTHOG_API_KEY", "POSTHOG_SALT", "POSTHOG_HOST"):
            self.assertRegex(vu, variable + r".*PASS")
        self.assertIn("ENVIRONNEMENT : TEST", vu)
        self.assertIn("charyam-test", vu)
        self.assertEqual((strict.instances, strict.connexions), (0, 0))   # rien d'ouvert avant/sans confirmation

    def test_clef_marquee_non_utilisee_pour_le_scenario_cle_invalide(self):
        code, sortie, _ = lancer_strict(CONFIRME + ["--cle-invalide"], {k: v for k, v in ENV.items() if k != "POSTHOG_API_KEY"},
                                        http=FakeHttp(401))
        self.assertEqual(code, 0, sortie)
        ligne = self.ligne(sortie, "POSTHOG_API_KEY")
        self.assertIn("non utilisée", ligne)
        self.assertTrue(ligne.rstrip().endswith("N/A"))

    def test_aucun_evenement_ni_connexion_avant_la_confirmation_du_projet(self):
        for reponse in ("", "non", "charyam", "CHARYAM-TEST", "charyam-test ; oui"):
            with self.subTest(reponse=reponse):
                http = FakeHttp(200)
                code, sortie, strict = lancer_strict(CONFIRME, ENV, reponse=reponse, http=http)
                self.assertEqual(code, 2, sortie)
                self.assertEqual((http.calls, strict.instances, strict.connexions), ([], 0, 0))
                self.assertEqual(len(strict.invites), 1)
                self.assertIn("charyam-test", strict.invites[0])

    def test_la_confirmation_humaine_liste_les_cinq_points_et_la_limite(self):
        _, sortie, _ = lancer_strict(CONFIRME, ENV, reponse="non", http=FakeHttp())
        for point in ("l'organisation sélectionnée est « charyam-test »", "projet de test dédié", "provient de ce projet précis",
                      "région", "aucune clé de production"):
            self.assertIn(point, sortie)
        self.assertIn("ne prouve PAS", sortie)
        self.assertIn("ne peut pas le vérifier", sortie)
        self.assertNotIn("clé vérifiée automatiquement", sortie)

    def test_secrets_jamais_affiches_ni_ecrits_dans_un_fichier(self):
        def fichiers():
            trouves = set()
            for dossier, sous, noms in os.walk(ROOT):
                sous[:] = [d for d in sous if d not in (".git", "__pycache__", ".pytest_cache", ".venv-test")]
                trouves.update(os.path.join(dossier, n) for n in noms)
            return trouves
        avant = fichiers()
        CLE_ALEA, SEL_ALEA = "phc_Qz7Kx9Vw2Rm4Lp8Nd3Hs", "Jb5Tc6Yg1Fv0Ua2Xe7Wo4Zi9"
        alea = {"POSTHOG_API_KEY": CLE_ALEA, "POSTHOG_SALT": SEL_ALEA, "POSTHOG_HOST": HOTE}
        for env in (ENV, {**ENV, "POSTHOG_SALT": "court"}, {**ENV, "POSTHOG_HOST": CLE}, {**ENV, "POSTHOG_API_KEY": SEL}, alea):
            for argv in (CONFIRME, CONFIRME + ["--cle-invalide"]):
                _, sortie, _ = lancer_strict(argv, env, http=FakeHttp(401))
                sortie2 = lancer_strict(argv, alea, http=FakeHttp(401))[1]
                for secret in (CLE, SEL):
                    self.assertNotIn(secret, sortie)
                    self.assertNotIn(secret[:10], sortie)      # ni même un début de secret
                # Secrets aléatoires distincts : aucune suite de 4 caractères ne doit apparaître dans la sortie
                for secret in (CLE_ALEA[4:], SEL_ALEA):      # sans le préfixe public phc_
                    for i in range(len(secret) - 3):
                        self.assertNotIn(secret[i:i + 4], sortie2)
        self.assertEqual(fichiers(), avant, "le script ne doit créer aucun fichier")


class ModeABlancTest(unittest.TestCase):
    def test_a_blanc_sans_reseau_sans_lire_les_variables(self):
        sortie = io.StringIO()
        with contextlib.redirect_stdout(sortie):
            code = reel.executer(["--a-blanc"], env=ENV, saisie=lambda _: self.fail("pas de saisie en mode à blanc"),
                                 http=InterdiHttp())
        texte = sortie.getvalue()
        self.assertEqual(code, 0)
        for nom in usage_analytics.EVENT_NAMES:
            self.assertIn('"event": "%s"' % nom, texte)
        self.assertIn('"api_key": "***"', texte)
        self.assertNotIn(CLE, texte)
        self.assertNotIn(SEL, texte)

    def test_a_blanc_fonctionne_sans_aucune_variable(self):
        sortie = io.StringIO()
        with contextlib.redirect_stdout(sortie):
            code = reel.executer(["--a-blanc"], env={}, saisie=lambda _: "")
        self.assertEqual(code, 0)


class ErreurInattendueTest(unittest.TestCase):
    def test_main_n_affiche_que_le_type_de_l_erreur(self):
        sortie = io.StringIO()
        original = reel.executer
        reel.executer = lambda argv: (_ for _ in ()).throw(ValueError("contient " + CLE))
        try:
            with contextlib.redirect_stdout(sortie):
                code = reel.main(CONFIRME)
        finally:
            reel.executer = original
        self.assertEqual(code, 1)
        self.assertIn("ValueError", sortie.getvalue())
        self.assertNotIn(CLE, sortie.getvalue())


class DocumentationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = open(os.path.join(ROOT, "docs", "TEST_REEL_POSTHOG.md"), encoding="utf-8").read()

    def test_commandes_powershell_sans_secret_en_clair(self):
        self.assertIn("Read-Host", self.doc)
        self.assertIn("-AsSecureString", self.doc)
        self.assertIn("SaveNothing", self.doc)
        self.assertIn("Remove-Item Env:POSTHOG_API_KEY", self.doc)
        self.assertNotRegex(self.doc, r"phc_[A-Za-z0-9]{16,}")

    def test_documente_region_ip_et_limites(self):
        for morceau in ("us.i.posthog.com", "eu.i.posthog.com", "Discard client IP data", "IP data capture configuration",
                        "GeoIP", "ne permet pas de savoir", "pseudonymis", "pas anonyme"):
            self.assertIn(morceau, self.doc)

    def test_ne_pretend_pas_que_les_evenements_prouvent_un_paiement_ou_une_livraison(self):
        self.assertIn("payment_requested", self.doc)
        self.assertIn("ne prouve pas", self.doc)


if __name__ == "__main__":
    unittest.main()
