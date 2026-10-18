#!/usr/bin/env python3
"""
CHARYAM Bot - Telegram + Claude
Serveur Flask (webhook Telegram) qui répond via l'API Anthropic.

Variables d'environnement
  Obligatoires : TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY
  Facultatives : CLAUDE_MODEL            (défaut : claude-sonnet-5-5)
                 CLAUDE_MAX_TOKENS       (défaut : 8192, borné entre 256 et 16000)
                 CLAUDE_TIMEOUT_SECONDS  (défaut : 150)
                 CLAUDE_THINKING         (vide = réflexion par défaut du modèle ; "between_tools" = réflexion réduite)
                 WEBHOOK_URL             (défaut : RENDER_EXTERNAL_URL + /webhook)
                 TELEGRAM_WEBHOOK_SECRET (jeton secret du webhook : lettres, chiffres, _ et - uniquement)
Intégrations (toutes facultatives, voir docs/INTEGRATIONS.md et .env.example) :
                 PERPLEXITY_API_KEY, FIRECRAWL_API_KEY, COMPOSIO_API_KEY (+ COMPOSIO_USER_ID,
                 COMPOSIO_READONLY_TOOLS, COMPOSIO_TOOL_VERSION), PLAYWRIGHT_CDP_URL ou PLAYWRIGHT_LOCAL
                 (+ PLAYWRIGHT_ALLOWED_DOMAINS)
"""

import os
import re
import datetime
import hmac
import time
import logging
import platform
import threading
import traceback
import importlib.metadata
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import anthropic
import httpx
from flask import Flask, request, jsonify

import access
import charyam_docs
import store as store_module
from integrations.common import redact_secrets
from integrations.tool_router import ToolRouter, complete_with_tools


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Lit un entier dans l'environnement, borné ; retourne le défaut si invalide"""
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


# Config depuis variables d'environnement (SÉCURISÉ)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CLAUDE_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
USER_ID = 5608719781  # Seul utilisateur autorisé

# La réflexion de Claude compte dans max_tokens : on garde une marge large pour le texte.
MAX_TOKENS = _env_int("CLAUDE_MAX_TOKENS", 8192, 256, 16000)
RETRY_MAX_TOKENS = min(MAX_TOKENS * 2, 16000)
CLAUDE_TIMEOUT = _env_int("CLAUDE_TIMEOUT_SECONDS", 150, 20, 280)
CLAUDE_THINKING = os.environ.get("CLAUDE_THINKING", "").strip().lower()

WEBHOOK_URL = (
    os.environ.get("WEBHOOK_URL")
    or (os.environ.get("RENDER_EXTERNAL_URL") or "https://charyam-bot.onrender.com").rstrip("/") + "/webhook"
)
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
# Production = Render (variable RENDER=true posée par la plateforme) ou CHARYAM_ENV=production.
# En production le secret du webhook est OBLIGATOIRE : sans lui, /webhook refuse tout (503).
IS_PRODUCTION = (os.environ.get("RENDER", "").strip().lower() == "true"
                 or os.environ.get("CHARYAM_ENV", "").strip().lower() == "production")

MAX_HISTORY_MESSAGES = 20      # nombre maximal de messages gardés par conversation
MAX_HISTORY_CHARS = 30000      # taille maximale cumulée de l'historique
TELEGRAM_CHUNK_SIZE = 3900     # Telegram refuse > 4096 caractères : marge de sécurité
WORKER_THREADS = 4

# Prénoms connus, par identifiant Telegram (jamais partagés d'un utilisateur à l'autre).
# Le champ "first_name" de Telegram est un nom de profil, pas toujours un prénom
# (ex. "Mr") : on ne s'en sert donc PAS pour désigner la personne.
KNOWN_FIRST_NAMES = {
    USER_ID: "Karim",
}

# Stockage persistant facultatif (Upstash) : sans ses deux variables, tout reste en mémoire locale
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "").strip()
persistent_store = store_module.make_store()

# Testeurs autorisés (variable ALLOWED_USER_IDS) et limite de messages par utilisateur
access_control = access.AccessControl(USER_ID, store=persistent_store)

# Validation au démarrage
if not TELEGRAM_TOKEN:
    raise ValueError("❌ TELEGRAM_BOT_TOKEN n'est pas défini")
if not CLAUDE_API_KEY:
    raise ValueError("❌ ANTHROPIC_API_KEY n'est pas défini")

app = Flask(__name__)
# Le SDK Anthropic utilise sa propre bibliothèque HTTP (httpx2) : on lui passe un simple nombre
# de secondes, jamais un objet httpx.Timeout (il le refuserait).
client = anthropic.Anthropic(
    api_key=CLAUDE_API_KEY,
    timeout=float(CLAUDE_TIMEOUT),
    max_retries=1,
)
telegram_http = httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0))
executor = ThreadPoolExecutor(max_workers=WORKER_THREADS, thread_name_prefix="charyam")

# Mémoire par chat : cache local en RAM (perdu à chaque redémarrage). Si le stockage persistant est
# configuré, la conversation y est aussi sauvegardée (7 jours) et rechargée après un redémarrage.
chat_memory = {}
chat_modes = {}              # activité choisie dans le menu, par chat (perdue au redémarrage)
last_answers = {}            # dernière réponse de Claude, par chat, pour l'export Word/PDF (en RAM)
LAST_ANSWERS_MAX = 200       # nombre maximal de chats gardés dans last_answers
_chat_locks = {}
_state_lock = threading.Lock()
_seen_updates = OrderedDict()
_secret_active = False       # vrai seulement une fois le secret enregistré chez Telegram
_thinking_rejected = False   # vrai si l'API a refusé le réglage CLAUDE_THINKING


