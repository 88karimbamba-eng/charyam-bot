"""Questionnaire « CV intelligent » : 8 étapes, une question à la fois, « Étape X sur 8 ».

Principe :
- Le client répond étape par étape (boutons /passer et /annuler). Rien n'est inventé : seules ses réponses
  servent à rédiger le CV, et les étapes facultatives sautées restent vides ([à compléter]).
- La progression est gardée en mémoire locale et, si le stockage persistant existe, dans Upstash
  (clé propre à chaque chat, expiration 7 jours). Elle est effacée à la fin, sur /annuler et sur /oubli.
- Aucune réponse n'est jamais écrite dans les journaux (elles contiennent téléphone et e-mail).
- Le module ne dépend ni de Telegram ni de Claude : il renvoie des textes, le bot décide de l'envoi.
"""

import logging

logger = logging.getLogger("charyam")

TOTAL_STEPS = 8
MAX_ANSWER_CHARS = 6000           # une réponse plus longue est refusée (garde-fou de coût et de taille)
MAX_TOTAL_CHARS = 24000           # total de toutes les réponses : borne le coût de la rédaction finale
MIN_REQUIRED_CHARS = 2

COMMAND_START = "/cv"
COMMAND_CANCEL = "/annuler"
COMMAND_SKIP = "/passer"

# (clé, titre, question, obligatoire)
STEPS = (
    (
        "poste",
        "Poste visé",
        "Quel poste vises-tu ? Écris l'intitulé exact.\n"
        "Si tu as l'offre d'emploi, colle son texte complet dans le même message : je m'en servirai "
        "pour adapter le CV et calculer le score de correspondance.",
        True,
    ),
    (
        "identite",
        "Identité et contact",
        "Écris ton nom et tes prénoms, ton numéro de téléphone, ton e-mail et ta ville.\n"
        "Exemple : une information par ligne.",
        True,
    ),
    (
        "profil",
        "Profil",
        "En 2 ou 3 phrases : ton domaine, ton nombre d'années d'expérience, ce qui te distingue.\n"
        "Ne mets que du vrai. Touche /passer si tu préfères que je le rédige à partir du reste.",
        False,
    ),
    (
        "experience_recente",
        "Expérience actuelle ou la plus récente",
        "Donne : intitulé du poste, employeur, ville, dates (mois et année), puis tes missions principales.\n"
        "Ajoute les chiffres dont tu es sûr (portefeuille géré, nombre de clients ou de groupes suivis, "
        "montants, taux).",
        True,
    ),
    (
        "experiences_precedentes",
        "Expériences précédentes",
        "Même format pour chaque ancien poste (intitulé, employeur, dates, missions, chiffres vrais). "
        "Sépare les postes par une ligne vide.\n"
        "Touche /passer s'il n'y en a pas.",
        False,
    ),
    (
        "realisations",
        "Réalisations chiffrées",
        "Cite 3 à 5 résultats chiffrés dont tu es sûr (ex. : 500 producteurs accompagnés, "
        "365 millions FCFA de portefeuille, taux de remboursement, dossiers instruits).\n"
        "Pas d'estimation inventée : si tu n'es pas sûr d'un chiffre, écris-le sans chiffre ou touche /passer.",
        False,
    ),
    (
        "formation",
        "Formation",
        "Donne tes diplômes (intitulé, établissement, année) et tes formations complémentaires.",
        True,
    ),
    (
        "competences",
        "Compétences, langues et divers",
        "Donne tes compétences clés, tes outils, tes langues avec leur niveau, ton permis de conduire "
        "et tes certifications.\nTouche /passer pour terminer.",
        False,
    ),
)

assert len(STEPS) == TOTAL_STEPS

INTRO_TEXT = (
    "Je vais construire ton CV en {n} étapes, une question à la fois.\n"
    "• /passer saute une étape facultative.\n"
    "• /annuler abandonne le questionnaire.\n"
    "Je n'écris que ce que tu me donnes : ce qui manque sera marqué [à compléter]."
).format(n=TOTAL_STEPS)

CANCELLED_TEXT = "Questionnaire CV abandonné. Touche 📝 CV intelligent pour recommencer quand tu veux."
ALREADY_RUNNING_TEXT = "Un questionnaire CV est déjà en cours. Je te repose la question de l'étape."
TOO_LONG_TEXT = (
    "Ta réponse est trop longue (limite : environ 6 000 caractères). Raccourcis-la, "
    "ou envoie l'essentiel de cette étape."
)
TOO_MUCH_TOTAL_TEXT = (
    "Le total de tes réponses devient trop long pour que je rédige le CV. Raccourcis cette réponse "
    "(garde l'essentiel), ou touche /passer."
)
TOO_SHORT_TEXT = "Cette étape est obligatoire : écris ta réponse, s'il te plaît."
CANNOT_SKIP_TEXT = "Cette étape est obligatoire, je ne peux pas la sauter. Écris ta réponse."
DONE_TEXT = "Merci, j'ai tout. Je rédige ton CV…"


def step_text(index: int) -> str:
    """Texte complet de la question à l'étape `index` (0 à 7)."""
    _key, title, question, required = STEPS[index]
    tag = "obligatoire" if required else "facultatif, /passer possible"
    return f"Étape {index + 1} sur {TOTAL_STEPS} — {title} ({tag})\n\n{question}"


