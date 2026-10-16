"""Tests SIMULÉS : CV balisé, score de correspondance calculé par le code, thème couleur ATS."""

import io
import unittest
import zipfile

import test_charyam_telegram as base

bot = base.bot

CV = """# Bamba Abdul Karim
Abengourou, Côte d'Ivoire | 88karimbamba@gmail.com

## Profil
Chargé de portefeuille crédit, 7 ans en finance coopérative agricole.

## Expérience professionnelle
### Chargé de développement de portefeuille crédit, UNACOOPEC-CI (2019 - présent)
- Gestion d'un portefeuille de 365 millions FCFA
- Animation de groupes AVEC/VSLA
"""

SCORE_ITEMS = (
    "✅ Instruction de dossiers de crédit\n✅ Animation de groupes AVEC/VSLA\n⚠️ Anglais courant\n"
    "❌ Licence Bac+3\n✅ Recouvrement\n✅ Permis B\n"
)

ANSWER = f"Voici le CV adapté.\n\n[[CV]]\n{CV}[[/CV]]\n\n[[SCORE]]\n{SCORE_ITEMS}[[/SCORE]]\n"


def lines(n_ok, n_part, n_miss):
    return "\n".join(["✅ a"] * n_ok + ["⚠️ b"] * n_part + ["❌ c"] * n_miss)


class ScoreTest(unittest.TestCase):
    def test_calcul_par_le_code(self):
        # 4 ✅, 1 ⚠️, 1 ❌ -> (4 + 0,5) / 6 = 75 %
        self.assertEqual(bot.match_score(SCORE_ITEMS), (75, 4, 1, 1))

    def test_bornes(self):
        self.assertEqual(bot.match_score(lines(3, 0, 0))[0], 100)
        self.assertEqual(bot.match_score(lines(0, 0, 3))[0], 0)
        self.assertEqual(bot.match_score(lines(0, 4, 0))[0], 50)

    def test_trop_peu_de_lignes_pas_de_score(self):
        self.assertIsNone(bot.match_score(lines(1, 1, 0)))
        self.assertIsNone(bot.match_score("aucune ligne marquée"))

    def test_couleurs(self):
        self.assertIn("🟢", bot.score_line(75))
        self.assertIn("🟡", bot.score_line(74))
        self.assertIn("🟡", bot.score_line(50))
        self.assertIn("🔴", bot.score_line(49))
        self.assertIn("estimation", bot.score_line(60))
        self.assertIn("pas un score officiel", bot.score_line(60))

    def test_barre_proportionnelle(self):
        self.assertIn("█" * 8 + "░" * 2, bot.score_line(80))


class DisplayTest(unittest.TestCase):
    def test_affichage_avec_score_et_sans_balises(self):
        out = bot.telegram_display(ANSWER)
        self.assertNotIn("[[", out)
        self.assertIn("Score de correspondance avec l'offre : 75/100", out)
        self.assertIn("🟢", out)
        self.assertIn("✅ 4   ⚠️ 1   ❌ 1", out)
        self.assertIn("CORRESPONDANCE AVEC L'OFFRE", out)
        self.assertIn("BAMBA ABDUL KARIM", out)               # titre du CV, mis en forme pour Telegram
        self.assertNotIn("##", out)

    def test_balises_isolees_retirees(self):
        out = bot.telegram_display("Texte [[CV]] sans fin et [[/SCORE]] stray")
        self.assertNotIn("[[", out)

    def test_pas_de_score_sans_balise(self):
        self.assertNotIn("Score de correspondance", bot.telegram_display("Réponse simple"))


class ExportTest(unittest.TestCase):
    def setUp(self):
        base.TELEGRAM_CALLS.clear()
        bot.last_answers.clear()
        bot.chat_modes.clear()
        bot.last_answers[7] = ANSWER
        bot.chat_modes[7] = bot.MODE_EMPLOI

    def docx_xml(self, data):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return z.read("word/document.xml").decode(), z.read("word/styles.xml").decode()

    def test_le_fichier_contient_le_cv_et_jamais_le_score(self):
        name, data = bot.build_export(7, "docx")
        document, styles = self.docx_xml(data)
        self.assertIn("Bamba Abdul Karim", document)
        self.assertIn("365 millions FCFA", document)
        for forbidden in ("Score de correspondance", "Voici le CV adapté", "CORRESPONDANCE", "[[", "✅", "❌"):
            self.assertNotIn(forbidden, document)
        self.assertTrue(name.startswith("CV_"))

    def test_theme_cv_sans_titre_automatique_et_en_couleur(self):
        _, data = bot.build_export(7, "docx")
        document, styles = self.docx_xml(data)
        self.assertNotIn('w:val="Title"', document)           # le nom du candidat ouvre le document
        self.assertIn('w:val="Heading1"', document)
        self.assertIn("1F3A5F", styles)                       # bleu marine
        self.assertIn("0E7C7B", styles)                       # vert-bleu, avec filet sous les sections
        self.assertIn("w:pBdr", styles)

    def test_ats_une_colonne_sans_tableau_ni_image(self):
        _, data = bot.build_export(7, "docx")
        document, _ = self.docx_xml(data)
        self.assertNotIn("<w:tbl>", document)
        self.assertNotIn("w:drawing", document)
        self.assertNotIn("w:pict", document)

    def test_pdf_valide_avec_texte_selectionnable(self):
        _, data = bot.build_export(7, "pdf")
        self.assertTrue(data.startswith(b"%PDF"))
        from pypdf import PdfReader
        text = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)
        self.assertIn("Bamba Abdul Karim", text)
        self.assertIn("Profil", text)
        self.assertNotIn("Score de correspondance", text)

    def test_sans_balises_export_complet_en_theme_document(self):
        bot.last_answers[7] = "# Lettre\n\nBonjour,\n\n[[SCORE]]\n✅ a\n✅ b\n❌ c\n[[/SCORE]]"
        name, data = bot.build_export(7, "docx")
        document, _ = self.docx_xml(data)
        self.assertIn("Candidature", document)               # titre automatique du thème « document »
        self.assertIn("Bonjour", document)
        self.assertNotIn("✅", document)                      # le bloc de score n'entre jamais dans le fichier
        self.assertNotIn("[[", document)

    def test_mode_documents_inchange(self):
        bot.chat_modes[7] = bot.MODE_DOCUMENTS
        bot.last_answers[7] = "# Devis\n\n| A | B |\n|---|---|\n| 1 | 2 |"
        _, data = bot.build_export(7, "docx")
        document, _ = self.docx_xml(data)
        self.assertIn("Document professionnel", document)
        self.assertIn("<w:tbl>", document)                    # les tableaux restent permis hors CV


class PromptTest(unittest.TestCase):
    def test_consigne_emploi_contient_les_regles_ats_et_score(self):
        prompt = bot.build_system_prompt("Karim", bot.MODE_EMPLOI)
        for needle in ("[[CV]]", "[[/CV]]", "[[SCORE]]", "aucun tableau", "N'écris aucun pourcentage",
                       "N'ajoute jamais au CV"):
            self.assertIn(needle, prompt)

    def test_autre_mode_sans_balises(self):
        self.assertNotIn("[[CV]]", bot.build_system_prompt("Karim", bot.MODE_DOCUMENTS))


if __name__ == "__main__":
    unittest.main()
