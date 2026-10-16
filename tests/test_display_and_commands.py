"""Tests SIMULÉS : affichage Telegram sans Markdown brut, réponse aux commandes inconnues."""

import unittest

import test_charyam_telegram as base

bot = base.bot

SAMPLE = """# Lettre de motivation

**Objet :** candidature au poste de __chargé__ de clientèle

## Compétences
- Instruction de dossiers
* Animation de groupes
  - Visites terrain

| Poste | Période |
|---|---|
| Crédit | 2019 |

---
Voir `RCCM` et ```code``` ci-dessous.
"""


class DisplayTest(unittest.TestCase):
    def test_plus_d_etoiles_ni_de_dieses(self):
        out = bot.telegram_display(SAMPLE)
        for raw in ("**", "##", "__", "`", "|---"):
            self.assertNotIn(raw, out)
        self.assertNotIn("# ", out)

    def test_contenu_conserve(self):
        out = bot.telegram_display(SAMPLE)
        self.assertIn("LETTRE DE MOTIVATION", out)
        self.assertIn("Objet : candidature au poste de chargé de clientèle", out)
        self.assertIn("Compétences", out)
        self.assertIn("• Instruction de dossiers", out)
        self.assertIn("• Animation de groupes", out)
        self.assertIn("• Visites terrain", out)
        self.assertIn("Poste | Période", out)
        self.assertIn("Crédit | 2019", out)

    def test_texte_simple_inchange(self):
        self.assertEqual(bot.telegram_display("Bonjour Karim, 2 * 3 = 6."), "Bonjour Karim, 2 * 3 = 6.")

    def test_vide(self):
        self.assertEqual(bot.telegram_display(""), "")
        self.assertEqual(bot.telegram_display(None), "")


class WebhookDisplayTest(unittest.TestCase):
    def setUp(self):
        base.TELEGRAM_CALLS.clear()
        bot.chat_memory.clear()
        bot.chat_modes.clear()
        self.client = bot.app.test_client()

    def sent(self):
        return [p["text"] for m, p in base.TELEGRAM_CALLS if m == "sendMessage"]

    def test_reponse_claude_envoyee_sans_markdown_mais_export_garde_le_markdown(self):
        from unittest import mock
        from tests.test_router import Scripted, reply, text
        script = Scripted(reply(text("# Titre\n\n**Gras** et\n- point")))
        with mock.patch.object(bot.client.messages, "create", script):
            self.client.post("/webhook", json=base._update("écris"))
        sent = self.sent()[-1]
        self.assertNotIn("**", sent)
        self.assertNotIn("# ", sent)
        self.assertIn("TITRE", sent)
        self.assertIn("• point", sent)
        # la version d'origine (avec Markdown) reste disponible pour Word/PDF
        self.assertIn("# Titre", bot.last_answers[42][0] if isinstance(bot.last_answers[42], tuple) else str(bot.last_answers[42]))

    def test_commande_inconnue_recoit_une_reponse_et_le_menu(self):
        self.client.post("/webhook", json=base._update("/test secret-argument"))
        calls = [(m, p) for m, p in base.TELEGRAM_CALLS if m == "sendMessage"]
        self.assertTrue(calls)
        payload = calls[-1][1]
        self.assertIn("Commande inconnue", payload["text"])
        self.assertNotIn("secret-argument", payload["text"])
        self.assertIn("keyboard", payload["reply_markup"])

    def test_commande_inconnue_d_un_autre_utilisateur_reste_ignoree(self):
        self.client.post("/webhook", json=base._update("/test", user_id=1))
        self.assertEqual(self.sent(), [])


if __name__ == "__main__":
    unittest.main()