def redact(value) -> str:
    """Masque les secrets avant d'écrire quoi que ce soit dans les journaux"""
    text = str(value)
    for secret in (TELEGRAM_TOKEN, CLAUDE_API_KEY, WEBHOOK_SECRET, UPSTASH_TOKEN):
        if secret:
            text = text.replace(secret, "***")
    return redact_secrets(text)


class RedactingFormatter(logging.Formatter):
    """Masque les secrets dans TOUT ce qui est journalisé : nos messages,
    ceux des bibliothèques et les traces d'erreur."""

    def format(self, record):
        return redact(super().format(record))


_log_handler = logging.StreamHandler()
_log_handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_log_handler], force=True)

# httpx écrit l'URL complète de chaque appel au niveau INFO. Celle de Telegram
# contient le jeton du bot : on coupe ces lignes (le filtre ci-dessus est une 2e protection).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("charyam")

# Outils externes : aucun n'est exposé à Claude tant que sa configuration est incomplète
ROUTER = ToolRouter()


def _idna_codec_works() -> bool:
    try:
        "charyam.example".encode("idna")
        return True
    except LookupError:
        return False


def ensure_idna_codec():
    """Charge le codec « idna » dans le thread principal, AVANT tout autre thread.

    Werkzeug (nom d'hôte de chaque requête), socket (DNS) et httpx en dépendent. Au premier
    usage, Python importe encodings.idna ; si cet import échoue, Python AVALE l'ImportError et
    mémorise « codec inexistant » pour tout le processus, ce qui donne ensuite, à chaque
    requête, « LookupError: unknown encoding: idna ». On le charge donc ici, au démarrage.
    En cas d'échec, on importe le module explicitement pour obtenir la VRAIE erreur, puis on
    purge l'échec mémorisé. Si le codec reste inutilisable, on arrête le démarrage avec un
    message clair : le service ne pourrait de toute façon ni servir de requêtes ni joindre Telegram."""
    if _idna_codec_works():
        return
    logger.warning("Codec « idna » indisponible au premier essai : diagnostic et récupération")
    try:
        import encodings.idna  # noqa: F401  (lève la vraie erreur, normalement masquée)
    except Exception as e:
        raise RuntimeError(
            f"Codec 'idna' inutilisable : import de encodings.idna impossible "
            f"({type(e).__name__}: {e}). Python {platform.python_version()} semble incomplet."
        ) from e
    cache = getattr(encodings, "_cache", None)
    if isinstance(cache, dict):
        cache.pop("idna", None)  # purge de l'échec mémorisé par Python
    if not _idna_codec_works():
        raise RuntimeError(
            f"Codec 'idna' toujours inutilisable après récupération (Python {platform.python_version()})."
        )
    logger.warning("Codec « idna » récupéré après purge de l'échec mémorisé")


ensure_idna_codec()


def _log_runtime_info():
    """Journalise la version de Python et des bibliothèques réellement utilisées au démarrage"""
    versions = []
    for package in ("Flask", "Werkzeug", "anthropic", "gunicorn", "idna", "httpx"):
        try:
            versions.append(f"{package}={importlib.metadata.version(package)}")
        except Exception:
            versions.append(f"{package}=?")
    logger.info(
        "Démarrage CHARYAM : Python %s | %s | modèle=%s | max_tokens=%s",
        platform.python_version(), " ".join(versions), CLAUDE_MODEL, MAX_TOKENS,
    )


_log_runtime_info()
logger.info("Intégrations actives : %s", [k for k, v in ROUTER.status().items() if v] or "aucune")

THINKING_PARAM = None
if CLAUDE_THINKING == "between_tools":
    THINKING_PARAM = {"type": "between_tools"}
elif CLAUDE_THINKING:
    logger.warning("CLAUDE_THINKING=%r non reconnu : réglage ignoré", CLAUDE_THINKING)


# ---------------------------------------------------------------- Menu et activités

MODE_EMPLOI = "emploi"
MODE_DOCUMENTS = "documents"

BUTTON_EMPLOI = "💼 Emploi & Candidatures"
BUTTON_DOCUMENTS = "📄 Documents professionnels"
BUTTON_AIDE = "❓ Aide"
BUTTON_WORD = "⬇️ Word"
BUTTON_PDF = "⬇️ PDF"

EXPORT_BUTTONS = {BUTTON_WORD: "docx", BUTTON_PDF: "pdf"}

BUTTON_MODES = {
    BUTTON_EMPLOI: MODE_EMPLOI,
    BUTTON_DOCUMENTS: MODE_DOCUMENTS,
}

