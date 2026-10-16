"""Tests RÉELS Playwright : un vrai Chromium local lit de vraies pages servies par un serveur local.

Ce ne sont PAS des simulations. Ils sont ignorés (skip) si Chromium ne peut pas démarrer.
Le validateur d'URL est remplacé uniquement ici (paramètre _validator) pour autoriser 127.0.0.1 ;
le garde-fou de production, lui, est testé séparément et doit refuser ces mêmes adresses.
"""

import http.server
import subprocess
import threading
import time
import unittest
import unittest.mock

from integrations import playwright_client
from integrations.common import UnsafeURL

PAGE = b"""<!doctype html><html><head><title>Page de test CHARYAM</title></head>
<body><h1 id="titre">Bonjour</h1><div id="dyn">chargement</div><div id="attaque">attente</div>
<script>
document.getElementById('dyn').textContent = 'contenu rendu par JavaScript';
fetch('/secret').then(r => r.text()).then(t => {document.getElementById('attaque').textContent = 'FUITE:' + t})
  .catch(() => {document.getElementById('attaque').textContent = 'requete bloquee'});
</script></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/secret":
            body, ctype = b"donnee-interne", "text/plain"
        elif self.path == "/redir":
            self.send_response(302)
            self.send_header("Location", "/interdit")
            self.end_headers()
            return
        else:
            body, ctype = PAGE, "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def validator(url, domains=None):
    """Valideur de test : refuse /secret (sous-requête) et /interdit (redirection)"""
    if "/secret" in url or "/interdit" in url:
        raise UnsafeURL("chemin interdit (test)")
    return url


class RealPlaywrightTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                pw.chromium.launch(headless=True).close()
        except Exception as exc:   # navigateur absent ou non lançable dans cet environnement
            raise unittest.SkipTest(f"Chromium indisponible : {type(exc).__name__}")
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.env = {"PLAYWRIGHT_LOCAL": "1", "PLAYWRIGHT_ALLOWED_DOMAINS": "127.0.0.1"}

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "server"):
            cls.server.shutdown()

    def url(self, path="/"):
        return f"http://127.0.0.1:{self.port}{path}"

    def test_ouvre_attend_et_lit_une_page_dynamique(self):
        result = playwright_client.read_page(self.url(), env=self.env, _validator=validator)
        self.assertTrue(result.ok, result.text)
        self.assertEqual(result.meta["title"], "Page de test CHARYAM")
        self.assertIn("contenu rendu par JavaScript", result.text)   # preuve que le JavaScript s'est exécuté
        self.assertIn("donnees_externes_non_fiables", result.text)

    def test_extraction_par_selecteur(self):
        result = playwright_client.read_page(self.url(), extract_selector="#titre", env=self.env, _validator=validator)
        self.assertTrue(result.ok, result.text)
        self.assertIn("Bonjour", result.text)
        self.assertNotIn("contenu rendu", result.text)

    def test_les_sous_requetes_interdites_sont_bloquees_par_le_navigateur(self):
        result = playwright_client.read_page(self.url(), env=self.env, _validator=validator)
        self.assertTrue(result.ok, result.text)
        self.assertNotIn("FUITE", result.text)
        self.assertNotIn("donnee-interne", result.text)

    def test_redirection_vers_une_cible_interdite(self):
        result = playwright_client.read_page(self.url("/redir"), env=self.env, _validator=validator)
        self.assertFalse(result.ok)

    def test_delai_depasse_selecteur_absent(self):
        with unittest.mock.patch.object(playwright_client, "ACTION_TIMEOUT_MS", 1500):
            result = playwright_client.read_page(self.url(), wait_for_selector="#inexistant", env=self.env, _validator=validator)
        self.assertFalse(result.ok)
        self.assertIn("délai", result.text)

    def test_aucun_processus_navigateur_ne_survit(self):
        playwright_client.read_page(self.url(), env=self.env, _validator=validator)
        playwright_client.read_page(self.url("/redir"), env=self.env, _validator=validator)
        # Nom du processus (et non sa ligne de commande) : évite les faux positifs.
        # Chromium met quelques centaines de ms à s'éteindre : une vraie fuite, elle, persiste.
        def chromium_processes():
            names = subprocess.run(["ps", "-eo", "comm="], capture_output=True, text=True).stdout.split()
            return [n for n in names if "chrom" in n.lower() or "headless" in n.lower()]

        deadline = time.monotonic() + 5
        leftovers = chromium_processes()
        while leftovers and time.monotonic() < deadline:
            time.sleep(0.25)
            leftovers = chromium_processes()
        self.assertEqual(leftovers, [], "des processus Chromium sont restés ouverts")

    def test_un_seul_navigateur_a_la_fois(self):
        with unittest.mock.patch.object(playwright_client, "SLOT_WAIT_SECONDS", 0.1):
            self.assertTrue(playwright_client._browser_slot.acquire(timeout=1))
            try:
                result = playwright_client.read_page(self.url(), env=self.env, _validator=validator)
            finally:
                playwright_client._browser_slot.release()
        self.assertFalse(result.ok)
        self.assertIn("occupé", result.text)

    def test_le_garde_fou_de_production_refuse_ces_memes_adresses(self):
        for target in (self.url(), "http://169.254.169.254/latest/meta-data/", "http://10.0.0.1/"):
            result = playwright_client.read_page(target, env=self.env)   # validateur par défaut
            self.assertFalse(result.ok, target)
            self.assertIn("refusée", result.text)


class PlaywrightConfigTest(unittest.TestCase):
    def test_desactive_sans_configuration_ou_sans_liste_blanche(self):
        self.assertFalse(playwright_client.read_page("https://exemple.org", env={}).ok)
        self.assertFalse(playwright_client.read_page("https://exemple.org", env={"PLAYWRIGHT_LOCAL": "1"}).ok)

    def test_domaine_hors_liste_blanche(self):
        env = {"PLAYWRIGHT_LOCAL": "1", "PLAYWRIGHT_ALLOWED_DOMAINS": "exemple.org"}
        result = playwright_client.read_page("https://autre.org/", env=env)
        self.assertFalse(result.ok)
        self.assertIn("refusée", result.text)


if __name__ == "__main__":
    import unittest.mock  # noqa: F401
    unittest.main()
