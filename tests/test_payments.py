"""Tests SIMULÉS de la porte de paiement manuelle : aucun fichier sans paiement validé."""

import unittest

import test_charyam_telegram as base
import payments

bot = base.bot

OWNER = bot.USER_ID
CLIENT = 555000111


class GateLogicTest(unittest.TestCase):
    def make(self, instructions="Wave 0102030405, 2000 FCFA", hours=24):
        return payments.PaymentGate(OWNER, instructions, hours)

    def test_proprietaire_jamais_soumis(self):
        self.assertTrue(self.make("").can_export(OWNER, OWNER))
        self.assertTrue(self.make().can_export(OWNER, OWNER))

    def test_client_refuse_par_defaut(self):
        self.assertFalse(self.make().can_export(CLIENT, CLIENT))

    def test_sans_configuration_meme_valide_refuse(self):
        gate = self.make("")
        gate.approve(CLIENT)
        self.assertFalse(gate.can_export(CLIENT, CLIENT))
        self.assertEqual(gate.payment_request_text(), payments.NOT_CONFIGURED_TEXT)

    def test_valide_puis_expire(self):
        gate = self.make(hours=1)
        gate.approve(CLIENT, now=1000.0)
        self.assertTrue(gate.can_export(CLIENT, CLIENT, now=1000.0 + 3599))
        self.assertFalse(gate.can_export(CLIENT, CLIENT, now=1000.0 + 3600))
        self.assertFalse(gate.can_export(CLIENT, CLIENT, now=1000.0 + 3601))

    def test_declaration_ne_donne_pas_acces(self):
        gate = self.make()
        self.assertEqual(gate.declare(CLIENT, "ID123"), "ID123")
        self.assertFalse(gate.can_export(CLIENT, CLIENT))
        self.assertEqual(gate.pending_summary(), [(CLIENT, "ID123")])

    def test_refus_retire_acces_et_attente(self):
        gate = self.make()
        gate.declare(CLIENT, "ID123")
        gate.approve(CLIENT)
        self.assertTrue(gate.reject(CLIENT))
        self.assertFalse(gate.can_export(CLIENT, CLIENT))
        self.assertEqual(gate.pending_summary(), [])
        self.assertFalse(gate.reject(CLIENT))

    def test_validation_d_un_client_ne_profite_pas_a_un_autre(self):
        gate = self.make()
        gate.approve(CLIENT)
        self.assertFalse(gate.can_export(CLIENT + 1, CLIENT + 1))

    def test_reference_nettoyee(self):
        self.assertIsNone(self.make().declare(CLIENT, "   "))
        self.assertEqual(payments.clean_reference("ID\n12\t3  \x00x"), "ID 12 3 x")
        self.assertEqual(len(payments.clean_reference("a" * 500)), payments.MAX_REFERENCE_LENGTH)

    def test_identifiant_client(self):
        self.assertEqual(payments.parse_chat_id(" 123456 "), 123456)
        for bad in ("", "abc", "-5", "0", "12 34", "9" * 20):
            self.assertIsNone(payments.parse_chat_id(bad))


