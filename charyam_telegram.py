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
"""

import os
import hmac
import time
import logging
import threading
import traceback
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import anthropic
import httpx
from flask import Flask, request, jsonify


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

# Mémoire simple par chat (en RAM : perdue à chaque redémarrage)
chat_memory = {}
_chat_locks = {}
_state_lock = threading.Lock()
_seen_updates = OrderedDict()
_secret_active = False       # vrai seulement une fois le secret enregistré chez Telegram
_thinking_rejected = False   # vrai si l'API a refusé le réglage CLAUDE_THINKING


def redact(value) -> str:
    """Masque les secrets avant d'écrire quoi que ce soit dans les journaux"""
    text = str(value)
    for secret in (TELEGRAM_TOKEN, CLAUDE_API_KEY, WEBHOOK_SECRET):
        if secret:
            text = text.replace(secret, "***")
    return text


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

THINKING_PARAM = None
if CLAUDE_THINKING == "between_tools":
    THINKING_PARAM = {"type": "between_tools"}
elif CLAUDE_THINKING:
    logger.warning("CLAUDE_THINKING=%r non reconnu : réglage ignoré", CLAUDE_THINKING)


def build_system_prompt(first_name: str = "") -> str:
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
                    return

            payload = {"url": WEBHOOK_URL, "allowed_updates": ["message"]}
            if WEBHOOK_SECRET:
                payload["secret_token"] = WEBHOOK_SECRET
            result = tg_call("setWebhook", payload)
            if result.get("ok"):
                _secret_active = bool(WEBHOOK_SECRET)
                logger.info("✅ Webhook configuré: %s", WEBHOOK_URL)
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


def setup_webhook():
    """Lance la configuration du webhook en arrière-plan : ne bloque pas le démarrage de Gunicorn"""
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


def send_telegram_message(chat_id: int, text: str, reply_to_message_id: int = None):
    """Envoie un message Telegram (découpé si nécessaire). Retourne la dernière réponse Telegram."""
    data = None
    for index, chunk in enumerate(split_for_telegram(text)):
        payload = {"chat_id": chat_id, "text": chunk}
        if reply_to_message_id and index == 0:
            payload["reply_to_message_id"] = reply_to_message_id
            payload["allow_sending_without_reply"] = True

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
        return client.messages.create(**kwargs)
    except anthropic.BadRequestError as e:
        if use_thinking and "thinking" in str(e).lower():
            _thinking_rejected = True
            logger.warning("Réglage CLAUDE_THINKING refusé par l'API : désactivé (%s)", redact(e))
            kwargs.pop("extra_body", None)
            return client.messages.create(**kwargs)
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


def get_claude_response(user_id: int, chat_id: int, user_message: str, first_name: str = "") -> str:
    """Obtient une réponse de Claude avec mémoire du chat.
    La mémoire n'est modifiée qu'après une réponse réussie."""
    messages = clean_history(
        chat_memory.get(chat_id, []) + [{"role": "user", "content": user_message}]
    )
    system = build_system_prompt(first_name)

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
        chat_memory[chat_id] = trim_history(messages + [{"role": "assistant", "content": text}])

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
            chat_memory.pop(chat_id, None)
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


def process_message(user_id: int, chat_id: int, text: str, message_id: int):
    """Traite un message en arrière-plan : une conversation à la fois, erreurs isolées"""
    with _get_chat_lock(chat_id):
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
            send_telegram_message(chat_id, response_text, reply_to_message_id=message_id)
        except Exception as e:
            logger.error("Erreur envoi réponse (%s): %s", type(e).__name__, redact(e))


@app.route("/webhook", methods=["POST"])
def webhook():
    """Reçoit les mises à jour Telegram et répond tout de suite (le travail se fait en arrière-plan)"""
    if _secret_active:
        received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(received.encode("utf-8"), WEBHOOK_SECRET.encode("utf-8")):
            logger.warning("Webhook refusé: jeton secret invalide")
            return jsonify({"ok": False}), 403

    try:
        update = request.get_json(silent=True)
        if not isinstance(update, dict):
            return jsonify({"ok": True})

        message = update.get("message")
        if not isinstance(message, dict):
            return jsonify({"ok": True})

        user_id = (message.get("from") or {}).get("id")
        chat_id = (message.get("chat") or {}).get("id")
        text = message.get("text") or ""
        message_id = message.get("message_id")

        if user_id != USER_ID:
            logger.warning("Message ignoré: utilisateur non autorisé (%s)", user_id)
            return jsonify({"ok": True})

        if chat_id is None or not text or text.startswith("/"):
            return jsonify({"ok": True})

        if _already_seen(update.get("update_id")):
            logger.info("Mise à jour déjà traitée, ignorée")
            return jsonify({"ok": True})

        # Le contenu des messages n'est jamais écrit dans les journaux
        logger.info("Message reçu de %s (%s caractères)", user_id, len(text))
        executor.submit(process_message, user_id, chat_id, text, message_id)
        return jsonify({"ok": True})
    except Exception as e:
        logger.error("Erreur webhook (%s): %s", type(e).__name__, redact(e))
        return jsonify({"ok": True})


@app.route("/health", methods=["GET"])
def health():
    """Healthcheck"""
    return jsonify({"status": "ok"})


# Configure le webhook au chargement du module (s'exécute avec Gunicorn)
setup_webhook()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)