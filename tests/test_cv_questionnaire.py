"""Tests SIMULÉS du questionnaire « CV intelligent » : étapes, saut, annulation, persistance, intégration au bot."""

import unittest

import test_charyam_telegram as base
import cv_questionnaire as cq
import store as store_module

bot = base.bot

GOOD = {
    "poste": "Chargé de clientèle",
    "identite": "Karim BAMBA, 0102030405, karim@example.com, Abengourou",
    "profil": "Sept ans de crédit agricole.",
    "experience_recente": "Chargé de portefeuille, UNACOOPEC-CI, Abengourou, 2019-2026, 500 producteurs suivis.",
    "experiences_precedentes": "Chef d'équipe, PALMAFRIQUE, 2017-2019.",
    "realisations": "365 millions FCFA de portefeuille.",
    "formation": "BTS Agriculture Tropicale, AIST Toumodi, 2015.",
    "competences": "Instruction de dossiers, Excel, français, anglais intermédiaire.",
}


def run_all(flow, chat_id=1):
    flow.start(chat_id)
    result = None
    for key, _t, _q, _r in cq.STEPS:
        result = flow.answer(chat_id, GOOD[key])
    return result


class FlowTest(unittest.TestCase):
    def test_huit_etapes_une_question_a_la_fois(self):
        flow = cq.CvFlow()
        first = flow.start(1)
        self.assertIn("Étape 1 sur 8", first)
        for i in range(1, cq.TOTAL_STEPS):
            status, text, answers = flow.answer(1, GOOD[cq.STEPS[i - 1][0]])
            self.assertEqual(status, "ask")
            self.assertIn(f"Étape {i + 1} sur 8", text)
            self.assertNotIn(f"Étape {i + 2} sur 8", text)
            self.assertIsNone(answers)

    def test_fin_renvoie_les_reponses_et_efface(self):
        flow = cq.CvFlow()
        status, text, answers = run_all(flow)
        self.assertEqual(status, "done")
        self.assertEqual(answers["poste"], GOOD["poste"])
        self.assertEqual(len(answers), 8)
        self.assertFalse(flow.is_active(1))

    def test_etape_obligatoire_refuse_vide_et_saut(self):
        flow = cq.CvFlow()
        flow.start(1)
        self.assertEqual(flow.answer(1, " ")[0], "refused")
        self.assertEqual(flow.skip(1)[0], "refused")
        self.assertIn("Étape 1 sur 8", flow.start(1))   # toujours à l'étape 1

    def test_saut_etape_facultative(self):
        flow = cq.CvFlow()
        flow.start(1)
        flow.answer(1, GOOD["poste"])
        flow.answer(1, GOOD["identite"])
        status, text, _ = flow.skip(1)          # profil facultatif
        self.assertEqual(status, "ask")
        self.assertIn("Étape 4 sur 8", text)

    def test_reponse_trop_longue_refusee(self):
        flow = cq.CvFlow()
        flow.start(1)
        self.assertEqual(flow.answer(1, "x" * (cq.MAX_ANSWER_CHARS + 1))[0], "refused")

    def test_total_trop_long_refuse(self):
        flow = cq.CvFlow()
        flow.start(1)
        flow.answer(1, "p" * 5000)
        flow.answer(1, "i" * 5000)
        flow.skip(1)
        flow.answer(1, "e" * 5000)
        flow.skip(1)
        flow.skip(1)   # réalisations sautée
        flow.answer(1, "f" * 5000)
        status, text, _ = flow.answer(1, "c" * 5000)
        self.assertEqual(status, "refused")

    def test_annuler_efface(self):
        flow = cq.CvFlow()
        flow.start(1)
        flow.answer(1, GOOD["poste"])
        flow.cancel(1)
        self.assertFalse(flow.is_active(1))
        self.assertEqual(flow.answer(1, "bonjour")[0], "none")

    def test_isolation_entre_chats(self):
        flow = cq.CvFlow()
        flow.start(1)
        flow.start(2)
        flow.answer(1, GOOD["poste"])
        self.assertIn("Étape 2 sur 8", flow.start(1))
        self.assertIn("Étape 1 sur 8", flow.start(2))

    def test_prompt_final_contient_reponses_et_regles(self):
        prompt = cq.build_generation_prompt({"poste": "Comptable"})
        self.assertIn("Comptable", prompt)
        self.assertIn("(non renseigné)", prompt)
        self.assertIn("[[CV]]", prompt)
        self.assertIn("n'invente rien", prompt)


