"""Tests du bot CHARYAM, sans réseau : Telegram et Claude sont simulés.

Lancement : python -m unittest test_charyam_telegram
"""

import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:TEST")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test")

import httpx

TELEGRAM_CALLS = []


class _FakeResponse:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def _fake_post(self, url, json=None, **kwargs):
    TELEGRAM_CALLS.append((url.rsplit("/", 1)[-1], json or {}))
    return _FakeResponse({"ok": True, "result": {"url": ""}})


# Avant l'import : le module configure le webhook dès son chargement
httpx.Client.post = _fake_post

import charyam_telegram as bot  # noqa: E402

CLAUDE_CALLS = []


def _fake_create(**kwargs):
    CLAUDE_CALLS.append(kwargs)
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text="Réponse de test")],
        stop_reason="end_turn",
        model=kwargs["model"],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )


bot.client.messages.create = _fake_create


class _ImmediateExecutor:
    """Exécute le traitement tout de suite, pour des tests déterministes"""

    def submit(self, fn, *args):
        fn(*args)


bot.executor = _ImmediateExecutor()


def _update(text, user_id=bot.USER_ID, chat_id=42, update_id=None):
    _update.counter += 1
    return {
        "update_id": update_id or _update.counter,
        "message": {"message_id": 7, "from": {"id": user_id}, "chat": {"id": chat_id}, "text": text},
    }


_update.counter = 1000


class BotTest(unittest.TestCase):
    def setUp(self):
        TELEGRAM_CALLS.clear()
        CLAUDE_CALLS.clear()
        bot.chat_memory.clear()
        bot.chat_modes.clear()
        self.client = bot.app.test_client()

    def post(self, text, **kwargs):
        return self.client.post("/webhook", json=_update(text, **kwargs))

    def sent(self):
        return [payload for method, payload in TELEGRAM_CALLS if method == "sendMessage"]

    def test_health(self):
        self.assertEqual(self.client.get("/health").get_json(), {"status": "ok"})

    def test_message_normal_appelle_claude(self):
        self.post("bonjour")
        self.assertEqual(len(CLAUDE_CALLS), 1)
        self.assertNotIn("Activité en cours", CLAUDE_CALLS[0]["system"])
        self.assertEqual(self.sent()[-1]["text"], "Réponse de test")

    def test_utilisateur_non_autorise_ignore(self):
        self.post("bonjour", user_id=1)
        self.assertEqual(CLAUDE_CALLS, [])
        self.assertEqual(self.sent(), [])

    def test_start_affiche_le_menu_sans_claude(self):
        self.post("/start")
        self.assertEqual(CLAUDE_CALLS, [])
        message = self.sent()[-1]
        self.assertEqual(message["reply_markup"], bot.MENU_KEYBOARD)
        self.assertIn(bot.BUTTON_EMPLOI, message["text"])

    def test_commande_avec_nom_du_bot(self):
        self.post("/menu@CharyamBot")
        self.assertEqual(self.sent()[-1]["reply_markup"], bot.MENU_KEYBOARD)

    def test_commande_inconnue_ignoree(self):
        self.post("/inconnue")
        self.assertEqual(CLAUDE_CALLS, [])
        self.assertEqual(self.sent(), [])

    def test_aide(self):
        self.post(bot.BUTTON_AIDE)
        self.assertEqual(CLAUDE_CALLS, [])
        self.assertIn("/menu", self.sent()[-1]["text"])

    def test_bouton_emploi_puis_question(self):
        self.post(bot.BUTTON_EMPLOI)
        self.assertEqual(CLAUDE_CALLS, [])
        self.assertEqual(bot.chat_modes[42], bot.MODE_EMPLOI)
        self.post("Analyse cette offre")
        system = CLAUDE_CALLS[-1]["system"]
        self.assertIn("Emploi & Candidatures", system)
        self.assertIn("Hypothèses", system)
        # Le libellé du bouton n'entre pas dans l'historique envoyé à Claude
        self.assertEqual(CLAUDE_CALLS[-1]["messages"], [{"role": "user", "content": "Analyse cette offre"}])

    def test_bouton_documents(self):
        self.post(bot.BUTTON_DOCUMENTS)
        self.post("Fais un devis")
        self.assertIn("[à compléter]", CLAUDE_CALLS[-1]["system"])

    def test_activites_separees_par_chat(self):
        self.post(bot.BUTTON_EMPLOI, chat_id=1)
        self.post("question", chat_id=2)
        self.assertNotIn("Activité en cours", CLAUDE_CALLS[-1]["system"])

    def test_clavier_sur_derniere_partie_seulement(self):
        TELEGRAM_CALLS.clear()
        bot.send_telegram_message(42, "a" * 5000, reply_markup=bot.MENU_KEYBOARD)
        parts = self.sent()
        self.assertEqual(len(parts), 2)
        self.assertNotIn("reply_markup", parts[0])
        self.assertIn("reply_markup", parts[1])

    def test_message_espaces_seulement(self):
        response = self.post("   ")
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
