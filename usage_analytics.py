"""Mesure d'usage PSEUDONYMISÉE avec PostHog : facultative, silencieuse, jamais bloquante.

Désactivée tant que POSTHOG_API_KEY ET POSTHOG_SALT ne sont pas tous deux définis : le bot se comporte alors
exactement comme avant et aucune requête réseau n'est faite.

Ce qui est envoyé : un nom d'événement choisi dans une liste fermée (EVENT_NAMES), un identifiant
pseudonymisé (HMAC-SHA256 du numéro Telegram avec POSTHOG_SALT) et quelques propriétés d'une liste blanche
(type de bouton, format d'export…). Ce qui n'est JAMAIS envoyé : le numéro Telegram ou l'identifiant du chat
en clair, le texte des messages, les noms, les numéros de téléphone ou de paiement, les CV, les réponses de
Claude, les liens.

« Pseudonymisé » ne veut pas dire « anonyme » : quiconque détient POSTHOG_SALT peut recalculer l'identifiant
d'une personne dont il connaît le numéro Telegram, et les événements d'une même personne restent reliables
entre eux. Voir docs/MESURE_USAGE.md.

Variables d'environnement (voir .env.example) :
  POSTHOG_API_KEY  clé « projet » PostHog (celle qui commence par phc_ ; elle ne permet que d'écrire des événements)
  POSTHOG_SALT     secret de pseudonymisation, 16 caractères minimum, INDÉPENDANT du jeton Telegram
  POSTHOG_HOST     facultative, défaut https://us.i.posthog.com (région États-Unis ; https://eu.i.posthog.com pour l'UE)

Envoi et fiabilité : l'envoi se fait dans un fil d'arrière-plan. Une réponse 2xx de PostHog = événement accepté ;
429, 5xx, 408 et erreurs réseau = nouvelle tentative limitée (MAX_ATTEMPTS au total) ; les autres erreurs HTTP
(400, 401, 403…) sont définitives et ne sont pas retentées. Une clé refusée (401/403) suspend la mesure
jusqu'au redémarrage. `capture()` renvoie True quand l'événement est mis en file, PAS quand PostHog l'a accepté :
le résultat réel est dans `snapshot()` (accepted, rejected, failed…).
"""

import datetime
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

logger = logging.getLogger("charyam.analytics")

DEFAULT_HOST = "https://us.i.posthog.com"
CAPTURE_PATH = "/i/v0/e/"
REQUEST_TIMEOUT_SECONDS = 3.0
MAX_PENDING = 50                 # au-delà, les événements sont abandonnés plutôt que de s'accumuler
MAX_VALUE_CHARS = 40
MIN_SALT_CHARS = 16

MAX_ATTEMPTS = 3                 # 1 envoi + 2 nouvelles tentatives au plus
BACKOFF_SECONDS = (0.5, 1.5)     # attente avant la 2e puis la 3e tentative
MAX_RETRY_AFTER_SECONDS = 5.0    # plafond de l'attente demandée par PostHog (en-tête Retry-After)
LOG_INTERVAL_SECONDS = 300.0     # un même échec n'est journalisé en WARNING qu'une fois par intervalle

TRANSIENT_STATUSES = frozenset({408, 429})   # + tous les 5xx
AUTH_STATUSES = frozenset({401, 403})

# Liste fermée : tout autre nom d'événement est refusé. Volontairement ABSENTS : « payment_confirmed » et
# « delivery_completed », tant que le code n'a pas de preuve fiable de paiement et de livraison (voir docs/MESURE_USAGE.md).
EVENT_NAMES = frozenset({
    "menu_used",
    "cv_questionnaire_reply",
    "ai_reply_sent",
    "export_requested",
    "payment_requested",
})

# Seules ces propriétés peuvent partir vers PostHog ; toute autre clé est ignorée.
ALLOWED_PROPERTIES = frozenset({"kind", "format", "mode", "status", "is_owner", "done", "cv"})

