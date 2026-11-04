"""Tests SIMULÉS de l'accueil à trois volets : CV et lettre, ebooks (Chariow), boutique (WhatsApp)."""

import unittest

import test_charyam_telegram as base
import boutique
import cv_questionnaire as cq
import payments

bot = base.bot

OWNER = bot.USER_ID
CLIENT = 777000333
INSTRUCTIONS = "CV 1000 FCFA. Wave 0100000000."
SHOP = "https://wa.me/c/2250000000000"


class UrlTest(unittest.TestCase):
    def test_liens_valides(self):
        for url in ("https://zsykaits.mychariow.shop", "  https://wa.me/c/225  "):
            self.assertTrue(boutique.clean_url(url), url)

    def test_liens_refuses(self):
        for url in ("", None, "   ", "http://exemple.com", "wa.me/c/225", "javascript:alert(1)",
                    "https://", "https://a b.com", "https://localhost", "ftp://x.com"):
            self.assertEqual(boutique.clean_url(url), "", url)

    def test_lien_par_defaut_est_celui_de_la_boutique_chariow(self):
        self.assertEqual(boutique.clean_url(boutique.DEFAULT_CHARIOW_URL), boutique.DEFAULT_CHARIOW_URL)

    def test_pas_de_bouton_sans_lien(self):
        self.assertIsNone(boutique.link_keyboard("x", ""))
        self.assertIsNone(boutique.link_keyboard("x", "http://non-securise.com"))


class VoletsTest(unittest.TestCase):
    def setUp(self):
        base.TELEGRAM_CALLS.clear()
        base.CLAUDE_CALLS.clear()
        bot.chat_memory.clear()
        bot.chat_modes.clear()
        bot.cv_flow = cq.CvFlow()
        self.old_gate = bot.payment_gate
        self.old_wa = bot.WHATSAPP_SHOP_URL
        self.old_ch = bot.CHARIOW_STORE_URL
        bot.payment_gate = payments.PaymentGate(OWNER, INSTRUCTIONS)
        bot.WHATSAPP_SHOP_URL = SHOP
        bot.CHARIOW_STORE_URL = boutique.DEFAULT_CHARIOW_URL

    def tearDown(self):
        bot.payment_gate = self.old_gate
        bot.WHATSAPP_SHOP_URL = self.old_wa
        bot.CHARIOW_STORE_URL = self.old_ch

    def messages(self):
        return [p for m, p in base.TELEGRAM_CALLS if m == "sendMessage" and p.get("chat_id") == CLIENT]

    def test_clavier_a_les_trois_volets_en_premiere_ligne(self):
        first_row = [b["text"] for b in bot.MENU_KEYBOARD["keyboard"][0]]
        self.assertEqual(first_row, [bot.BUTTON_VOLET_CV, bot.BUTTON_EBOOKS, bot.BUTTON_BOUTIQUE])

    def test_bonjour_presente_les_trois_volets(self):
        bot.process_message(CLIENT, CLIENT, "Bonjour", 1)
        self.assertEqual(base.CLAUDE_CALLS, [])
        sent = self.messages()
        self.assertEqual(len(sent), 1)
        text = sent[0]["text"]
        for word in ("CV et lettre", "Ebooks", "savon bio", "WhatsApp", INSTRUCTIONS):
            self.assertIn(word, text)
        for label in (bot.BUTTON_VOLET_CV, bot.BUTTON_EBOOKS, bot.BUTTON_BOUTIQUE):
            self.assertIn(label, text)
        self.assertEqual(sent[0]["reply_markup"], bot.MENU_KEYBOARD)
        self.assertLess(len(text), 4000)

    def test_volet_cv(self):
        bot.process_message(CLIENT, CLIENT, bot.BUTTON_VOLET_CV, 1)
        self.assertEqual(base.CLAUDE_CALLS, [])
        sent = self.messages()[0]
        self.assertIn(INSTRUCTIONS, sent["text"])
        for label in (bot.BUTTON_CV, bot.BUTTON_LETTRE, bot.BUTTON_TARIFS):
            self.assertIn(label, sent["text"])
        self.assertEqual(sent["reply_markup"], bot.MENU_KEYBOARD)

    def test_volet_ebooks_redirige_vers_chariow(self):
        bot.process_message(CLIENT, CLIENT, bot.BUTTON_EBOOKS, 1)
        self.assertEqual(base.CLAUDE_CALLS, [])
        sent = self.messages()[0]
        button = sent["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(button["url"], boutique.DEFAULT_CHARIOW_URL)
        self.assertIn("pas dans ce chat", sent["text"])

    def test_volet_boutique_redirige_vers_whatsapp(self):
        bot.process_message(CLIENT, CLIENT, bot.BUTTON_BOUTIQUE, 1)
        self.assertEqual(base.CLAUDE_CALLS, [])
        sent = self.messages()[0]
        self.assertEqual(sent["reply_markup"]["inline_keyboard"][0][0]["url"], SHOP)
        self.assertIn("savon bio", sent["text"])

    def test_boutique_sans_lien_ne_ment_pas(self):
        bot.WHATSAPP_SHOP_URL = ""
        bot.process_message(CLIENT, CLIENT, bot.BUTTON_BOUTIQUE, 1)
        sent = self.messages()[0]
        self.assertIn("arrive bientôt", sent["text"])
        self.assertEqual(sent["reply_markup"], bot.MENU_KEYBOARD)           # aucun faux bouton

    def test_ebooks_sans_lien(self):
        bot.CHARIOW_STORE_URL = ""
        bot.process_message(CLIENT, CLIENT, bot.BUTTON_EBOOKS, 1)
        sent = self.messages()[0]
        self.assertEqual(sent["reply_markup"], bot.MENU_KEYBOARD)
        self.assertIn("pas joignable", sent["text"])

    def test_les_volets_ne_sont_pas_des_reponses_au_questionnaire(self):
        bot.process_message(CLIENT, CLIENT, "/cv", 1)
        for button in (bot.BUTTON_VOLET_CV, bot.BUTTON_EBOOKS, bot.BUTTON_BOUTIQUE):
            self.assertIsNone(bot.handle_cv_flow(CLIENT, button))
        self.assertIn("Étape 1 sur 8", bot.cv_flow.start(CLIENT))             # l'étape 1 n'a pas bougé

    def test_menu_liste_les_volets(self):
        bot.process_message(CLIENT, CLIENT, "/menu", 1)
        text = self.messages()[0]["text"]
        for label in (bot.BUTTON_VOLET_CV, bot.BUTTON_EBOOKS, bot.BUTTON_BOUTIQUE):
            self.assertIn(label, text)


if __name__ == "__main__":
    unittest.main()
