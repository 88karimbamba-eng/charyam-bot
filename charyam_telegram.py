#!/usr/bin/env python3
"""
CHARYAM Bot - Telegram + Claude
Serveur Flask qui reçoit les mises à jour Telegram et répond via Claude
"""

import os
import json
import logging
import traceback
import httpx
from flask import Flask, request, jsonify
from anthropic import Anthropic

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("charyam")

# Config depuis variables d'environnement (SÉCURISÉ)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CLAUDE_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
USER_ID = 5608719781  # Seul utilisateur autorisé

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
client = Anthropic(api_key=CLAUDE_API_KEY)

# Mémoire simple par chat (en RAM : perdue à chaque redémarrage)
chat_memory = {}


def redact(value) -> str:
    """Masque les secrets avant d'écrire quoi que ce soit dans les journaux"""
    text = str(value)
    for secret in (TELEGRAM_TOKEN, CLAUDE_API_KEY):
        if secret:
            text = text.replace(secret, "***")
    return text


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


def setup_webhook():
    """Configure le webhook Telegram au démarrage"""
    webhook_url = "https://charyam-bot.onrender.com/webhook"
    api_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"

    try:
        response = httpx.post(api_url, json={"url": webhook_url}, timeout=10)
        result = response.json()
        if result.get("ok"):
            logger.info("✅ Webhook configuré: %s", webhook_url)
            logger.info("✅ Webhook info: %s", redact(result.get("result", {})))
        else:
            logger.error("❌ Erreur webhook: %s", redact(result))
    except Exception as e:
        logger.error("❌ Erreur configuration webhook (%s): %s", type(e).__name__, redact(e))


def send_telegram_message(chat_id: int, text: str, reply_to_message_id: int = None):
    """Envoie un message Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id

    try:
        response = httpx.post(url, json=payload, timeout=10)
        data = response.json()
        if not data.get("ok"):
            logger.error("Telegram a refusé le message: %s", redact(data))
        return data
    except Exception as e:
        logger.error("Erreur envoi Telegram (%s): %s", type(e).__name__, redact(e))
        return None


def get_claude_response(user_id: int, chat_id: int, user_message: str, first_name: str = "") -> str:
    """Obtient une réponse de Claude avec mémoire du chat"""
    if chat_id not in chat_memory:
        chat_memory[chat_id] = []

    chat_memory[chat_id].append({
        "role": "user",
        "content": user_message
    })

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=500,
            system=build_system_prompt(first_name),
            messages=chat_memory[chat_id]
        )

        # Journal utile pour diagnostiquer : modèle, types de blocs, arrêt, consommation
        stop_reason = getattr(response, "stop_reason", None)
        usage = getattr(response, "usage", None)
        logger.info(
            "Réponse Claude: modele=%s stop_reason=%s blocs=%s tokens_in=%s tokens_out=%s",
            getattr(response, "model", None),
            stop_reason,
            [getattr(block, "type", None) for block in response.content],
            getattr(usage, "input_tokens", None),
            getattr(usage, "output_tokens", None),
        )
        if stop_reason == "max_tokens":
            logger.warning("Réponse possiblement tronquée (max_tokens atteint)")

        # Claude peut retourner plusieurs blocs (thinking, text, etc.).
        # On extrait uniquement les blocs texte pour Telegram.
        text_blocks = [
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text"
        ]

        assistant_message = "".join(text_blocks).strip()

        if not assistant_message:
            raise RuntimeError(
                "Claude n'a retourné aucun bloc texte exploitable."
            )

        chat_memory[chat_id].append({
            "role": "assistant",
            "content": assistant_message
        })

        if len(chat_memory[chat_id]) > 20:
            chat_memory[chat_id] = chat_memory[chat_id][-20:]

        return assistant_message
    except Exception as e:
        logger.error("Erreur Claude (%s): %s", type(e).__name__, redact(e))
        logger.error("Détail: %s", redact(traceback.format_exc()))
        return (
            f"Désolé, une erreur technique est survenue ({type(e).__name__}). "
            "Réessaie dans un instant."
        )


@app.route("/webhook", methods=["POST"])
def webhook():
    """Reçoit les mises à jour Telegram"""
    try:
        update = request.json

        if "message" not in update:
            return jsonify({"ok": True})

        message = update["message"]
        user_id = message["from"]["id"]
        chat_id = message["chat"]["id"]
        text = message.get("text", "")
        message_id = message.get("message_id")

        if user_id != USER_ID:
            logger.warning("Message ignoré: utilisateur non autorisé (%s)", user_id)
            return jsonify({"ok": True})

        if not text or text.startswith("/"):
            return jsonify({"ok": True})

        logger.info("Message reçu de %s: %s", user_id, text)

        response_text = get_claude_response(
            user_id, chat_id, text, KNOWN_FIRST_NAMES.get(user_id, "")
        )

        send_telegram_message(chat_id, response_text, reply_to_message_id=message_id)

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