# Clavier permanent : chaque bouton envoie son libellé comme un message texte ordinaire,
# ce qui fonctionne avec allowed_updates=["message"] (pas de callback_query à gérer).
MENU_KEYBOARD = {
    "keyboard": [
        [{"text": BUTTON_EMPLOI}],
        [{"text": BUTTON_DOCUMENTS}],
        [{"text": BUTTON_WORD}, {"text": BUTTON_PDF}],
        [{"text": BUTTON_AIDE}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

MODE_PROMPTS = {
    MODE_EMPLOI: (
        "Activité en cours : Emploi & Candidatures. Tu aides à analyser des offres d'emploi, "
        "adapter un CV, rédiger des lettres de motivation et des emails de candidature, "
        "et préparer des entretiens. N'invente jamais d'expérience, de diplôme, de date, "
        "d'employeur ou de chiffre : utilise uniquement ce que la personne fournit et demande "
        "ce qui manque. Signale honnêtement les écarts entre son profil et les exigences du poste. "
        "Quand tu analyses une offre lue depuis une page, n'attribue à l'offre que ce que la page "
        "contient réellement ; ce qui vient de toi (conseils, questions d'entretien types) doit "
        "être présenté comme ton conseil, jamais comme un contenu de la page. "
        "Si l'offre demande de candidater par e-mail, donne l'adresse seulement si elle figure "
        "dans le texte lu ou dans la ligne « Adresses e-mail présentes sur la page » ; sinon écris "
        "« adresse non trouvée sur la page » et ne l'invente pas. "
        "Si une date limite est indiquée ou probable, calcule le nombre de jours restants à partir "
        "de la date du jour donnée plus bas et signale l'urgence. "
        "Tu ne connais pas le profil de la personne tant qu'elle ne l'a pas donné : ne dis pas "
        "qu'elle convient au poste, demande-lui son CV actuel ou ses expériences. "
        "Structure les CV et les lettres avec des titres (#) et des listes (-) : la personne pourra "
        "ensuite en obtenir une copie Word ou PDF avec les boutons du menu."
    ),
    MODE_DOCUMENTS: (
        "Activité en cours : Documents professionnels. Tu rédiges des courriers, devis, factures, "
        "comptes rendus et rapports. Pour tout montant, détaille le calcul ligne par ligne "
        "(quantité × prix unitaire, total HT, taxe, total TTC) et vérifie-le. N'invente aucun prix, "
        "taux de taxe, nom ou coordonnée : écris [à compléter]. Écris le document dans ta réponse, "
        "structuré avec des titres (#), des listes (-) et des tableaux Markdown (|) : la personne "
        "pourra ensuite en obtenir une copie Word ou PDF avec les boutons du menu."
    ),
}

MODE_RULE = (
    "Quand tu analyses ou conseilles, sépare clairement trois parties : « Données fournies », "
    "« Hypothèses » et « Recommandations ». Quand tu rédiges un document, donne le document, "
    "puis liste à part tes hypothèses et les éléments à compléter. "
    "Tu ne crées ni n'envoies jamais de fichier toi-même : n'affirme jamais l'avoir fait."
)

MENU_TEXT = (
    "Choisis une activité dans le menu ci-dessous, ou écris simplement ta question.\n\n"
    f"{BUTTON_EMPLOI} : analyse d'offre, CV, lettre, email de candidature, entretien.\n"
    f"{BUTTON_DOCUMENTS} : courrier, devis, facture, compte rendu, rapport.\n\n"
    f"{BUTTON_WORD} ou {BUTTON_PDF} : recevoir ma dernière réponse en fichier."
)

HELP_TEXT = (
    "Comment m'utiliser :\n"
    "• Choisis une activité avec les boutons, puis écris ta demande en donnant le plus "
    "de détails possible (offre complète, ton parcours, montants…).\n"
    "• Je ne devine pas les informations manquantes : je te les demande ou je les marque "
    "[à compléter].\n"
    "• Après une réponse, touche ⬇️ Word ou ⬇️ PDF pour la recevoir en fichier "
    "(le fichier contient ma dernière réponse, à relire avant usage).\n"
    "• Je ne retiens que la conversation en cours. Selon la configuration du service, je peux "
    "l'oublier après un redémarrage ; /oubli l'efface quand tu veux.\n\n"
    "Commandes : /menu pour afficher le menu, /aide pour ce message, /oubli pour effacer la conversation."
)

MODE_SELECTED_TEXT = {
    MODE_EMPLOI: (
        "Activité : Emploi & Candidatures.\n"
        "Envoie-moi l'offre d'emploi (texte complet) et/ou ton parcours, puis dis-moi ce que tu veux : "
        "analyse, CV, lettre de motivation, email ou préparation d'entretien."
    ),
    MODE_DOCUMENTS: (
        "Activité : Documents professionnels.\n"
        "Dis-moi quel document tu veux (courrier, devis, facture, compte rendu, rapport), "
        "pour qui, et donne les informations utiles (noms, articles, quantités, prix, taux de taxe)."
    ),
}


MENU_COMMANDS = ("/start", "/menu", "/aide", "/help", "/outils", "/oubli")

FORGET_DONE_TEXT = "C'est fait : j'ai effacé notre conversation et ma dernière réponse."
FORGET_PARTIAL_TEXT = (
    "J'ai effacé la conversation sur ce service, mais je n'ai pas pu confirmer l'effacement de la copie "
    "sauvegardée. Réessaie /oubli dans quelques minutes."
)

UNKNOWN_COMMAND_TEXT = "Commande inconnue. Touche /menu pour voir les choix, ou écris simplement ta demande."


def telegram_display(text: str) -> str:
    """Rend une réponse lisible dans Telegram (qui n'affiche pas le Markdown) : plus d'étoiles ni de dièses.

    Affichage seulement : la version Markdown d'origine reste gardée pour les exports Word/PDF,
    qui s'en servent pour les titres, listes et tableaux."""
    out = []
    in_code = False
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            out.append(line)
            continue
        if re.fullmatch(r"\|?[\s:|-]+\|[\s:|-]*", stripped) and "-" in stripped:      # séparateur de tableau
            continue
        if re.fullmatch(r"[-*_]{3,}", stripped):                                         # trait horizontal
            continue
        heading = re.match(r"(#{1,6})\s+(.*)", stripped)
        if heading:
            title = heading.group(2).strip()
            out.extend(["", title.upper() if len(heading.group(1)) == 1 else title])
            continue
        if stripped.startswith("|") and stripped.endswith("|"):                           # ligne de tableau
            line = " | ".join(cell.strip() for cell in stripped.strip("|").split("|"))
        bullet = re.match(r"(\s*)[-*]\s+(.*)", line)
        if bullet:
            line = f"{bullet.group(1)}• {bullet.group(2)}"
        out.append(line)
    cleaned = "\n".join(out)
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"__(.+?)__", r"\1", cleaned)
    cleaned = re.sub(r"`+", "", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _reply_unknown_command(chat_id: int):
    try:
        send_telegram_message(chat_id, UNKNOWN_COMMAND_TEXT, reply_markup=MENU_KEYBOARD)
    except Exception as e:
        logger.error("Erreur envoi commande inconnue (%s): %s", type(e).__name__, redact(e))


def tools_status_text() -> str:
    """État des outils externes (activé / non configuré), sans aucun secret"""
    lines = ["Outils externes :"]
    for label, active in ROUTER.status().items():
        lines.append(f"• {label} : {'activé' if active else 'non configuré'}")
    lines.append("Je ne les utilise que si ta demande l'exige.")
    return "\n".join(lines)


def handle_menu(chat_id: int, text: str):
    """Traite les commandes et les boutons du menu sans appeler Claude.
    Retourne (texte, clavier) si le message est géré ici, sinon None."""
    command = text.strip().split()[0].split("@")[0].lower() if text.strip() else ""
    if command in ("/start", "/menu"):
        return MENU_TEXT, MENU_KEYBOARD
    if command in ("/aide", "/help") or text.strip() == BUTTON_AIDE:
        return HELP_TEXT, MENU_KEYBOARD
    if command == "/outils":
        return tools_status_text(), MENU_KEYBOARD
    if command == "/oubli":
        last_answers.pop(chat_id, None)
        if _clear_history(chat_id):
            return FORGET_DONE_TEXT, MENU_KEYBOARD
        return FORGET_PARTIAL_TEXT, MENU_KEYBOARD
    mode = BUTTON_MODES.get(text.strip())
    if mode:
        chat_modes[chat_id] = mode
        return MODE_SELECTED_TEXT[mode], MENU_KEYBOARD
    return None


# ---------------------------------------------------------------- Export Word / PDF

EXPORT_TITLES = {MODE_EMPLOI: "Candidature", MODE_DOCUMENTS: "Document professionnel"}
EXPORT_MIME = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}
NO_ANSWER_TEXT = (
    "Je n'ai pas de réponse récente à convertir (je l'oublie après un redémarrage). "
    "Demande-moi d'abord le document, puis touche à nouveau le bouton."
)


def _remember_answer(chat_id: int, text: str):
    """Garde la dernière réponse du chat pour l'export (borné en nombre de chats)"""
    last_answers.pop(chat_id, None)
    last_answers[chat_id] = text
    while len(last_answers) > LAST_ANSWERS_MAX:
        last_answers.pop(next(iter(last_answers)))


def build_export(chat_id: int, fmt: str):
    """Construit (nom_de_fichier, octets) à partir de la dernière réponse du chat.
    Tout reste en mémoire : aucun fichier temporaire. Lève ValueError (message destiné
    à l'utilisateur) s'il n'y a rien à convertir."""
    text = last_answers.get(chat_id)
    if not text:
        raise ValueError(NO_ANSWER_TEXT)
    title = EXPORT_TITLES.get(chat_modes.get(chat_id), "Document CHARYAM")
    blocks = charyam_docs.markdown_to_blocks(text)
    if fmt == "pdf":
        data = charyam_docs.pdf_bytes(title, blocks)
    else:
        data = charyam_docs.docx_bytes(title, blocks)
    filename = charyam_docs.safe_filename(f"{title}_{time.strftime('%Y-%m-%d')}", fmt)
    return filename, data


def _handle_export(chat_id: int, fmt: str):
    """Génère et envoie le fichier. Ne dit « envoyé » que si Telegram a accepté le fichier."""
    try:
        filename, data = build_export(chat_id, fmt)
    except ValueError as e:
        send_telegram_message(chat_id, str(e), reply_markup=MENU_KEYBOARD)
        return
    except Exception as e:
        logger.error("Erreur création %s (%s): %s", fmt, type(e).__name__, redact(e))
        other = "Word" if fmt == "pdf" else "PDF"
        send_telegram_message(
            chat_id,
            f"Je n'ai pas pu créer ce fichier. Réessaie, ou demande le format {other}.",
            reply_markup=MENU_KEYBOARD,
        )
        return
    try:
        result = send_telegram_document(
            chat_id, filename, data, EXPORT_MIME[fmt],
            caption="Fichier créé à partir de ma dernière réponse. Relis-le avant de l'utiliser.",
        )
    except Exception as e:
        logger.error("Erreur envoi fichier (%s): %s", type(e).__name__, redact(e))
        result = {"ok": False}
    if result.get("ok"):
        logger.info("Telegram: fichier %s envoyé (chat %s, %s octets)", fmt, chat_id, len(data))
    else:
        logger.error("Telegram a refusé le fichier: %s", redact(result))
        send_telegram_message(
            chat_id, "Le fichier a été créé mais l'envoi a échoué. Réessaie dans un instant.",
            reply_markup=MENU_KEYBOARD,
        )


def build_system_prompt(first_name: str = "", mode: str = None) -> str:
    """Construit le message système : identité du bot, de l'utilisateur, limites de la mémoire"""
    parts = ["Tu es CHARYAM BOUTIQUE, un assistant Telegram."]
    first_name = (first_name or "").strip()
    if first_name:
        parts.append(
            f"Le prénom de la personne qui t'écrit est {first_name}. "
            "Ce prénom est le sien, pas le tien. "
            "Si elle t'indique un autre prénom dans la conversation, "
            "utilise celui qu'elle te donne."
        )
    else:
        parts.append(
            "Tu ne connais pas le prénom de la personne tant qu'elle ne te l'a pas dit : "
            "ne le devine pas."
        )
    parts.append(
        "Si elle te dit son prénom ou te salue par son prénom, "
        "c'est elle qui le porte, jamais toi."
    )
    parts.append("Réponds en français, de façon concise et utile. Ne fabrique rien.")
    parts.append(
        "Tu ne retiens que la conversation en cours. Tu n'as pas de mémoire permanente "
        "et tu peux tout oublier après un redémarrage : ne promets jamais de te souvenir "
        "à long terme."
    )
    if mode in MODE_PROMPTS:
        parts.append(MODE_PROMPTS[mode])
        parts.append(MODE_RULE)
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        parts.append(f"Date du jour (UTC) : {today}.")
    return " ".join(parts)


# ---------------------------------------------------------------- Telegram

def tg_call(method: str, payload: dict = None) -> dict:
    """Appelle l'API Telegram et retourne toujours un dict (jamais de jeton dans les erreurs)"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    response = telegram_http.post(url, json=payload or {})
    try:
        data = response.json()
    except ValueError:
        data = {"ok": False, "description": f"HTTP {response.status_code}, réponse non JSON"}
    return data if isinstance(data, dict) else {"ok": False, "description": "réponse inattendue"}


def _log_webhook_status():
    """Journalise l'état du webhook tel que Telegram le voit (getWebhookInfo).

    Ni le jeton ni le secret n'y figurent : c'est le diagnostic qu'il fallait faire à la main
    dans un navigateur avec le jeton. Il montre si l'URL est la bonne, combien de messages
    attendent, et la dernière erreur de livraison rencontrée par Telegram."""
    try:
        info = tg_call("getWebhookInfo")
        if not info.get("ok"):
            logger.warning("État du webhook illisible: %s", redact(info.get("description")))
            return
        result = info.get("result") or {}
        error_date = result.get("last_error_date")
        when = (
            time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(error_date))
            if isinstance(error_date, (int, float)) else "jamais"
        )
        logger.info(
            "État du webhook Telegram: url_conforme=%s en_attente=%s derniere_erreur=%s (%s) "
            "mises_a_jour_autorisees=%s",
            result.get("url") == WEBHOOK_URL,
            result.get("pending_update_count"),
            when,
            redact(result.get("last_error_message") or "aucune")[:200],
            result.get("allowed_updates"),
        )
    except Exception as e:
        logger.warning("État du webhook indisponible (%s): %s", type(e).__name__, redact(e))


def _configure_webhook():
    """Configure le webhook sans appel inutile : getWebhookInfo d'abord, setWebhook seulement
    si l'URL diffère. Jusqu'à 3 tentatives (ReadTimeout isolé, démarrage à froid)."""
    global _secret_active
    for attempt in range(1, 4):
        try:
            if not WEBHOOK_SECRET:
                info = tg_call("getWebhookInfo")
                if info.get("ok") and (info.get("result") or {}).get("url") == WEBHOOK_URL:
                    logger.info("✅ Webhook déjà configuré: %s", WEBHOOK_URL)
                    _log_webhook_status()
                    return

            payload = {"url": WEBHOOK_URL, "allowed_updates": ["message"]}
            if WEBHOOK_SECRET:
                payload["secret_token"] = WEBHOOK_SECRET
            result = tg_call("setWebhook", payload)
            if result.get("ok"):
                _secret_active = bool(WEBHOOK_SECRET)
                logger.info("✅ Webhook configuré: %s (secret enregistré chez Telegram : %s)",
                            WEBHOOK_URL, "oui" if _secret_active else "non")
                _log_webhook_status()
                return
            logger.error("❌ Erreur webhook (tentative %s/3): %s", attempt, redact(result))
        except Exception as e:
            logger.error(
                "❌ Erreur configuration webhook (tentative %s/3, %s): %s",
                attempt, type(e).__name__, redact(e),
            )
        if attempt < 3:
            time.sleep(3)
    logger.error("❌ Webhook non configuré après 3 tentatives (celui déjà enregistré reste actif)")


def log_secret_diagnostic():
    """Journalise UNIQUEMENT si le secret du webhook est présent et non vide, jamais sa valeur ni sa longueur."""
    if WEBHOOK_SECRET:
        logger.info("Secret du webhook : présent et non vide (contrôle de l'en-tête exigé sur /webhook)")
    elif IS_PRODUCTION:
        logger.critical("Secret du webhook : ABSENT ou vide en PRODUCTION (TELEGRAM_WEBHOOK_SECRET) : "
                        "/webhook refuse toutes les requêtes (503) tant qu'il n'est pas défini")
    else:
        logger.warning("Secret du webhook : ABSENT ou vide (TELEGRAM_WEBHOOK_SECRET), hors production : "
                       "/webhook n'authentifie PAS Telegram")


def setup_webhook():
    """Lance la configuration du webhook en arrière-plan : ne bloque pas le démarrage de Gunicorn"""
    log_secret_diagnostic()
    threading.Thread(target=_configure_webhook, name="setup-webhook", daemon=True).start()


def split_for_telegram(text: str, limit: int = TELEGRAM_CHUNK_SIZE) -> list:
    """Découpe un long texte en morceaux acceptés par Telegram, de préférence
    aux sauts de ligne, puis aux espaces."""
    text = (text or "").strip()
    if not text:
        return []
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def send_telegram_message(chat_id: int, text: str, reply_to_message_id: int = None,
                          reply_markup: dict = None):
    """Envoie un message Telegram (découpé si nécessaire). Retourne la dernière réponse Telegram.
    Le clavier éventuel (reply_markup) est joint à la dernière partie."""
    data = None
    chunks = split_for_telegram(text)
    for index, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk}
        if reply_to_message_id and index == 0:
            payload["reply_to_message_id"] = reply_to_message_id
            payload["allow_sending_without_reply"] = True
        if reply_markup and index == len(chunks) - 1:
            payload["reply_markup"] = reply_markup

        try:
            data = tg_call("sendMessage", payload)
            if not data.get("ok") and data.get("error_code") == 429:
                wait = (data.get("parameters") or {}).get("retry_after", 2)
                time.sleep(min(float(wait), 5.0))
                data = tg_call("sendMessage", payload)

            if data.get("ok"):
                logger.info("Telegram: message envoyé (chat %s, partie %s)", chat_id, index + 1)
            else:
                logger.error("Telegram a refusé le message: %s", redact(data))
                break
        except Exception as e:
            logger.error("Erreur envoi Telegram (%s): %s", type(e).__name__, redact(e))
            return None
    return data


def send_telegram_document(chat_id: int, filename: str, data: bytes, mime: str, caption: str = None) -> dict:
    """Envoie un fichier (multipart). Retourne toujours un dict, comme tg_call."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    form = {"chat_id": str(chat_id)}
    if caption:
        form["caption"] = caption
    response = telegram_http.post(url, data=form, files={"document": (filename, data, mime)})
    try:
        result = response.json()
    except ValueError:
        return {"ok": False, "description": f"HTTP {response.status_code}, réponse non JSON"}
    return result if isinstance(result, dict) else {"ok": False, "description": "réponse inattendue"}


def _typing_loop(chat_id: int, stop: threading.Event):
    """Affiche « écrit… » pendant que Claude répond (échecs ignorés)"""
    while not stop.is_set():
        try:
            tg_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except Exception:
            pass
        stop.wait(4.5)


# ---------------------------------------------------------------- Mémoire

def clean_history(messages: list) -> list:
    """Rend l'historique valide pour l'API : pas de contenu vide, rôles qui alternent
    (les messages consécutifs du même rôle sont fusionnés), et premier message = utilisateur."""
    cleaned = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if cleaned and cleaned[-1]["role"] == message["role"]:
            cleaned[-1] = {
                "role": message["role"],
                "content": cleaned[-1]["content"] + "\n\n" + content,
            }
        else:
            cleaned.append({"role": message["role"], "content": content})
    while cleaned and cleaned[0]["role"] != "user":
        cleaned.pop(0)
    return cleaned


def trim_history(messages: list) -> list:
    """Limite l'historique en nombre de messages et en taille, sans perdre le dernier échange"""
    messages = clean_history(messages)[-MAX_HISTORY_MESSAGES:]
    while len(messages) > 2 and sum(len(m["content"]) for m in messages) > MAX_HISTORY_CHARS:
        messages = messages[1:]
    return clean_history(messages)


# ---------------------------------------------------------------- Claude

def _call_claude(messages: list, system: str, max_tokens: int):
    """Appel unique à l'API Anthropic. Si le réglage CLAUDE_THINKING est refusé (400),
    on le désactive pour la suite et on réessaie sans lui."""
    global _thinking_rejected
    kwargs = {
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }
    use_thinking = THINKING_PARAM is not None and not _thinking_rejected
    if use_thinking:
        kwargs["extra_body"] = {"thinking": THINKING_PARAM}
    try:
        return complete_with_tools(lambda **kw: client.messages.create(**kw), kwargs, ROUTER)
    except anthropic.BadRequestError as e:
        if use_thinking and "thinking" in str(e).lower():
            _thinking_rejected = True
            logger.warning("Réglage CLAUDE_THINKING refusé par l'API : désactivé (%s)", redact(e))
            kwargs.pop("extra_body", None)
            return complete_with_tools(lambda **kw: client.messages.create(**kw), kwargs, ROUTER)
        raise


def _read_response(response):
    """Journalise la réponse et en extrait le texte. Seuls les blocs de type « text »
    sont utilisés ; thinking et autres types sont ignorés, sans supposer d'attribut .text."""
    blocks = getattr(response, "content", None) or []
    stop_reason = getattr(response, "stop_reason", None)
    usage = getattr(response, "usage", None)
    logger.info(
        "Réponse Claude: modele=%s stop_reason=%s blocs=%s tokens_in=%s tokens_out=%s",
        getattr(response, "model", None),
        stop_reason,
        [getattr(block, "type", None) for block in blocks],
        getattr(usage, "input_tokens", None),
        getattr(usage, "output_tokens", None),
    )
    parts = []
    for block in blocks:
        if getattr(block, "type", None) == "text":
            block_text = getattr(block, "text", None)
            if isinstance(block_text, str) and block_text:
                parts.append(block_text)
    return "".join(parts).strip(), stop_reason


def _user_error_message(exc: Exception) -> str:
    """Message d'erreur clair en français (sans détail technique sensible)"""
    status = getattr(exc, "status_code", None)
    if isinstance(exc, anthropic.APITimeoutError):
        return "Claude met trop de temps à répondre. Réessaie dans un instant."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Connexion à Claude impossible pour le moment. Réessaie dans un instant."
    if isinstance(exc, anthropic.RateLimitError):
        return "Trop de demandes en ce moment. Réessaie dans une minute."
    if status in (401, 403):
        return "Le service n'a pas accès à Claude (configuration à vérifier). Réessaie plus tard."
    if isinstance(status, int) and status >= 500:
        return "Claude est momentanément indisponible. Réessaie dans un instant."
    return (
        f"Désolé, une erreur technique est survenue ({type(exc).__name__}). "
        "Réessaie dans un instant."
    )


def _load_history(chat_id: int) -> list:
    """Historique du chat : le cache local d'abord (toujours au moins aussi récent dans ce processus),
    sinon le stockage persistant. Une panne du stockage n'interrompt jamais la conversation."""
    local = chat_memory.get(chat_id)
    if local:
        return local
    if persistent_store is not None:
        try:
            saved = persistent_store.get_history(chat_id)
        except store_module.StoreError as exc:
            logger.warning("Mémoire persistante illisible, conversation locale utilisée (%s)", exc)
            return []
        if saved:
            chat_memory[chat_id] = saved
            return saved
    return []


def _save_history(chat_id: int, messages: list):
    """Sauvegarde dans le cache local, puis dans le stockage persistant s'il existe (échec journalisé)."""
    chat_memory[chat_id] = messages
    if persistent_store is not None:
        try:
            persistent_store.set_history(chat_id, messages)
        except store_module.StoreError as exc:
            logger.warning("Mémoire persistante non sauvegardée, copie locale conservée (%s)", exc)


def _clear_history(chat_id: int) -> bool:
    """Efface la conversation partout. Retourne False si la copie persistante n'a pas pu être effacée."""
    chat_memory.pop(chat_id, None)
    if persistent_store is not None:
        try:
            persistent_store.delete_history(chat_id)
        except store_module.StoreError as exc:
            logger.warning("Effacement de la mémoire persistante impossible (%s)", exc)
            return False
    return True


def get_claude_response(user_id: int, chat_id: int, user_message: str, first_name: str = "") -> str:
    """Obtient une réponse de Claude avec mémoire du chat.
    La mémoire n'est modifiée qu'après une réponse réussie."""
    messages = clean_history(
        _load_history(chat_id) + [{"role": "user", "content": user_message}]
    )
    system = build_system_prompt(first_name, chat_modes.get(chat_id))

    try:
        response = _call_claude(messages, system, MAX_TOKENS)
        text, stop_reason = _read_response(response)

        # La réflexion a pu consommer tout le budget avant le texte : un seul nouvel essai, plus large
        if not text and stop_reason == "max_tokens" and RETRY_MAX_TOKENS > MAX_TOKENS:
            logger.warning(
                "Aucun texte (budget épuisé) : nouvel essai avec max_tokens=%s", RETRY_MAX_TOKENS
            )
            response = _call_claude(messages, system, RETRY_MAX_TOKENS)
            text, stop_reason = _read_response(response)

        if not text:
            logger.error("Aucun bloc texte exploitable (stop_reason=%s)", stop_reason)
            if stop_reason == "max_tokens":
                return (
                    "Ma réponse n'a pas pu aboutir dans la limite de longueur. "
                    "Réessaie, ou pose une question plus ciblée."
                )
            if stop_reason == "refusal":
                return "Je ne peux pas répondre à cette demande."
            return "Je n'ai pas réussi à formuler de réponse. Reformule ta demande, s'il te plaît."

        # Mémoire : uniquement du texte, jamais de blocs « thinking »
        _save_history(chat_id, trim_history(messages + [{"role": "assistant", "content": text}]))
        _remember_answer(
            chat_id,
            text + ("\n\n[Réponse incomplète : limite de longueur atteinte.]" if stop_reason == "max_tokens" else ""),
        )

        if stop_reason == "max_tokens":
            return (
                text
                + "\n\n[⚠️ Réponse incomplète : limite de longueur atteinte. "
                "Écris « continue » pour obtenir la suite.]"
            )
        return text
    except Exception as e:
        logger.error("Erreur Claude (%s): %s", type(e).__name__, redact(e))
        logger.error("Détail: %s", redact(traceback.format_exc()))
        if isinstance(e, anthropic.BadRequestError):
            # Requête refusée : on repart d'un historique vide pour ne pas bloquer les messages suivants
            _clear_history(chat_id)
            logger.warning("Historique du chat %s réinitialisé après une requête refusée", chat_id)
        return _user_error_message(e)


# ---------------------------------------------------------------- Traitement des messages

def _get_chat_lock(chat_id: int) -> threading.Lock:
    with _state_lock:
        return _chat_locks.setdefault(chat_id, threading.Lock())


def _already_seen(update_id) -> bool:
    """Ignore une mise à jour déjà reçue (Telegram peut la renvoyer si la réponse tarde)"""
    if update_id is None:
        return False
    with _state_lock:
        if update_id in _seen_updates:
            return True
        _seen_updates[update_id] = True
        while len(_seen_updates) > 500:
            _seen_updates.popitem(last=False)
        return False


def _send_notice(chat_id: int, text: str):
    """Envoie un court message d'information (accès refusé, limite atteinte), sans appeler Claude"""
    try:
        send_telegram_message(chat_id, text)
    except Exception as e:
        logger.error("Erreur envoi information (%s): %s", type(e).__name__, redact(e))


def process_message(user_id: int, chat_id: int, text: str, message_id: int):
    """Traite un message en arrière-plan : une conversation à la fois, erreurs isolées"""
    with _get_chat_lock(chat_id):
        export_format = EXPORT_BUTTONS.get(text.strip())
        if export_format:
            try:
                _handle_export(chat_id, export_format)
            except Exception as e:
                logger.error("Erreur export (%s): %s", type(e).__name__, redact(e))
            return

        menu_reply = handle_menu(chat_id, text)
        if menu_reply:
            reply_text, keyboard = menu_reply
            try:
                send_telegram_message(chat_id, reply_text, reply_markup=keyboard)
            except Exception as e:
                logger.error("Erreur envoi menu (%s): %s", type(e).__name__, redact(e))
            return

        stop_typing = threading.Event()
        threading.Thread(
            target=_typing_loop, args=(chat_id, stop_typing), name="typing", daemon=True
        ).start()
        try:
            response_text = get_claude_response(
                user_id, chat_id, text, KNOWN_FIRST_NAMES.get(user_id, "")
            )
        except Exception as e:
            logger.error("Erreur traitement message (%s): %s", type(e).__name__, redact(e))
            response_text = "Désolé, une erreur technique est survenue. Réessaie dans un instant."
        finally:
            stop_typing.set()

        try:
            send_telegram_message(chat_id, telegram_display(response_text), reply_to_message_id=message_id)
        except Exception as e:
            logger.error("Erreur envoi réponse (%s): %s", type(e).__name__, redact(e))


def webhook_auth(received: str, secret: str, production: bool) -> str:
    """Décide si une requête /webhook est authentifiée. Retourne :
    - « accepte »      : secret configuré et en-tête identique (comparaison en temps constant) ;
    - « refuse »       : secret configuré mais en-tête absent, vide ou différent ;
    - « mal_configure » : production SANS secret : tout est refusé (jamais de fail-open en production) ;
    - « ouvert_dev »   : hors production et sans secret : contrôle désactivé (poste de développement, tests).
    Un secret vide n'est jamais comparé : hmac.compare_digest(b"", b"") serait vrai."""
    secret = (secret or "").strip()
    if not secret:
        return "mal_configure" if production else "ouvert_dev"
    received = received or ""
    if hmac.compare_digest(received.encode("utf-8"), secret.encode("utf-8")):
        return "accepte"
    return "refuse"


@app.route("/webhook", methods=["POST"])
def webhook():
    """Reçoit les mises à jour Telegram et répond tout de suite (le travail se fait en arrière-plan)"""
    # Authentification AVANT tout traitement. Secret configuré : exigé, même si son enregistrement chez
    # Telegram n'a pas (encore) réussi. Production sans secret : tout est refusé (503), jamais de fail-open.
    verdict = webhook_auth(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), WEBHOOK_SECRET, IS_PRODUCTION)
    if verdict == "mal_configure":
        logger.error("Webhook refusé: TELEGRAM_WEBHOOK_SECRET absent ou vide en production (service fermé)")
        return jsonify({"ok": False}), 503
    if verdict == "refuse":
        logger.warning("Webhook refusé: jeton secret invalide")
        return jsonify({"ok": False}), 403

    try:
        update = request.get_json(silent=True)
        if not isinstance(update, dict):
            logger.info("Requête ignorée: corps absent ou non JSON")
            return jsonify({"ok": True})

        message = update.get("message")
        if not isinstance(message, dict):
            # Seuls les NOMS de champs sont journalisés, jamais leur contenu
            logger.info("Mise à jour ignorée: pas de message (champs: %s)", sorted(update)[:8])
            return jsonify({"ok": True})

        user_id = (message.get("from") or {}).get("id")
        chat_id = (message.get("chat") or {}).get("id")
        text = message.get("text") or ""
        message_id = message.get("message_id")

        if not access_control.is_allowed(user_id):
            logger.warning("Message ignoré: utilisateur non autorisé (%s)", user_id)
            if chat_id is not None and access_control.should_notify(user_id, "refus"):
                executor.submit(_send_notice, chat_id, access.refusal_text(user_id))
            return jsonify({"ok": True})

        if chat_id is None or not text.strip():
            logger.info("Message ignoré: sans texte exploitable (champs: %s)", sorted(message)[:10])
            return jsonify({"ok": True})
        # Commandes : seules celles du menu sont traitées ; les autres restent ignorées
        if text.startswith("/") and text.split()[0].split("@")[0].lower() not in MENU_COMMANDS:
            logger.info("Message ignoré: commande non gérée")
            executor.submit(_reply_unknown_command, chat_id)
            return jsonify({"ok": True})

        if _already_seen(update.get("update_id")):
            logger.info("Mise à jour déjà traitée, ignorée")
            return jsonify({"ok": True})

        allowed_now, limit_kind = access_control.check_rate(user_id)
        if not allowed_now:
            logger.info("Message refusé: limite par %s atteinte (%s)", limit_kind, user_id)
            if access_control.should_notify(user_id, "limite-" + limit_kind):
                executor.submit(_send_notice, chat_id, access.rate_limit_text(limit_kind))
            return jsonify({"ok": True})

        # Le contenu des messages n'est jamais écrit dans les journaux
        logger.info("Message reçu de %s (%s caractères)", user_id, len(text))
        executor.submit(process_message, user_id, chat_id, text, message_id)
        return jsonify({"ok": True})
    except Exception as e:
        logger.error("Erreur webhook (%s): %s", type(e).__name__, redact(e))
        return jsonify({"ok": True})


@app.route("/", methods=["GET"])
def index():
    """Page d'accueil minimale : Render et les robots interrogent souvent « / »"""
    return jsonify({"service": "charyam-bot", "status": "ok"})


@app.route("/health", methods=["GET"])
def health():
    """Healthcheck"""
    return jsonify({"status": "ok"})


# Configure le webhook au chargement du module (s'exécute avec Gunicorn)
setup_webhook()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
