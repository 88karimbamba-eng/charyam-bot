"""Mesure d'usage ANONYME avec PostHog : facultative, silencieuse, jamais bloquante.

Désactivée tant que POSTHOG_API_KEY n'est pas définie : le bot se comporte alors exactement comme avant
et aucune requête réseau n'est faite.

Ce qui est envoyé : un nom d'événement fixe (ex. « export_requested »), un identifiant pseudonymisé
(HMAC du numéro Telegram, jamais le numéro lui-même) et quelques propriétés d'une liste blanche
(type de bouton, format d'export…). Ce qui n'est JAMAIS envoyé : le texte des messages, les noms, les
numéros, les CV, les réponses de Claude, les liens ou les numéros de paiement.

Variables d'environnement (voir .env.example) :
  POSTHOG_API_KEY  clé « projet » PostHog (celle qui commence par phc_ ; elle ne permet que d'écrire des événements)
  POSTHOG_HOST     facultative, défaut https://us.i.posthog.com (région États-Unis ; https://eu.i.posthog.com pour l'UE)
  POSTHOG_SALT     facultative : secret de pseudonymisation ; à défaut, le jeton du bot Telegram est utilisé
"""

import datetime
import hashlib
import hmac
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx

logger = logging.getLogger("charyam.analytics")

DEFAULT_HOST = "https://us.i.posthog.com"
CAPTURE_PATH = "/i/v0/e/"
REQUEST_TIMEOUT_SECONDS = 3.0
MAX_PENDING = 50                 # au-delà, les événements sont abandonnés plutôt que de s'accumuler
MAX_VALUE_CHARS = 40

# Seules ces propriétés peuvent partir vers PostHog ; toute autre clé est ignorée.
ALLOWED_PROPERTIES = frozenset({"kind", "format", "mode", "status", "is_owner", "done", "cv"})


def _clean_properties(properties) -> dict:
    """Garde uniquement les clés autorisées, avec des valeurs courtes (texte, entier ou booléen)."""
    cleaned = {}
    for key, value in (properties or {}).items():
        if key not in ALLOWED_PROPERTIES:
            continue
        if isinstance(value, bool) or isinstance(value, int):
            cleaned[key] = value
        elif isinstance(value, str):
            cleaned[key] = value[:MAX_VALUE_CHARS]
    return cleaned


class UsageAnalytics:
    def __init__(self, salt: str = "", env=None, http=None, executor=None):
        source = os.environ if env is None else env
        self.api_key = (source.get("POSTHOG_API_KEY") or "").strip()
        host = (source.get("POSTHOG_HOST") or DEFAULT_HOST).strip().rstrip("/")
        # Une clé ne part jamais en clair : seulement vers un hôte HTTPS
        self.host = host if host.startswith("https://") else DEFAULT_HOST
        self._salt = (source.get("POSTHOG_SALT") or "").strip() or salt
        self._http = http
        self._executor = executor
        self._pending = 0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self._salt)

    def distinct_id(self, user_id) -> str:
        """Identifiant pseudonymisé et stable (HMAC) : impossible à retrouver sans le secret."""
        digest = hmac.new(self._salt.encode(), str(user_id).encode(), hashlib.sha256).hexdigest()
        return "u_" + digest[:16]

    def build_payload(self, event: str, user_id=None, properties=None, now=None) -> dict:
        moment = now or datetime.datetime.now(datetime.timezone.utc)
        props = _clean_properties(properties)
        props["$lib"] = "charyam-bot"
        props["$process_person_profile"] = False   # pas de fiche « personne » : événements anonymes
        return {
            "api_key": self.api_key,
            "event": str(event)[:MAX_VALUE_CHARS],
            "distinct_id": self.distinct_id(user_id) if user_id is not None else "u_anonyme",
            "properties": props,
            "timestamp": moment.isoformat(),
        }

    def capture(self, event: str, user_id=None, **properties) -> bool:
        """Envoie un événement en arrière-plan. Ne lève jamais d'exception ; retourne False si rien n'est parti."""
        try:
            if not self.enabled:
                return False
            with self._lock:
                if self._pending >= MAX_PENDING:
                    return False
                self._pending += 1
            payload = self.build_payload(event, user_id, properties)
            self._get_executor().submit(self._send, payload)
            return True
        except Exception as exc:  # la mesure ne doit jamais gêner un client
            logger.debug("Mesure ignorée (%s)", type(exc).__name__)
            return False

    def _get_executor(self):
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="analytics")
        return self._executor

    def _get_http(self):
        if self._http is None:
            self._http = httpx.Client(timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=REQUEST_TIMEOUT_SECONDS))
        return self._http

    def _send(self, payload: dict):
        try:
            self._get_http().post(self.host + CAPTURE_PATH, json=payload)
        except Exception as exc:
            # On n'écrit ni l'URL ni la clé dans le journal : seulement le type d'erreur
            logger.debug("Envoi PostHog échoué (%s)", type(exc).__name__)
        finally:
            with self._lock:
                self._pending = max(0, self._pending - 1)
