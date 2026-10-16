"""Tests du bot CHARYAM, sans réseau : Telegram et Claude sont simulés.

Lancement : python -m unittest test_charyam_telegram
"""

import io
import os
import unittest
import zipfile
from types import SimpleNamespace

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:TEST")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test")

import httpx

TELEGRAM_CALLS = []
DOCUMENT_CALLS = []
DOCUMENT_RESPONSE = {"ok": True, "result": {}}


class _FakeResponse:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def _fake_post(self, url, json=None, data=None, files=None, **kwargs):
    method = url.rsplit("/", 1)[-1]
    if files:  # envoi de fichier (multipart) : on garde le contenu pour le relire
        DOCUMENT_CALLS.append({"method": method, "data": data, "files": files})
        return _FakeResponse(dict(DOCUMENT_RESPONSE))
    TELEGRAM_CALLS.append((method, json or {}))
    return _FakeResponse({"ok": True, "result": {"url": ""}})


# Avant l'import : le module configure le webhook dès son chargement
httpx.Client.post = _fake_post

import charyam_telegram as bot  # noqa: E402

CLAUDE_CALLS = []
CLAUDE_REPLY = {"text": "Réponse de test", "stop_reason": "end_turn"}


def _fake_create(**kwargs):
    CLAUDE_CALLS.append(kwargs)
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=CLAUDE_REPLY["text"])],
        stop_reason=CLAUDE_REPLY["stop_reason"],
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
        DOCUMENT_CALLS.clear()
        DOCUMENT_RESPONSE.clear()
        DOCUMENT_RESPONSE.update({"ok": True, "result": {}})
        CLAUDE_REPLY.update({"text": "Réponse de test", "stop_reason": "end_turn"})
        bot.chat_memory.clear()
        bot.chat_modes.clear()
        bot.last_answers.clear()
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
        # Claude n'est jamais appelé ; l'inconnu reçoit seulement un court message avec son identifiant
        self.assertEqual(len(self.sent()), 1)
        self.assertIn("identifiant Telegram est 1", self.sent()[0]["text"])

    def test_start_affiche_le_menu_sans_claude(self):
        self.post("/start")
        self.assertEqual(CLAUDE_CALLS, [])
        message = self.sent()[-1]
        self.assertEqual(message["reply_markup"], bot.MENU_KEYBOARD)
        self.assertIn(bot.BUTTON_EMPLOI, message["text"])

    def test_commande_avec_nom_du_bot(self):
        self.post("/menu@CharyamBot")
        self.assertEqual(self.sent()[-1]["reply_markup"], bot.MENU_KEYBOARD)

    def test_commande_inconnue_repond_sans_appeler_claude(self):
        self.post("/inconnue")
        self.assertEqual(CLAUDE_CALLS, [])                       # aucun coût Claude
        self.assertEqual(len(self.sent()), 1)
        self.assertIn("Commande inconnue", self.sent()[0]["text"])
        self.assertEqual(self.sent()[0]["reply_markup"], bot.MENU_KEYBOARD)

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

    # ------------------------------------------------------------ export Word / PDF

    LETTRE = "# Lettre de motivation\n\nBonjour **Madame**, œuvre & café.\n\n- Point un\n- Point deux"

    def ask(self, chat_id=42):
        CLAUDE_REPLY["text"] = self.LETTRE
        self.post("Écris une lettre", chat_id=chat_id)

    def test_clavier_contient_les_boutons_export(self):
        texts = [b["text"] for row in bot.MENU_KEYBOARD["keyboard"] for b in row]
        self.assertIn(bot.BUTTON_WORD, texts)
        self.assertIn(bot.BUTTON_PDF, texts)

    def test_export_word_apres_une_reponse(self):
        self.ask()
        self.post(bot.BUTTON_WORD)
        self.assertEqual(len(CLAUDE_CALLS), 1)  # le bouton n'appelle pas Claude
        self.assertEqual(len(DOCUMENT_CALLS), 1)
        call = DOCUMENT_CALLS[0]
        self.assertEqual(call["method"], "sendDocument")
        self.assertEqual(call["data"]["chat_id"], "42")
        name, content, mime = call["files"]["document"]
        self.assertTrue(name.startswith("Document_CHARYAM_") and name.endswith(".docx"))
        self.assertEqual(mime, bot.EXPORT_MIME["docx"])
        xml = zipfile.ZipFile(io.BytesIO(content)).read("word/document.xml").decode("utf-8")
        for needle in ("Lettre de motivation", "Bonjour Madame", "œuvre &amp; café", "Point deux"):
            self.assertIn(needle, xml)
        self.assertNotIn("**", xml)

    def test_export_pdf_apres_une_reponse(self):
        self.ask()
        self.post(bot.BUTTON_PDF)
        name, content, mime = DOCUMENT_CALLS[0]["files"]["document"]
        self.assertTrue(name.endswith(".pdf"))
        self.assertEqual(mime, "application/pdf")
        self.assertTrue(content.startswith(b"%PDF"))

    def test_nom_du_fichier_selon_l_activite(self):
        self.post(bot.BUTTON_DOCUMENTS)
        self.ask()
        self.post(bot.BUTTON_WORD)
        self.assertTrue(DOCUMENT_CALLS[0]["files"]["document"][0].startswith("Document_professionnel_"))

    def test_export_sans_reponse_recente(self):
        self.post(bot.BUTTON_PDF)
        self.assertEqual(DOCUMENT_CALLS, [])
        self.assertEqual(CLAUDE_CALLS, [])
        self.assertIn("pas de réponse récente", self.sent()[-1]["text"])

    def test_export_isole_par_conversation(self):
        CLAUDE_REPLY["text"] = "SECRET-DU-CHAT-1"
        self.post("question", chat_id=1)
        self.post(bot.BUTTON_WORD, chat_id=2)
        self.assertEqual(DOCUMENT_CALLS, [])
        self.post(bot.BUTTON_WORD, chat_id=1)
        content = DOCUMENT_CALLS[0]["files"]["document"][1]
        self.assertIn(b"SECRET-DU-CHAT-1", zipfile.ZipFile(io.BytesIO(content)).read("word/document.xml"))

    def test_reponse_tronquee_signalee_dans_le_fichier(self):
        CLAUDE_REPLY.update({"text": "Début du rapport", "stop_reason": "max_tokens"})
        self.post("rapport")
        self.post(bot.BUTTON_WORD)
        xml = zipfile.ZipFile(io.BytesIO(DOCUMENT_CALLS[0]["files"]["document"][1])).read("word/document.xml")
        self.assertIn("Réponse incomplète".encode("utf-8"), xml)

    def test_echec_de_creation_du_fichier(self):
        self.ask()
        original = bot.charyam_docs.pdf_bytes
        bot.charyam_docs.pdf_bytes = lambda *a, **k: (_ for _ in ()).throw(ImportError("reportlab absent"))
        try:
            self.post(bot.BUTTON_PDF)
        finally:
            bot.charyam_docs.pdf_bytes = original
        self.assertEqual(DOCUMENT_CALLS, [])
        text = self.sent()[-1]["text"]
        self.assertIn("pas pu créer", text)
        self.assertIn("Word", text)  # propose l'autre format

    def test_telegram_refuse_le_fichier(self):
        self.ask()
        DOCUMENT_RESPONSE.update({"ok": False, "description": "Bad Request"})
        self.post(bot.BUTTON_WORD)
        text = self.sent()[-1]["text"]
        self.assertIn("envoi a échoué", text)
        self.assertNotIn("envoyé", text)

    def test_les_consignes_ne_promettent_plus_de_fichier_direct(self):
        self.post(bot.BUTTON_DOCUMENTS)
        self.post("devis")
        system = CLAUDE_CALLS[-1]["system"]
        self.assertNotIn("tu ne peux pas encore envoyer", system)
        self.assertIn("n'affirme jamais l'avoir fait", system)


if __name__ == "__main__":
    unittest.main()
