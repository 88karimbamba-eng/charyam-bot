"""Tests SIMULÉS : routeur d'outils et boucle d'outils (Claude est un faux scénarisé)."""

import time
import unittest
from types import SimpleNamespace
from unittest import mock

from integrations import tool_router
from integrations.common import ToolResult
from integrations.tool_router import ToolRouter, complete_with_tools

ALL_ENV = {
    "PERPLEXITY_API_KEY": "pplx-TESTKEY1234567890",
    "FIRECRAWL_API_KEY": "fc-TESTKEY1234567890",
    "PLAYWRIGHT_CDP_URL": "wss://browser.example/?token=T0K3N123456",
    "PLAYWRIGHT_ALLOWED_DOMAINS": "exemple.org",
    "COMPOSIO_API_KEY": "cmp-TESTKEY1234567890",
    "COMPOSIO_USER_ID": "u1",
    "COMPOSIO_READONLY_TOOLS": "SLACK_LIST_CHANNELS",
}


def reply(*blocks, stop="end_turn"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop, model="m", usage=SimpleNamespace(input_tokens=1, output_tokens=1))


def text(value):
    return SimpleNamespace(type="text", text=value)


def tool_use(name, tool_input, call_id="tu_1"):
    return SimpleNamespace(type="tool_use", id=call_id, name=name, input=tool_input)


class Scripted:
    """Faux « client.messages.create » : renvoie les réponses dans l'ordre et mémorise les appels"""

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class DefinitionsTest(unittest.TestCase):
    def names(self, env):
        return sorted(d["name"] for d in ToolRouter(env=env).definitions())

    def test_rien_n_est_expose_sans_configuration(self):
        router = ToolRouter(env={})
        self.assertFalse(router.enabled())
        self.assertEqual(router.definitions(), [])
        self.assertFalse(any(router.status().values()))

    def test_tous_les_outils_quand_tout_est_configure(self):
        self.assertEqual(self.names(ALL_ENV), sorted([
            "recherche_web", "lire_page_web", "explorer_site_web", "naviguer_page_web", "action_application",
        ]))

    def test_chaque_outil_depend_de_sa_propre_configuration(self):
        self.assertEqual(self.names({"PERPLEXITY_API_KEY": "pplx-x" * 3}), ["recherche_web"])
        self.assertEqual(self.names({"FIRECRAWL_API_KEY": "fc-xxxxxx"}), ["explorer_site_web", "lire_page_web"])

    def test_navigateur_exige_une_liste_blanche(self):
        self.assertEqual(self.names({"PLAYWRIGHT_CDP_URL": "wss://b.example"}), [])
        self.assertEqual(self.names({"PLAYWRIGHT_LOCAL": "1"}), [])
        self.assertEqual(self.names({"PLAYWRIGHT_LOCAL": "1", "PLAYWRIGHT_ALLOWED_DOMAINS": "exemple.org"}), ["naviguer_page_web"])

    def test_composio_exige_la_liste_blanche_et_l_utilisateur(self):
        self.assertEqual(self.names({"COMPOSIO_API_KEY": "cmp-xxxxxx", "COMPOSIO_USER_ID": "u"}), [])
        self.assertEqual(self.names({"COMPOSIO_API_KEY": "cmp-xxxxxx", "COMPOSIO_READONLY_TOOLS": "A"}), [])

    def test_le_statut_ne_contient_aucun_secret(self):
        self.assertNotIn("TESTKEY", repr(ToolRouter(env=ALL_ENV).status()))
        self.assertNotIn("T0K3N", repr(ToolRouter(env=ALL_ENV).status()))