class BotPaymentFlowTest(unittest.TestCase):
    def setUp(self):
        base.TELEGRAM_CALLS.clear()
        base.DOCUMENT_CALLS.clear()
        bot.last_answers.clear()
        bot.chat_modes.clear()
        self.old_gate = bot.payment_gate
        bot.payment_gate = payments.PaymentGate(OWNER, "Wave 0102030405, 2000 FCFA")
        bot.last_answers[CLIENT] = "# Lettre\n\nBonjour."
        bot.last_answers[OWNER] = "# Lettre\n\nBonjour."

    def tearDown(self):
        bot.payment_gate = self.old_gate

    def texts(self):
        return [(payload.get("chat_id"), payload.get("text", ""))
                for method, payload in base.TELEGRAM_CALLS if method == "sendMessage"]

    def test_client_sans_paiement_ne_recoit_pas_de_fichier(self):
        bot.process_message(CLIENT, CLIENT, bot.BUTTON_WORD, 1)
        self.assertEqual(base.DOCUMENT_CALLS, [])
        sent = self.texts()
        self.assertEqual(len(sent), 1)
        self.assertIn("Wave 0102030405", sent[0][1])
        self.assertIn("/paye", sent[0][1])

    def test_proprietaire_recoit_son_fichier(self):
        bot.process_message(OWNER, OWNER, bot.BUTTON_PDF, 1)
        self.assertEqual(len(base.DOCUMENT_CALLS), 1)

    def test_parcours_complet(self):
        bot.process_message(CLIENT, CLIENT, "/paye ID98765", 1)
        owner_msgs = [t for cid, t in self.texts() if cid == OWNER]
        self.assertEqual(len(owner_msgs), 1)
        self.assertIn("ID98765", owner_msgs[0])
        self.assertIn(f"/valider {CLIENT}", owner_msgs[0])
        self.assertEqual(base.DOCUMENT_CALLS, [])

        bot.process_message(CLIENT, CLIENT, bot.BUTTON_WORD, 2)
        self.assertEqual(base.DOCUMENT_CALLS, [])

        bot.process_message(OWNER, OWNER, f"/valider {CLIENT}", 3)
        self.assertTrue(any(cid == CLIENT and "Paiement vérifié" in t for cid, t in self.texts()))

        bot.process_message(CLIENT, CLIENT, bot.BUTTON_WORD, 4)
        self.assertEqual(len(base.DOCUMENT_CALLS), 1)

    def test_client_ne_peut_pas_se_valider_lui_meme(self):
        bot.process_message(CLIENT, CLIENT, f"/valider {CLIENT}", 1)
        bot.process_message(CLIENT, CLIENT, bot.BUTTON_WORD, 2)
        self.assertEqual(base.DOCUMENT_CALLS, [])
        self.assertFalse(bot.payment_gate.can_export(CLIENT, CLIENT))
        self.assertTrue(any("inconnue" in t for _, t in self.texts()))

    def test_refus_prevenu_et_sans_fichier(self):
        bot.process_message(CLIENT, CLIENT, "/paye ID1", 1)
        bot.process_message(OWNER, OWNER, f"/refuser {CLIENT}", 2)
        bot.process_message(CLIENT, CLIENT, bot.BUTTON_PDF, 3)
        self.assertEqual(base.DOCUMENT_CALLS, [])
        self.assertTrue(any(cid == CLIENT and "pas pu être vérifié" in t for cid, t in self.texts()))

    def test_paye_sans_reference(self):
        bot.process_message(CLIENT, CLIENT, "/paye", 1)
        self.assertEqual([cid for cid, _ in self.texts()], [CLIENT])
        self.assertIn("/paye", self.texts()[0][1])

    def test_paye_sans_configuration(self):
        bot.payment_gate = payments.PaymentGate(OWNER, "")
        bot.process_message(CLIENT, CLIENT, "/paye ID1", 1)
        self.assertEqual([cid for cid, _ in self.texts()], [CLIENT])
        self.assertEqual(bot.payment_gate.pending_summary(), [])

    def test_valider_identifiant_invalide(self):
        bot.process_message(OWNER, OWNER, "/valider abc", 1)
        self.assertFalse(bot.payment_gate.can_export(CLIENT, CLIENT))
        self.assertIn("identifiant", self.texts()[0][1])

    def test_liste_des_attentes(self):
        bot.process_message(CLIENT, CLIENT, "/paye ID42", 1)
        base.TELEGRAM_CALLS.clear()
        bot.process_message(OWNER, OWNER, "/paiements", 2)
        self.assertIn(f"{CLIENT} : ID42", self.texts()[0][1])

    def test_commandes_de_paiement_reconnues_par_le_webhook(self):
        for command in payments.PAYMENT_COMMANDS:
            self.assertIn(command, bot.MENU_COMMANDS)


if __name__ == "__main__":
    unittest.main()