class PersistenceTest(unittest.TestCase):
    def test_reprise_apres_redemarrage(self):
        fake = store_module.FakeStore()
        cq.CvFlow(fake).start(7)
        flow_a = cq.CvFlow(fake)
        flow_a.answer(7, GOOD["poste"])
        flow_b = cq.CvFlow(fake)                     # « redémarrage » : mémoire locale vide
        self.assertTrue(flow_b.is_active(7))
        status, text, _ = flow_b.answer(7, GOOD["identite"])
        self.assertIn("Étape 3 sur 8", text)

    def test_panne_du_stockage_ne_bloque_pas(self):
        fake = store_module.FakeStore()
        fake.down = True
        flow = cq.CvFlow(fake)
        self.assertIn("Étape 1 sur 8", flow.start(1))
        self.assertEqual(flow.answer(1, GOOD["poste"])[0], "ask")

    def test_clear_signale_echec_persistant(self):
        fake = store_module.FakeStore()
        flow = cq.CvFlow(fake)
        flow.start(1)
        fake.down = True
        self.assertFalse(flow.clear(1))

    def test_etat_corrompu_ignore(self):
        fake = store_module.FakeStore()
        fake.states[store_module.state_key(3)] = {"step": 99, "answers": {}}
        self.assertFalse(cq.CvFlow(fake).is_active(3))


class BotIntegrationTest(unittest.TestCase):
    def setUp(self):
        bot.cv_flow = cq.CvFlow()
        bot.chat_modes.clear()

    def test_bouton_demarre_le_questionnaire(self):
        text, keyboard, claude_input = bot.handle_cv_flow(5, bot.BUTTON_CV)
        self.assertIn("Étape 1 sur 8", text)
        self.assertIsNone(claude_input)
        self.assertEqual(bot.chat_modes[5], bot.MODE_EMPLOI)

    def test_message_sans_questionnaire_non_traite(self):
        self.assertIsNone(bot.handle_cv_flow(5, "Bonjour"))
        self.assertIsNone(bot.handle_cv_flow(5, bot.BUTTON_EMPLOI))

    def test_passer_sans_questionnaire(self):
        text, _k, claude_input = bot.handle_cv_flow(5, "/passer")
        self.assertIn("Aucun questionnaire", text)
        self.assertIsNone(claude_input)

    def test_boutons_de_menu_ne_sont_pas_des_reponses(self):
        bot.handle_cv_flow(5, "/cv")
        self.assertIsNone(bot.handle_cv_flow(5, bot.BUTTON_EMPLOI))
        self.assertIsNone(bot.handle_cv_flow(5, bot.BUTTON_AIDE))
        self.assertTrue(bot.cv_flow.is_active(5))

    def test_parcours_complet_appelle_claude_une_seule_fois(self):
        base.CLAUDE_CALLS.clear()
        base.TELEGRAM_CALLS.clear()
        base.CLAUDE_REPLY["text"] = "[[CV]]# Chargé de clientèle\n# Karim BAMBA[[/CV]]"
        try:
            bot.process_message(bot.USER_ID, 5, "/cv", 1)
            for key, _t, _q, _r in cq.STEPS:
                self.assertEqual(len(base.CLAUDE_CALLS), 0, "Claude ne doit pas être appelé avant la fin")
                bot.process_message(bot.USER_ID, 5, GOOD[key], 2)
        finally:
            base.CLAUDE_REPLY["text"] = "Réponse de test"
        self.assertEqual(len(base.CLAUDE_CALLS), 1)
        sent = base.CLAUDE_CALLS[0]["messages"][-1]["content"]
        self.assertIn("365 millions FCFA", sent)
        self.assertIn("BTS Agriculture Tropicale", sent)
        self.assertFalse(bot.cv_flow.is_active(5))
        self.assertIn("[[CV]]", bot.last_answers[5])          # export Word/PDF possible ensuite

    def test_annuler_puis_message_normal_va_a_claude(self):
        bot.handle_cv_flow(5, "/cv")
        text, _k, _c = bot.handle_cv_flow(5, "/annuler")
        self.assertIn("abandonné", text)
        self.assertIsNone(bot.handle_cv_flow(5, "Bonjour"))

    def test_oubli_efface_le_questionnaire(self):
        bot.handle_cv_flow(5, "/cv")
        bot.handle_menu(5, "/oubli")
        self.assertFalse(bot.cv_flow.is_active(5))

    def test_commandes_connues(self):
        for command in ("/cv", "/annuler", "/passer"):
            self.assertIn(command, bot.MENU_COMMANDS)


if __name__ == "__main__":
    unittest.main()
