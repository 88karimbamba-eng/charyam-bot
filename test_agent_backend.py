"""Tests du pont vers les agents managés, sans réseau : le client Anthropic est simulé.

Lancement : python -m unittest test_agent_backend
"""

import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:TEST")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test")

import httpx

def _fake_post_agent_tests(self, url, json=None, **kw):
    return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": {"url": ""}})


# Ne jamais écraser le faux Telegram de test_charyam_telegram s'il est déjà installé
# (sinon ses tests échouent quand les deux fichiers tournent dans le même processus).
if not getattr(httpx.Client.post, "__name__", "").startswith("_fake_post"):
    httpx.Client.post = _fake_post_agent_tests

import agent_backend as ab  # noqa: E402
import charyam_telegram as bot  # noqa: E402


def msg(event_id, text):
    return SimpleNamespace(id=event_id, type="agent.message", content=[SimpleNamespace(type="text", text=text)])


def idle(event_id, stop="end_turn"):
    return SimpleNamespace(id=event_id, type="session.status_idle", stop_reason=SimpleNamespace(type=stop))


class _Stream:
    def __init__(self, events):
        self.events = events

    def __enter__(self):
        return iter(self.events)

    def __exit__(self, *args):
        return False


class FakeClient:
    """Simule client.beta.sessions : crée des sessions et rejoue des événements programmés"""

    def __init__(self):
        self.script = []        # liste de listes d'événements, une par tour
        self.created = 0
        self.sent = []
        self.history = []
        sessions = SimpleNamespace(create=self._create, events=SimpleNamespace(
            stream=self._stream, send=self._send, list=self._list))
        self.beta = SimpleNamespace(sessions=sessions)

    def _create(self, **kwargs):
        self.created += 1
        self.create_kwargs = kwargs
        return SimpleNamespace(id=f"sess_{self.created}")

    def _stream(self, session_id):
        return _Stream(self.script.pop(0))

    def _send(self, session_id, events):
        self.sent.append((session_id, events))

    def _list(self, session_id):
        return SimpleNamespace(data=self.history)


class AgentBackendTest(unittest.TestCase):
    def setUp(self):
        ab.ENABLED, ab.AGENT_ID, ab.ENVIRONMENT_ID = True, "agent_1", "env_1"
        ab.AGENT_MODES = {"emploi"}
        ab._sessions.clear()
        self.fake = FakeClient()
        ab._client = self.fake

    def tearDown(self):
        ab._client = None
        ab.ENABLED = False

    def test_reponse_concatenee_et_session_creee(self):
        self.fake.script = [[msg("e1", "Bonjour "), msg("e2", "Karim"), idle("e3")]]
        out = ab.ask_agent(1, "emploi", "salut", "Karim")
        self.assertEqual(out, "Bonjour Karim")
        self.assertEqual(self.fake.create_kwargs["agent"], "agent_1")
        self.assertEqual(self.fake.create_kwargs["environment_id"], "env_1")
        sent_text = self.fake.sent[0][1][0]["content"][0]["text"]
        self.assertIn("Karim", sent_text)          # contexte du prénom à la 1re requête
        self.assertTrue(sent_text.endswith("salut"))

    def test_session_reutilisee_et_anciens_evenements_ignores(self):
        self.fake.script = [[msg("e1", "un"), idle("e2")], [msg("e1", "un"), msg("e4", "deux"), idle("e5")]]
        ab.ask_agent(1, "emploi", "a")
        self.fake.history = [SimpleNamespace(id="e1"), SimpleNamespace(id="e2")]
        out = ab.ask_agent(1, "emploi", "b")
        self.assertEqual(self.fake.created, 1)     # pas de nouvelle session
        self.assertEqual(out, "deux")              # e1 (ancien) ignoré
        self.assertNotIn("Contexte", self.fake.sent[1][1][0]["content"][0]["text"])

    def test_erreur_de_session_leve_une_erreur(self):
        err = SimpleNamespace(id="e1", type="session.error", error=SimpleNamespace(message="boom"))
        self.fake.script = [[err]]
        with self.assertRaises(ab.AgentBackendError):
            ab.ask_agent(1, "emploi", "x")

    def test_requires_action_bloque_et_oublie_la_session(self):
        self.fake.script = [[idle("e1", "requires_action")]]
        with self.assertRaises(ab.AgentBackendError):
            ab.ask_agent(1, "emploi", "x")
        self.assertNotIn((1, "emploi"), ab._sessions)

    def test_reponse_vide_est_une_erreur(self):
        self.fake.script = [[idle("e1")]]
        with self.assertRaises(ab.AgentBackendError):
            ab.ask_agent(1, "emploi", "x")

    def test_routage_desactive_par_defaut_et_par_activite(self):
        self.assertTrue(ab.should_use_agent("emploi"))
        self.assertFalse(ab.should_use_agent("documents"))
        self.assertFalse(ab.should_use_agent(None))
        ab.ENABLED = False
        self.assertFalse(ab.should_use_agent("emploi"))
        ab.ENABLED, ab.AGENT_ID = True, ""
        self.assertFalse(ab.should_use_agent("emploi"))


class BotRoutingTest(unittest.TestCase):
    """Intégration avec get_claude_response : agent utilisé, ou repli sur l'API Messages"""

    def setUp(self):
        ab.ENABLED, ab.AGENT_ID, ab.ENVIRONMENT_ID = True, "agent_1", "env_1"
        ab.AGENT_MODES = {"emploi"}
        ab._sessions.clear()
        bot.chat_memory.clear()
        bot.chat_modes.clear()
        bot.chat_modes[9] = bot.MODE_EMPLOI
        self.api_calls = []

        def fake_create(**kwargs):
            self.api_calls.append(kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="repli Messages")],
                stop_reason="end_turn", model="m", usage=SimpleNamespace(input_tokens=1, output_tokens=1))

        self._orig = bot.client.messages.create
        bot.client.messages.create = fake_create
        self.fake = FakeClient()
        ab._client = self.fake

    def tearDown(self):
        bot.client.messages.create = self._orig
        ab._client = None
        ab.ENABLED = False

    def test_agent_utilise_sans_appel_messages(self):
        self.fake.script = [[msg("e1", "réponse agent"), idle("e2")]]
        out = bot.get_claude_response(5608719781, 9, "analyse cette offre", "Karim")
        self.assertEqual(out, "réponse agent")
        self.assertEqual(self.api_calls, [])
        self.assertEqual(bot.chat_memory[9][-1], {"role": "assistant", "content": "réponse agent"})

    def test_repli_sur_messages_si_agent_en_erreur(self):
        err = SimpleNamespace(id="e1", type="session.error", error=SimpleNamespace(message="boom"))
        self.fake.script = [[err]]
        out = bot.get_claude_response(5608719781, 9, "analyse cette offre", "Karim")
        self.assertEqual(out, "repli Messages")
        self.assertEqual(len(self.api_calls), 1)

    def test_autre_activite_ne_passe_pas_par_l_agent(self):
        bot.chat_modes[9] = bot.MODE_DOCUMENTS
        out = bot.get_claude_response(5608719781, 9, "fais un devis", "Karim")
        self.assertEqual(out, "repli Messages")
        self.assertEqual(self.fake.created, 0)


if __name__ == "__main__":
    unittest.main()