STAT_KEYS = ("queued", "accepted", "rejected", "failed", "retried", "dropped")


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


def _leaks_identifier(payload: dict, user_id) -> bool:
    """Vrai si l'identifiant en clair apparaît dans le nom ou les propriétés de l'événement.
    (Le distinct_id pseudonymisé est exclu : c'est un hexadécimal qui peut contenir des chiffres par hasard.)"""
    if user_id is None:
        return False
    visible = {"event": payload.get("event"), "properties": payload.get("properties")}
    return str(user_id) in json.dumps(visible, ensure_ascii=False)


class UsageAnalytics:
    def __init__(self, env=None, http=None, executor=None, sleep=time.sleep, clock=time.monotonic):
        source = os.environ if env is None else env
        self.api_key = (source.get("POSTHOG_API_KEY") or "").strip()
        host = (source.get("POSTHOG_HOST") or DEFAULT_HOST).strip().rstrip("/")
        # Une clé ne part jamais en clair : seulement vers un hôte HTTPS
        self.host = host if host.startswith("https://") else DEFAULT_HOST
        # Aucun repli sur le jeton Telegram : le secret de pseudonymisation doit être indépendant
        self._salt = (source.get("POSTHOG_SALT") or "").strip()
        self._http = http
        self._executor = executor
        self._sleep = sleep
        self._clock = clock
        self._pending = 0
        self._lock = threading.Lock()
        self._auth_failed = False
        self._last_logged = {}
        self.stats = dict.fromkeys(STAT_KEYS, 0)
        if self.api_key and len(self._salt) < MIN_SALT_CHARS:
            logger.warning(
                "POSTHOG_API_KEY est défini mais POSTHOG_SALT est absent ou trop court "
                "(minimum %d caractères) : mesure d'usage désactivée.", MIN_SALT_CHARS,
            )

    @property
    def enabled(self) -> bool:
        """Configurée (clé + sel valides). N'indique pas que PostHog accepte la clé : voir `suspended`."""
        return bool(self.api_key) and len(self._salt) >= MIN_SALT_CHARS

    @property
    def suspended(self) -> bool:
        """Vrai si PostHog a refusé la clé (401/403) : plus aucun envoi jusqu'au redémarrage."""
        return self._auth_failed

    def snapshot(self) -> dict:
        """Compteurs : queued (mis en file), accepted (reçu et accepté par PostHog), rejected (refus définitif),
        failed (échec après nouvelles tentatives), retried (nouvelles tentatives), dropped (abandonné avant envoi)."""
        with self._lock:
            return dict(self.stats)

    def _count(self, key: str):
        with self._lock:
            self.stats[key] += 1

    def distinct_id(self, user_id) -> str:
        """Identifiant pseudonymisé et stable (HMAC) : non réversible sans le secret, mais PAS anonyme."""
        digest = hmac.new(self._salt.encode(), str(user_id).encode(), hashlib.sha256).hexdigest()
        return "u_" + digest[:16]

    def build_payload(self, event: str, user_id=None, properties=None, now=None) -> dict:
        moment = now or datetime.datetime.now(datetime.timezone.utc)
        props = _clean_properties(properties)
        props["$lib"] = "charyam-bot"
        props["$process_person_profile"] = False   # pas de fiche « personne » dans PostHog
        return {
            "api_key": self.api_key,
            "event": str(event)[:MAX_VALUE_CHARS],
            "distinct_id": self.distinct_id(user_id) if user_id is not None else "u_anonyme",
            "properties": props,
            "timestamp": moment.isoformat(),
        }

    def capture(self, event: str, user_id=None, **properties) -> bool:
        """Met l'événement en file pour un envoi en arrière-plan. Ne lève jamais d'exception.
        True = mis en file d'attente (et NON « accepté par PostHog » : voir snapshot()). False = rien n'est parti."""
        try:
            if not self.enabled:
                return False
            if event not in EVENT_NAMES or self._auth_failed:
                self._count("dropped")
                return False
            with self._lock:
                if self._pending >= MAX_PENDING:
                    self.stats["dropped"] += 1
                    return False
                self._pending += 1
            try:
                payload = self.build_payload(event, user_id, properties)
                if _leaks_identifier(payload, user_id):
                    raise ValueError("identifiant en clair dans la charge utile")
                self._get_executor().submit(self._send, payload)
                self._count("queued")
            except Exception as exc:
                self._release_pending()
                self._count("dropped")
                logger.debug("Mesure abandonnée (%s)", type(exc).__name__)
                return False
            return True
        except Exception as exc:  # la mesure ne doit jamais gêner un client
            logger.debug("Mesure ignorée (%s)", type(exc).__name__)
            return False

    def _release_pending(self):
        with self._lock:
            self._pending = max(0, self._pending - 1)

    def _get_executor(self):
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="analytics")
        return self._executor

    def _get_http(self):
        if self._http is None:
            self._http = httpx.Client(timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=REQUEST_TIMEOUT_SECONDS))
        return self._http

    def _post_once(self, payload: dict):
        """Un envoi. Retourne (statut HTTP ou None si erreur réseau, valeur de Retry-After ou None)."""
        try:
            response = self._get_http().post(self.host + CAPTURE_PATH, json=payload)
        except Exception as exc:
            logger.debug("Envoi PostHog impossible (%s)", type(exc).__name__)   # type seulement : ni URL ni clé
            return None, None
        status = response.status_code
        retry_after = None
        try:
            retry_after = response.headers.get("Retry-After")
        except Exception:
            pass
        return status, retry_after

    @staticmethod
    def _delay(attempt: int, retry_after) -> float:
        base = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
        try:
            asked = float(retry_after)
        except (TypeError, ValueError):
            return base
        return max(base, min(max(asked, 0.0), MAX_RETRY_AFTER_SECONDS))

    def _send(self, payload: dict) -> str:
        """Envoie avec nouvelles tentatives limitées. Retourne « accepted », « rejected » ou « failed »."""
        outcome, reason, attempts = "failed", "erreur réseau", 0
        try:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                attempts = attempt
                status, retry_after = self._post_once(payload)
                if status is not None and 200 <= status < 300:
                    outcome, reason = "accepted", "HTTP %d" % status
                    break
                reason = "erreur réseau" if status is None else "HTTP %d" % status
                if status in AUTH_STATUSES:
                    self._auth_failed = True
                transient = status is None or status in TRANSIENT_STATUSES or 500 <= status < 600
                if not transient:
                    outcome = "rejected"      # 400, 401, 403… : définitif, on ne retente pas
                    break
                if attempt == MAX_ATTEMPTS:
                    break
                self._count("retried")
                self._sleep(self._delay(attempt, retry_after))
        except Exception as exc:
            outcome, reason = "failed", type(exc).__name__
        finally:
            self._count(outcome)
            self._release_pending()
        if outcome != "accepted":
            self._log_failure(payload.get("event"), outcome, reason, attempts)
        return outcome

    def _log_failure(self, event, outcome: str, reason: str, attempts: int):
        """Journal sans clé, sans sel, sans identifiant ni contenu : nom d'événement, issue et statut seulement."""
        key = (outcome, reason)
        now = self._clock()
        with self._lock:
            last = self._last_logged.get(key)
            loud = last is None or now - last >= LOG_INTERVAL_SECONDS
            if loud:
                self._last_logged[key] = now
        message = "PostHog : événement « %s » %s (%s, %d tentative(s))"
        if reason in ("HTTP 401", "HTTP 403") and loud:
            logger.warning(message + " ; clé refusée, mesure suspendue jusqu'au redémarrage.",
                           event, outcome, reason, attempts)
            return
        logger.log(logging.WARNING if loud else logging.DEBUG, message, event, outcome, reason, attempts)
