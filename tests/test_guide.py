"""Tests SIMULÉS du parcours guidé : accueil après « bonjour », étapes jusqu'au paiement."""

import unittest

import test_charyam_telegram as base
import cv_questionnaire as cq
import guide
import payments

bot = base.bot

OWNER = bot.USER_ID
CLIENT = 777000222
INSTRUCTIONS = "CV 1000 FCFA. Wave 0100000000."


class GreetingTest(unittest.TestCase):
    def test_salutations_courtes(self):
        for text in ("Bonjour", "bonjour !", "BONSOIR", "Salut", "Bonsoir à tous", "Bonjour tout le monde",
                     "Salut, ça va ?", "Hello", "Coucou", "Bonne journée", "  bonjour  ", "Bonjour 👋",
                     "Assalamou alaykoum"):
            self.assertTrue(guide.is_greeting(text), text)

    def test_ce_qui_n_est_pas_une_salutation(self):
        for text in ("", "   ", "CV", "Je veux un CV", "Bonjour je veux un CV pour comptable",
                     "Bonjour CV", "Quel est le prix ?", "/menu", "Merci", "Étape suivante",
                     "Bonjour, voici mon poste : chargé de clientèle"):
            self.assertFalse(guide.is_greeting(text), text)

    def test_sans_prix_invente(self):
        text = guide.welcome_text("", "A", "B", "C", "D")
        self.assertNotIn("FCFA", text)
        self.assertIn("administrateur", text)


