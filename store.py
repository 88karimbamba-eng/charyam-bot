"""Stockage persistant facultatif (Redis Upstash via son API REST) pour la mémoire des conversations
et les compteurs de quotas.

Principe :
- Sans les variables UPSTASH_REDIS_REST_URL et UPSTASH_REDIS_REST_TOKEN, rien ne change : tout reste
  en mémoire locale, comme avant (make_store renvoie None).
- Aucune dépendance nouvelle : httpx est déjà installé.
- Deux espaces de clés séparés. Conversation : "charyam:hist:<chat>", expiration 7 jours (renouvelée à
  chaque message). Quotas : "charyam:q:<type>:<utilisateur>:<fenêtre>", compteurs numériques sans
  contenu, expiration égale à la fenêtre.
- Toute erreur du service lève StoreError. L'appelant décide du repli (voir charyam_telegram et access).
- Le jeton n'est jamais écrit dans les journaux ni dans les messages d'erreur.
"""

import json
import logging
import os

import httpx

logger = logging.getLogger("charyam")

HISTORY_TTL_SECONDS = 7 * 24 * 3600
REQUEST_TIMEOUT_SECONDS = 4.0
MAX_STORED_HISTORY_BYTES = 200_000   # garde-fou : une conversation plus grosse n'est pas envoyée


class StoreError(Exception):
    """Le stockage est indisponible ou a répondu de façon inattendue (message sans secret)."""


def history_key(chat_id) -> str:
    return f"charyam:hist:{int(chat_id)}"


def state_key(chat_id) -> str:
    return f"charyam:cv:{int(chat_id)}"


def quota_key(kind: str, user_id, window) -> str:
    return f"charyam:q:{kind}:{int(user_id)}:{int(window)}"


class UpstashStore:
    """Client minimal de l'API REST d'Upstash : une commande Redis par requête POST."""

    def __init__(self, url: str, token: str, transport=None):
        url = (url or "").strip().rstrip("/")
        token = (token or "").strip()
        if not url.startswith("https://") or not token:
            raise ValueError("URL https et jeton Upstash requis")
        self._url = url
        self._token = token
        self._http = httpx.Client(
            timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS), transport=transport
        )

    def _command(self, *args):
        try:
            response = self._http.request(
                "POST",
                self._url,
                json=[str(a) for a in args],
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.HTTPError as exc:
            raise StoreError(f"Stockage injoignable ({type(exc).__name__})") from None
        if response.status_code != 200:
            raise StoreError(f"Stockage : HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise StoreError("Stockage : réponse illisible") from None
        if not isinstance(body, dict) or "error" in body:
            raise StoreError("Stockage : commande refusée")
        return body.get("result")

    # ---- Conversation -------------------------------------------------------------------------
    def get_history(self, chat_id):
        """Renvoie la liste de messages stockée, ou None s'il n'y en a pas (ou si le contenu est invalide)."""
        raw = self._command("GET", history_key(chat_id))
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, list):
            return None
        return [
            m for m in data
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")
            and isinstance(m.get("content"), str)
        ]

    def set_history(self, chat_id, messages):
        payload = json.dumps(messages, ensure_ascii=False)
        if len(payload.encode("utf-8")) > MAX_STORED_HISTORY_BYTES:
            raise StoreError("Historique trop volumineux pour le stockage")
        self._command("SET", history_key(chat_id), payload, "EX", HISTORY_TTL_SECONDS)

    def delete_history(self, chat_id):
        self._command("DEL", history_key(chat_id))

    # ---- Progression du questionnaire CV (un petit dictionnaire JSON par chat) -----------------
    def get_state(self, chat_id):
        raw = self._command("GET", state_key(chat_id))
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def set_state(self, chat_id, state):
        payload = json.dumps(state, ensure_ascii=False)
        if len(payload.encode("utf-8")) > MAX_STORED_HISTORY_BYTES:
            raise StoreError("Progression trop volumineuse pour le stockage")
        self._command("SET", state_key(chat_id), payload, "EX", HISTORY_TTL_SECONDS)

    def delete_state(self, chat_id):
        self._command("DEL", state_key(chat_id))

    # ---- Quotas -------------------------------------------------------------------------------
    def incr_with_ttl(self, key: str, ttl_seconds: int) -> int:
        """Incrémente un compteur et fixe son expiration à la création. Renvoie la nouvelle valeur."""
        value = self._command("INCR", key)
        if not isinstance(value, int) or isinstance(value, bool):
            raise StoreError("Stockage : compteur invalide")
        if value == 1:
            self._command("EXPIRE", key, int(ttl_seconds))
        return value


class FakeStore:
    """Double en mémoire, pour les tests : même interface qu'UpstashStore, plus un interrupteur de panne."""

    def __init__(self):
        self.histories = {}
        self.states = {}
        self.counters = {}
        self.ttls = {}
        self.down = False

    def _check(self):
        if self.down:
            raise StoreError("Stockage injoignable (simulé)")

    def get_history(self, chat_id):
        self._check()
        value = self.histories.get(history_key(chat_id))
        return None if value is None else json.loads(json.dumps(value))

    def set_history(self, chat_id, messages):
        self._check()
        self.histories[history_key(chat_id)] = json.loads(json.dumps(messages))

    def delete_history(self, chat_id):
        self._check()
        self.histories.pop(history_key(chat_id), None)

    def get_state(self, chat_id):
        self._check()
        value = self.states.get(state_key(chat_id))
        return None if value is None else json.loads(json.dumps(value))

    def set_state(self, chat_id, state):
        self._check()
        self.states[state_key(chat_id)] = json.loads(json.dumps(state))

    def delete_state(self, chat_id):
        self._check()
        self.states.pop(state_key(chat_id), None)

    def incr_with_ttl(self, key, ttl_seconds):
        self._check()
        self.counters[key] = self.counters.get(key, 0) + 1
        if self.counters[key] == 1:
            self.ttls[key] = ttl_seconds
        return self.counters[key]


def make_store(env=None):
    """Crée le stockage persistant si les deux variables sont définies ; sinon None (mode mémoire locale).
    Une configuration invalide ne bloque jamais le démarrage : on journalise et on reste en mémoire locale."""
    source = os.environ if env is None else env
    url = (source.get("UPSTASH_REDIS_REST_URL") or "").strip()
    token = (source.get("UPSTASH_REDIS_REST_TOKEN") or "").strip()
    if not url and not token:
        return None
    try:
        return UpstashStore(url, token)
    except ValueError:
        logger.error("Stockage Upstash mal configuré (URL https et jeton requis) : mémoire locale utilisée")
        return None