class CvFlow:
    """États des questionnaires en cours, par chat. `store` est le stockage persistant ou None."""

    def __init__(self, store=None):
        self._store = store
        self._states = {}

    # ---- État ---------------------------------------------------------------------------------
    def _load(self, chat_id: int):
        state = self._states.get(chat_id)
        if state is not None:
            return state
        if self._store is not None:
            try:
                saved = self._store.get_state(chat_id)
            except Exception as exc:   # panne du stockage : on continue en mémoire locale
                logger.warning("Progression CV illisible (%s)", type(exc).__name__)
                return None
            if self._valid(saved):
                self._states[chat_id] = saved
                return saved
        return None

    @staticmethod
    def _valid(state) -> bool:
        return (
            isinstance(state, dict)
            and isinstance(state.get("step"), int)
            and 0 <= state["step"] < TOTAL_STEPS
            and isinstance(state.get("answers"), dict)
        )

    def _save(self, chat_id: int, state: dict):
        self._states[chat_id] = state
        if self._store is not None:
            try:
                self._store.set_state(chat_id, state)
            except Exception as exc:
                logger.warning("Progression CV non sauvegardée, copie locale conservée (%s)", type(exc).__name__)

    def clear(self, chat_id: int) -> bool:
        """Efface le questionnaire partout. False si la copie persistante n'a pas pu être effacée."""
        self._states.pop(chat_id, None)
        if self._store is not None:
            try:
                self._store.delete_state(chat_id)
            except Exception as exc:
                logger.warning("Effacement de la progression CV impossible (%s)", type(exc).__name__)
                return False
        return True

    def is_active(self, chat_id: int) -> bool:
        return self._load(chat_id) is not None

    # ---- Actions ------------------------------------------------------------------------------
    def start(self, chat_id: int) -> str:
        state = self._load(chat_id)
        if state is not None:
            return ALREADY_RUNNING_TEXT + "\n\n" + step_text(state["step"])
        self._save(chat_id, {"step": 0, "answers": {}})
        return INTRO_TEXT + "\n\n" + step_text(0)

    def cancel(self, chat_id: int) -> str:
        self.clear(chat_id)
        return CANCELLED_TEXT

    def skip(self, chat_id: int):
        """Saute une étape facultative. Retourne ("ask"|"done"|"refused", texte, réponses ou None)."""
        state = self._load(chat_id)
        if state is None:
            return ("none", "", None)
        _key, _title, _question, required = STEPS[state["step"]]
        if required:
            return ("refused", CANNOT_SKIP_TEXT, None)
        return self._advance(chat_id, state)

    def answer(self, chat_id: int, text: str):
        """Enregistre la réponse à l'étape courante.
        Retourne (statut, texte, réponses) : statut "ask" (question suivante), "done" (terminé, `réponses`
        contient tout), "refused" (réponse refusée, même question) ou "none" (pas de questionnaire)."""
        state = self._load(chat_id)
        if state is None:
            return ("none", "", None)
        text = (text or "").strip()
        key, _title, _question, required = STEPS[state["step"]]
        if len(text) > MAX_ANSWER_CHARS:
            return ("refused", TOO_LONG_TEXT, None)
        if required and len(text) < MIN_REQUIRED_CHARS:
            return ("refused", TOO_SHORT_TEXT, None)
        already = sum(len(v) for k, v in state["answers"].items() if k != key)
        if already + len(text) > MAX_TOTAL_CHARS:
            return ("refused", TOO_MUCH_TOTAL_TEXT, None)
        state["answers"][key] = text
        return self._advance(chat_id, state)

    def _advance(self, chat_id: int, state: dict):
        state["step"] += 1
        if state["step"] >= TOTAL_STEPS:
            answers = dict(state["answers"])
            self.clear(chat_id)
            return ("done", DONE_TEXT, answers)
        self._save(chat_id, state)
        return ("ask", step_text(state["step"]), None)


def build_generation_prompt(answers: dict) -> str:
    """Message envoyé à Claude une fois le questionnaire terminé. Contient uniquement les réponses du client."""
    labels = {key: title for key, title, _q, _r in STEPS}
    lines = [
        "Le questionnaire CV est terminé. Voici les réponses de la personne, étape par étape. "
        "Rédige maintenant son CV complet, entre [[CV]] et [[/CV]], selon les règles de l'activité en cours.",
        "Règles : n'invente rien ; écris [à compléter] pour toute information manquante (ne devine ni date, "
        "ni employeur, ni chiffre) ; reprends les chiffres exactement comme donnés ; rédige le profil "
        "toi-même seulement avec les faits fournis si l'étape Profil est vide ; vise 1 à 2 pages pleines ; "
        "si l'étape « Poste visé » contient une offre, adapte les mots-clés aux seuls éléments vrais "
        "et termine par le bloc [[SCORE]] ... [[/SCORE]] ; sinon n'écris aucun score.",
        "",
    ]
    for key, title, _q, _r in STEPS:
        value = (answers.get(key) or "").strip()
        lines.append(f"## {labels[key]}")
        lines.append(value if value else "(non renseigné)")
        lines.append("")
    return "\n".join(lines).strip()
