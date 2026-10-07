#!/usr/bin/env python3
"""
CHARYAM Bot - Telegram + Claude
Serveur Flask qui reçoit les mises à jour Telegram et répond via Claude
"""

import os
import json
import httpx
from flask import Flask, request, jsonify
from anthropic import Anthropic

# Config depuis variables d'environnement (SÉCURISÉ)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CLAUDE_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
USER_ID = 5608719781  # Seul utilisateur autorisé

# Validation au démarrage
if not TELEGRAM_TOKEN:
    raise ValueError("❌ TELEGRAM_BOT_TOKEN n'est pas défini")
if not CLAUDE_API_KEY:
    raise ValueError("❌ ANTHROPIC_API_KEY n'est pas défini")

app = Flask(__name__)
client = Anthropic(api_key=CLAUDE_API_KEY)

# Mémoire simple par chat
chat_memory = {}

def setup_webhook():
    """Configure le webhook Telegram au démarrage"""
    webhook_url = "https://charyam-bot.onrender.com/webhook"
    api_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
    
    try:
        response = httpx.post(api_url, json={"url": webhook_url}, timeout=10)
        result = response.json()
        if result.get("ok"):
            print(f"✅ Webhook configuré: {webhook_url}")
            print(f"✅ Webhook info: {result.get('result', {})}")
        else:
            print(f"❌ Erreur webhook: {result}")
    except Exception as e:
        print(f"❌ Erreur configuration webhook: {e}")

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
        return response.json()
    except Exception as e:
        print(f"Erreur envoi Telegram: {e}")
        return None

def get_claude_response(user_id: int, chat_id: int, user_message: str) -> str:
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
            system="Tu es un assistant Telegram. Réponds en français, de façon concise et utile. Ne fabrique rien.",
            messages=chat_memory[chat_id]
        )

        assistant_message = response.content[0].text

        chat_memory[chat_id].append({
            "role": "assistant",
            "content": assistant_message
        })

        if len(chat_memory[chat_id]) > 20:
            chat_memory[chat_id] = chat_memory[chat_id][-20:]

        return assistant_message
    except Exception as e:
        print(f"Erreur Claude: {e}")
        return f"Erreur: {str(e)}"

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
            return jsonify({"ok": True})

        if not text or text.startswith("/"):
            return jsonify({"ok": True})

        print(f"Message reçu de {user_id}: {text}")

        response_text = get_claude_response(user_id, chat_id, text)

        send_telegram_message(chat_id, response_text, reply_to_message_id=message_id)

        return jsonify({"ok": True})
    except Exception as e:
        print(f"Erreur webhook: {e}")
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