class RoutingTest(unittest.TestCase):
    """Chaque demande doit atteindre le bon client, et seulement celui-là."""

    def setUp(self):
        self.router = ToolRouter(env=ALL_ENV)
        self.targets = {
            "perplexity": mock.patch.object(tool_router.perplexity_client, "search", return_value=ToolResult(True, "P")),
            "scrape": mock.patch.object(tool_router.firecrawl_client, "scrape", return_value=ToolResult(True, "S")),
            "crawl": mock.patch.object(tool_router.firecrawl_client, "crawl", return_value=ToolResult(True, "C")),
            "playwright": mock.patch.object(tool_router.playwright_client, "read_page", return_value=ToolResult(True, "B")),
            "composio": mock.patch.object(tool_router.composio_client, "execute_readonly", return_value=ToolResult(True, "A")),
        }
        self.mocks = {name: patcher.start() for name, patcher in self.targets.items()}
        self.addCleanup(mock.patch.stopall)

    def called(self):
        return sorted(name for name, m in self.mocks.items() if m.called)

    def test_recherche_vers_perplexity(self):
        self.assertEqual(self.router.execute("recherche_web", {"requete": "cacao", "fraicheur": "day"}).text, "P")
        self.assertEqual(self.called(), ["perplexity"])

    def test_page_vers_firecrawl(self):
        self.router.execute("lire_page_web", {"url": "https://exemple.org"})
        self.assertEqual(self.called(), ["scrape"])

    def test_site_vers_crawl(self):
        self.router.execute("explorer_site_web", {"url": "https://exemple.org", "pages": 2})
        self.assertEqual(self.called(), ["crawl"])

    def test_navigation_vers_playwright(self):
        self.router.execute("naviguer_page_web", {"url": "https://exemple.org", "extraire_selecteur": "main"})
        self.assertEqual(self.called(), ["playwright"])

    def test_application_vers_composio(self):
        self.router.execute("action_application", {"outil": "SLACK_LIST_CHANNELS", "arguments": {}})
        self.assertEqual(self.called(), ["composio"])

    def test_outil_inconnu_ou_non_configure_n_appelle_rien(self):
        limited = ToolRouter(env={"PERPLEXITY_API_KEY": "pplx-xxxxxxx"})
        for name in ("lire_page_web", "naviguer_page_web", "action_application", "inconnu"):
            self.assertFalse(limited.execute(name, {}).ok)
        self.assertEqual(self.called(), [])

    def test_arguments_invalides(self):
        self.assertFalse(self.router.execute("recherche_web", "pas un objet").ok)
        self.assertEqual(self.called(), [])

    def test_une_exception_dans_un_client_ne_remonte_pas(self):
        self.mocks["perplexity"].side_effect = RuntimeError("boom")
        result = self.router.execute("recherche_web", {"requete": "x"})
        self.assertFalse(result.ok)

    def test_delai_maximal(self):
        self.mocks["perplexity"].side_effect = lambda *a, **k: time.sleep(1.0)
        with mock.patch.dict(ToolRouter.TOOL_TIMEOUTS, {"recherche_web": 0.05}):
            result = self.router.execute("recherche_web", {"requete": "x"})
        self.assertFalse(result.ok)
        self.assertIn("délai", result.text)

    def test_quota_horaire(self):
        router = ToolRouter(env=dict(ALL_ENV, TOOL_CALLS_PER_HOUR="2"))
        results = [router.execute("recherche_web", {"requete": "x"}).ok for _ in range(3)]
        self.assertEqual(results, [True, True, False])


