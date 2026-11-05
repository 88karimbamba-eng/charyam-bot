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

    def test_volet_ebooks_presente_chaque_ebook_avec_son_lien(self):
        bot.process_message(CLIENT, CLIENT, bot.BUTTON_EBOOKS, 1)
        self.assertEqual(base.CLAUDE_CALLS, [])
        sent = self.messages()[0]
        rows = sent["reply_markup"]["inline_keyboard"]
        urls = [row[0]["url"] for row in rows]
        self.assertEqual(urls[:-1], [e["url"] for e in boutique.EBOOKS])      # un bouton par ebook, dans l'ordre
        self.assertEqual(urls[-1], boutique.DEFAULT_CHARIOW_URL)               # puis la boutique complète
        for item in boutique.EBOOKS:
            self.assertIn(item["titre"], sent["text"])
            self.assertIn(item["description"], sent["text"])
        self.assertIn("pas dans ce chat", sent["text"])
        self.assertLess(len(sent["text"]), 4000)

    def test_volet_ebooks_n_affiche_aucun_prix(self):
        bot.process_message(CLIENT, CLIENT, bot.BUTTON_EBOOKS, 1)
        self.assertNotIn("FCFA", self.messages()[0]["text"])

    def test_ebooks_tous_les_liens_sont_des_https_complets(self):
        self.assertEqual(len(boutique.valid_ebooks()), len(boutique.EBOOKS))
        for item in boutique.EBOOKS:
            self.assertTrue(item["url"].startswith("https://zsykaits.mychariow.shop/prd_"), item["url"])

    def test_ebooks_correspondent_aux_produits_publies_sur_chariow(self):
        # Identifiants relevés sur la boutique (API + page publique) le 5 nov. 2026. À mettre à jour si la boutique change.
        publies = {"prd_6rbfwkby", "prd_5g9kzjwv", "prd_0bvt51bh", "prd_2j838h8j"}
        ids = {item["url"].rsplit("/", 1)[-1] for item in boutique.EBOOKS}
        self.assertEqual(ids, publies)

    def test_ebook_au_lien_invalide_est_ignore(self):
        old = boutique.EBOOKS
        boutique.EBOOKS = old + ({"titre": "Faux", "bouton": "5️⃣ Faux", "description": "x", "url": "http://non-securise.com"},)
        try:
            bot.process_message(CLIENT, CLIENT, bot.BUTTON_EBOOKS, 1)
            sent = self.messages()[0]
        finally:
            boutique.EBOOKS = old
        self.assertNotIn("Faux", sent["text"])
        self.assertEqual(len(sent["reply_markup"]["inline_keyboard"]), len(old) + 1)

    def test_ebooks_sans_lien_de_boutique_garde_les_fiches(self):
        bot.CHARIOW_STORE_URL = ""
        bot.process_message(CLIENT, CLIENT, bot.BUTTON_EBOOKS, 1)
        rows = self.messages()[0]["reply_markup"]["inline_keyboard"]
        self.assertEqual(len(rows), len(boutique.EBOOKS))                      # pas de bouton « boutique » inventé

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

    def test_ebooks_sans_aucun_lien(self):
        bot.CHARIOW_STORE_URL = ""
        old = boutique.EBOOKS
        boutique.EBOOKS = ()
        try:
            bot.process_message(CLIENT, CLIENT, bot.BUTTON_EBOOKS, 1)
            sent = self.messages()[0]
        finally:
            boutique.EBOOKS = old
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
