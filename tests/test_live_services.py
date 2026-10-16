"""Tests RÉELS des services distants (Perplexity, Firecrawl, Composio, navigateur distant).

Ils appellent les vrais services (consomment un peu de quota) et ne s'exécutent QUE si :
    RUN_LIVE_TESTS=1  et la clé du service concerné est dans l'environnement.
Sinon ils sont signalés « skipped » : un test ignoré n'est PAS un test réussi.

Lancement (clés saisies dans votre terminal, jamais dans un chat ni un dépôt) :
    export RUN_LIVE_TESTS=1 PERPLEXITY_API_KEY=... FIRECRAWL_API_KEY=...
    python -m unittest tests.test_live_services -v
Composio : COMPOSIO_API_KEY, COMPOSIO_USER_ID, COMPOSIO_TOOL_VERSION, et un outil en lecture seule déjà
connecté : COMPOSIO_LIVE_TOOL=SLUG (COMPOSIO_LIVE_ARGS='{"cle": "valeur"}' facultatif ; le slug doit
aussi figurer dans COMPOSIO_READONLY_TOOLS).
Navigateur distant : PLAYWRIGHT_CDP_URL et PLAYWRIGHT_ALLOWED_DOMAINS=example.com.
"""

import json
import os
import unittest

from integrations import composio_client, firecrawl_client, perplexity_client, playwright_client

LIVE = os.environ.get("RUN_LIVE_TESTS") == "1"


def need(*names):
    missing = [n for n in names if not os.environ.get(n)]
    return unittest.skipUnless(LIVE and not missing, f"test réel ignoré (RUN_LIVE_TESTS=1 et {', '.join(names)} requis)")


class LivePerplexity(unittest.TestCase):
    @need("PERPLEXITY_API_KEY")
    def test_recherche_reelle(self):
        result = perplexity_client.search("Côte d'Ivoire cacao prix", recency="month", max_results=3)
        self.assertTrue(result.ok, result.text)
        self.assertGreaterEqual(len(result.meta["sources"]), 1)
        for source in result.meta["sources"]:
            self.assertTrue(source["url"].startswith("http"))

    @need("PERPLEXITY_API_KEY")
    def test_cle_invalide_est_refusee_proprement(self):
        result = perplexity_client.search("test", env={"PERPLEXITY_API_KEY": "pplx-cle-volontairement-invalide-000"})
        self.assertFalse(result.ok)


class LiveFirecrawl(unittest.TestCase):
    @need("FIRECRAWL_API_KEY")
    def test_extraction_reelle(self):
        result = firecrawl_client.scrape("https://example.com")
        self.assertTrue(result.ok, result.text)
        self.assertIn("Example Domain", result.text)

    @need("FIRECRAWL_API_KEY")
    def test_cle_invalide_est_refusee_proprement(self):
        result = firecrawl_client.scrape("https://example.com", env={"FIRECRAWL_API_KEY": "fc-cle-volontairement-invalide-000"})
        self.assertFalse(result.ok)
        self.assertNotIn("fc-cle-volontairement", result.text)


class LiveComposio(unittest.TestCase):
    @need("COMPOSIO_API_KEY", "COMPOSIO_USER_ID", "COMPOSIO_TOOL_VERSION", "COMPOSIO_LIVE_TOOL", "COMPOSIO_READONLY_TOOLS")
    def test_action_lecture_seule_reelle(self):
        arguments = json.loads(os.environ.get("COMPOSIO_LIVE_ARGS", "{}"))
        result = composio_client.execute_readonly(os.environ["COMPOSIO_LIVE_TOOL"], arguments)
        self.assertTrue(result.ok, result.text)


class LiveRemoteBrowser(unittest.TestCase):
    @need("PLAYWRIGHT_CDP_URL", "PLAYWRIGHT_ALLOWED_DOMAINS")
    def test_page_reelle_via_navigateur_distant(self):
        result = playwright_client.read_page("https://example.com")
        self.assertTrue(result.ok, result.text)
        self.assertIn("Example Domain", result.text)


if __name__ == "__main__":
    unittest.main()