class LoopTest(unittest.TestCase):
    def setUp(self):
        self.router = ToolRouter(env=ALL_ENV)
        patcher = mock.patch.object(tool_router.perplexity_client, "search", return_value=ToolResult(True, "RÉSULTAT P"))
        self.search = patcher.start()
        self.addCleanup(patcher.stop)

    def base_kwargs(self):
        return {"model": "m", "max_tokens": 10, "system": "SYS", "messages": [{"role": "user", "content": "question"}]}

    def test_sans_outil_configure_un_seul_appel_inchange(self):
        create = Scripted(reply(text("réponse")))
        response = complete_with_tools(create, self.base_kwargs(), ToolRouter(env={}))
        self.assertEqual(len(create.calls), 1)
        self.assertNotIn("tools", create.calls[0])
        self.assertEqual(create.calls[0]["system"], "SYS")
        self.assertEqual(response.content[0].text, "réponse")

    def test_outils_proposes_mais_pas_utilises(self):
        create = Scripted(reply(text("Bonjour !")))
        response = complete_with_tools(create, self.base_kwargs(), self.router)
        self.assertEqual(len(create.calls), 1)
        self.assertIn("tools", create.calls[0])
        self.assertIn("jamais systématiquement", create.calls[0]["system"])
        self.assertEqual(response.content[0].text, "Bonjour !")
        self.assertFalse(self.search.called)

    def test_un_tour_d_outil(self):
        first = reply(tool_use("recherche_web", {"requete": "cacao"}), stop="tool_use")
        create = Scripted(first, reply(text("Voici la synthèse")))
        response = complete_with_tools(create, self.base_kwargs(), self.router)
        self.assertEqual(response.content[0].text, "Voici la synthèse")
        self.search.assert_called_once()
        second_messages = create.calls[1]["messages"]
        self.assertEqual(second_messages[1]["role"], "assistant")
        self.assertIs(second_messages[1]["content"][0], first.content[0])   # blocs d'origine renvoyés tels quels
        result_block = second_messages[2]["content"][0]
        self.assertEqual(result_block["type"], "tool_result")
        self.assertEqual(result_block["tool_use_id"], "tu_1")
        self.assertFalse(result_block["is_error"])
        self.assertEqual(result_block["content"], "RÉSULTAT P")

    def test_le_bloc_de_reflexion_est_conserve(self):
        thinking = SimpleNamespace(type="thinking", thinking="...", signature="sig")
        first = reply(thinking, tool_use("recherche_web", {"requete": "x"}), stop="tool_use")
        create = Scripted(first, reply(text("ok")))
        complete_with_tools(create, self.base_kwargs(), self.router)
        self.assertIs(create.calls[1]["messages"][1]["content"][0], thinking)

    def test_service_en_panne_est_signale_a_claude(self):
        self.search.return_value = ToolResult.error("Recherche web injoignable pour le moment.")
        create = Scripted(reply(tool_use("recherche_web", {"requete": "x"}), stop="tool_use"), reply(text("Je n'ai pas pu chercher.")))
        complete_with_tools(create, self.base_kwargs(), self.router)
        block = create.calls[1]["messages"][2]["content"][0]
        self.assertTrue(block["is_error"])
        self.assertIn("injoignable", block["content"])

    def test_plusieurs_appels_dans_un_tour_sont_plafonnes(self):
        calls = [tool_use("recherche_web", {"requete": str(i)}, call_id=f"tu_{i}") for i in range(5)]
        create = Scripted(reply(*calls, stop="tool_use"), reply(text("fin")))
        complete_with_tools(create, self.base_kwargs(), self.router)
        self.assertEqual(self.search.call_count, tool_router.MAX_TOOL_CALLS_PER_ROUND)
        blocks = create.calls[1]["messages"][2]["content"]
        self.assertEqual(len(blocks), 5)   # chaque appel de Claude reçoit une réponse, sinon l'API refuse
        self.assertTrue(blocks[-1]["is_error"])

    def test_boucle_infinie_impossible(self):
        endless = [reply(tool_use("recherche_web", {"requete": "x"}, call_id=f"tu_{i}"), stop="tool_use") for i in range(20)]
        create = Scripted(*endless)
        complete_with_tools(create, self.base_kwargs(), self.router)
        self.assertEqual(len(create.calls), tool_router.MAX_TOOL_ROUNDS + 2)
        self.assertEqual(self.search.call_count, tool_router.MAX_TOOL_ROUNDS)

    def test_apres_la_limite_claude_recoit_une_erreur_puis_peut_repondre(self):
        responses = [reply(tool_use("recherche_web", {"requete": "x"}, call_id=f"tu_{i}"), stop="tool_use") for i in range(tool_router.MAX_TOOL_ROUNDS + 1)]
        responses.append(reply(text("Réponse finale avec ce que j'ai")))
        create = Scripted(*responses)
        final = complete_with_tools(create, self.base_kwargs(), self.router)
        self.assertEqual(final.content[0].text, "Réponse finale avec ce que j'ai")
        last_results = create.calls[-1]["messages"][-1]["content"]
        self.assertTrue(last_results[0]["is_error"])
        self.assertIn("Limite", last_results[0]["content"])


if __name__ == "__main__":
    unittest.main()