class JourneyTest(unittest.TestCase):
    def setUp(self):
        base.TELEGRAM_CALLS.clear()
        base.CLAUDE_CALLS.clear()
        base.DOCUMENT_CALLS.clear()
        bot.chat_memory.clear()
        bot.chat_modes.clear()
        bot.last_answers.clear()
        bot.cv_flow = cq.CvFlow()
        self.old_gate = bot.payment_gate
        bot.payment_gate = payments.PaymentGate(OWNER, INSTRUCTIONS)

    def tearDown(self):
        bot.payment_gate = self.old_gate

    def messages(self, chat=CLIENT):
        return [p for m, p in base.TELEGRAM_CALLS if m == "sendMessage" and p.get("chat_id") == chat]

    def test_bonjour_accueille_sans_appeler_claude(self):
        bot.process_message(CLIENT, CLIENT, "Bonjour", 1)
        self.assertEqual(base.CLAUDE_CALLS, [])
        sent = self.messages()
        self.assertEqual(len(sent), 1)
        text = sent[0]["text"]
        self.assertIn("Bienvenue", text)
        self.assertIn("4 étapes", text)
        self.assertIn(INSTRUCTIONS, text)                       # tarifs officiels, tels quels
        for label in (bot.BUTTON_CV, bot.BUTTON_LETTRE, bot.BUTTON_EMPLOI, bot.BUTTON_DOCUMENTS):
            self.assertIn(label, text)
        self.assertEqual(sent[0]["reply_markup"], bot.MENU_KEYBOARD)

    def test_start_donne_le_meme_accueil(self):
        bot.process_message(CLIENT, CLIENT, "/start", 1)
        self.assertIn("Bienvenue", self.messages()[0]["text"])

    def test_clavier_contient_les_nouveaux_boutons(self):
        labels = [b["text"] for row in bot.MENU_KEYBOARD["keyboard"] for b in row]
        for label in (bot.BUTTON_CV, bot.BUTTON_LETTRE, bot.BUTTON_TARIFS, bot.BUTTON_WORD, bot.BUTTON_PDF):
            self.assertIn(label, labels)

    def test_bouton_tarifs(self):
        bot.process_message(CLIENT, CLIENT, bot.BUTTON_TARIFS, 1)
        self.assertEqual(base.CLAUDE_CALLS, [])
        text = self.messages()[0]["text"]
        self.assertIn(INSTRUCTIONS, text)
        self.assertIn("/paye", text)

    def test_bouton_lettre_guide_en_deux_etapes(self):
        bot.process_message(CLIENT, CLIENT, bot.BUTTON_LETTRE, 1)
        self.assertEqual(base.CLAUDE_CALLS, [])
        self.assertEqual(bot.chat_modes[CLIENT], bot.MODE_EMPLOI)
        text = self.messages()[0]["text"]
        self.assertIn("Étape 1", text)
        self.assertIn("Étape 2", text)

    def test_bouton_cv_demarre_etape_1(self):
        bot.process_message(CLIENT, CLIENT, bot.BUTTON_CV, 1)
        self.assertIn("Étape 1 sur 8", self.messages()[0]["text"])

    def test_bonjour_pendant_le_questionnaire_n_est_pas_une_reponse(self):
        bot.process_message(CLIENT, CLIENT, "/cv", 1)
        bot.process_message(CLIENT, CLIENT, "Chargé de clientèle", 2)       # étape 1 faite
        bot.process_message(CLIENT, CLIENT, "Bonjour", 3)
        text = self.messages()[-1]["text"]
        self.assertIn("Étape 2 sur 8", text)                                # on reprend l'étape 2
        self.assertIn("revoir", text)
        self.assertEqual(base.CLAUDE_CALLS, [])

    def test_boutons_guide_ne_sont_pas_des_reponses(self):
        bot.process_message(CLIENT, CLIENT, "/cv", 1)
        for button in (bot.BUTTON_LETTRE, bot.BUTTON_TARIFS):
            self.assertIsNone(bot.handle_cv_flow(CLIENT, button))
        self.assertTrue(bot.cv_flow.is_active(CLIENT))
        self.assertIn("Étape 1 sur 8", bot.cv_flow.start(CLIENT))            # toujours à l'étape 1

    def test_apres_le_cv_le_client_sait_quoi_faire(self):
        base.CLAUDE_REPLY["text"] = "[[CV]]# Chargé de clientèle\n# Karim BAMBA[[/CV]]"
        try:
            bot.process_message(CLIENT, CLIENT, "/cv", 1)
            for key, _t, _q, _r in cq.STEPS:
                bot.process_message(CLIENT, CLIENT, "Réponse valable pour " + key, 2)
        finally:
            base.CLAUDE_REPLY["text"] = "Réponse de test"
        self.assertEqual(len(base.CLAUDE_CALLS), 1)
        last = self.messages()[-1]
        self.assertIn("Prochaine étape", last["text"])
        self.assertIn(bot.BUTTON_WORD, last["text"])
        self.assertIn(bot.BUTTON_LETTRE, last["text"])
        self.assertEqual(last["reply_markup"], bot.MENU_KEYBOARD)

    def test_pas_d_etape_suivante_sans_cv(self):
        bot.process_message(CLIENT, CLIENT, bot.BUTTON_EMPLOI, 1)
        bot.process_message(CLIENT, CLIENT, "Analyse cette offre", 2)
        self.assertNotIn("Prochaine étape", self.messages()[-1]["text"])

    def test_parcours_complet_jusqu_au_fichier(self):
        bot.last_answers[CLIENT] = "# Titre\n# Nom\n\nTexte."
        bot.process_message(CLIENT, CLIENT, bot.BUTTON_WORD, 1)             # avant paiement : consignes
        text = self.messages()[-1]["text"]
        self.assertIn("3 étapes", text)
        self.assertIn(INSTRUCTIONS, text)
        self.assertIn("/paye", text)
        self.assertEqual(base.DOCUMENT_CALLS, [])
        bot.process_message(CLIENT, CLIENT, "/paye ID42", 2)
        bot.process_message(OWNER, OWNER, f"/valider {CLIENT}", 3)
        bot.process_message(CLIENT, CLIENT, bot.BUTTON_WORD, 4)
        self.assertEqual(len(base.DOCUMENT_CALLS), 1)

    def test_texte_libre_continue_d_aller_a_claude(self):
        bot.process_message(CLIENT, CLIENT, "Quel est le délai pour un CV ?", 1)
        self.assertEqual(len(base.CLAUDE_CALLS), 1)


if __name__ == "__main__":
    unittest.main()
