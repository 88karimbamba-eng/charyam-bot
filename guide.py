"""Parcours guidé du client : accueil après un « bonjour », puis étapes numérotées jusqu'au paiement.

Ce module ne contient que des textes et une détection de salutation. Il ne dépend ni de Telegram ni de
Claude : le bot décide de l'envoi. Aucun prix n'est écrit ici : les tarifs et les numéros de paiement
viennent uniquement des consignes du propriétaire (PAYMENT_INSTRUCTIONS dans Render), pour ne jamais en
annoncer de faux.
"""

import re
import unicodedata

MAX_GREETING_WORDS = 4

# Mots de salutation (sans accents, en minuscules). Un message n'est une salutation que s'il est COURT :
# « bonjour, je veux un CV pour le poste de comptable » part vers l'assistant, pas vers l'accueil.
GREETING_WORDS = frozenset({
    "bonjour", "bonsoir", "bonne", "salut", "slt", "bjr", "bsr", "hello", "hi", "hey", "coucou",
    "salam", "assalamou", "alaykoum", "alaikoum", "yo", "cc", "svp", "stp", "tout",
    "le", "monde", "a", "tous", "journee", "soiree", "ca", "va", "bien", "et", "toi", "vous",
    "charyam", "boutique", "bonjourr", "bonsoirr", "bonjou",
})
GREETING_STARTERS = frozenset({
    "bonjour", "bonsoir", "bonne", "salut", "slt", "bjr", "bsr", "hello", "hi", "hey", "coucou",
    "salam", "assalamou", "yo", "cc", "bonjourr", "bonsoirr", "bonjou",
})

_NON_LETTERS = re.compile(r"[^a-z\s]")


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", (text or "").lower())
    no_accents = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", _NON_LETTERS.sub(" ", no_accents)).strip()


def is_greeting(text: str) -> bool:
    """Vrai pour un message court qui est seulement une salutation (« Bonjour », « Bonsoir à tous »,
    « Salut, ça va ? »). Une phrase plus longue, ou une salutation suivie d'une vraie demande, est fausse."""
    words = _normalize(text).split()
    if not words or len(words) > MAX_GREETING_WORDS:
        return False
    return words[0] in GREETING_STARTERS and all(word in GREETING_WORDS for word in words)


STEPS_TEXT = (
    "Comment ça se passe, en 4 étapes :\n"
    "1️⃣ Tu choisis ce que tu veux avec les boutons.\n"
    "2️⃣ Je te pose mes questions, une à la fois (8 pour le CV).\n"
    "3️⃣ Je rédige ton document et tu le relis ici. Tu peux demander des corrections.\n"
    "4️⃣ Tu paies, puis tu reçois ton fichier Word ou PDF."
)


def tariffs_block(instructions: str) -> str:
    """Tarifs et moyens de paiement, tels que saisis par le propriétaire (jamais réécrits)."""
    instructions = (instructions or "").strip()
    if not instructions:
        return "Les tarifs et le moyen de paiement te seront communiqués par l'administrateur avant tout paiement."
    return "Tarifs et paiement :\n" + instructions


def welcome_text(instructions: str, button_cv: str, button_lettre: str, button_emploi: str,
                 button_documents: str, button_volet_cv: str, button_ebooks: str, button_boutique: str) -> str:
    return (
        "Bonjour 👋 Bienvenue chez CHARYAM BOUTIQUE.\n"
        "Je t'accompagne pas à pas. Voici ce que tu peux faire ici, touche un bouton 👇\n\n"
        f"📝 1. CV et lettre de motivation ({button_volet_cv})\n"
        + STEPS_TEXT + "\n"
        + tariffs_block(instructions) + "\n"
        f"{button_cv} : je te pose 8 questions, puis je rédige ton CV.\n"
        f"{button_lettre} : une lettre adaptée à ton offre.\n"
        f"{button_emploi} : analyse d'offre, email de candidature, entretien.\n"
        f"{button_documents} : courrier, devis, facture, rapport.\n\n"
        f"📚 2. Ebooks ({button_ebooks}) : nos ebooks en vente sur notre boutique en ligne.\n\n"
        f"🛍️ 3. Vêtements, accessoires et savon bio ({button_boutique}) : notre boutique sur WhatsApp."
    )


def cv_volet_text(instructions: str, button_cv: str, button_lettre: str, button_tarifs: str) -> str:
    """Volet « CV et lettre de motivation » : ce qui se passe, du premier bouton au fichier."""
    return (
        "📝 CV et lettre de motivation\n\n"
        + STEPS_TEXT + "\n\n"
        + tariffs_block(instructions) + "\n\n"
        "Par quoi commencer ?\n"
        f"{button_cv} : je te pose 8 questions, puis je rédige ton CV.\n"
        f"{button_lettre} : une lettre adaptée à ton offre.\n"
        f"{button_tarifs} : les prix et comment payer."
    )


def resume_text() -> str:
    return "Content de te revoir 👋 Tu as un questionnaire CV en cours. Voici la question où on s'est arrêtés :"


def lettre_intro(button_cv: str) -> str:
    return (
        "✉️ Lettre de motivation. 2 étapes :\n"
        "Étape 1 : colle ici le texte COMPLET de l'offre d'emploi (ou écris le poste visé et l'entreprise).\n"
        "Étape 2 : donne-moi ton parcours : tes postes, tes diplômes et tes chiffres vrais.\n"
        f"Si tu viens de faire ton CV ici, dis-le : je m'en sers. Sinon, touche {button_cv} d'abord.\n\n"
        "Je n'invente rien : ce qui manque sera marqué [à compléter]."
    )


def tariffs_text(instructions: str, button_word: str, button_pdf: str) -> str:
    return (
        tariffs_block(instructions) + "\n\n"
        "Le paiement se fait à la fin, une fois ton document relu et prêt :\n"
        f"1️⃣ Touche {button_word} ou {button_pdf} : je te donne les consignes.\n"
        "2️⃣ Tu paies, puis tu m'envoies /paye suivi de ta référence de transaction.\n"
        "3️⃣ L'administrateur vérifie ton paiement. Je te préviens ici dès que c'est fait.\n"
        f"4️⃣ Tu touches de nouveau {button_word} ou {button_pdf} et ton fichier arrive."
    )


def after_cv_text(button_word: str, button_pdf: str, button_lettre: str) -> str:
    """Étape suivante, envoyée juste après la rédaction du CV. Texte fixe : ne passe pas par Claude."""
    return (
        "✅ Voilà ton CV. Prochaine étape :\n"
        "• Relis-le. Pour corriger, écris simplement ce qui change "
        "(ex. : « mets le poste en titre », « ajoute mon permis B »).\n"
        f"• Quand il te convient, touche {button_word} ou {button_pdf}. "
        "Je te donne les consignes de paiement, puis tu reçois ton fichier.\n"
        f"• Tu veux aussi une lettre de motivation ? Touche {button_lettre}."
    )
