"""Accès réservé aux testeurs et limite de messages par utilisateur.

Principe : par défaut (OPEN_ACCESS non défini ou « 1 ») le bot est OUVERT à tout le monde, avec des limites
par personne et un plafond GLOBAL de messages par jour pour les personnes hors liste (OPEN_DAILY_CAP) :
c'est ce plafond qui borne la dépense de l'API. Avec OPEN_ACCESS=0, seuls le propriétaire et les
identifiants Telegram listés dans ALLOWED_USER_IDS (séparés par des virgules) peuvent parler au bot. Chaque testeur est limité en
nombre de messages par minute et par jour, pour maîtriser le coût de l'API Anthropic. Le
propriétaire n'est jamais limité.

Compteurs : en mémoire par défaut (remis à zéro au redémarrage du service, ce qui arrive après
environ 15 minutes d'inactivité sur le plan gratuit de Render). Si un stockage persistant est fourni
(store.py, Upstash), les compteurs y sont conservés : fenêtres fixes d'une minute et d'un jour (UTC).
Si ce stockage est indisponible, les messages des testeurs sont REFUSÉS avec un message clair (jamais
de désactivation silencieuse de la limite) ; le propriétaire n'est jamais limité.

Dans tous les cas, le plafond de dépense du compte Anthropic reste indispensable.
"""

import logging
import os
import re
import threading
import time
from collections import OrderedDict, deque

import store as store_module

logger = logging.getLogger("charyam")

DEFAULT_PER_MINUTE = 6
DEFAULT_PER_DAY = 60
DEFAULT_OPEN_DAILY_CAP = 300       # messages par jour, TOUTES personnes hors liste confondues
NOTICE_INTERVAL_SECONDS = 600      # un même message d'information n'est renvoyé qu'une fois par 10 min
MAX_TRACKED_USERS = 2000


def parse_allowed_ids(raw: str, owner_id: int) -> frozenset:
    """Lit « 123, 456;789 » et renvoie l'ensemble des identifiants valides, propriétaire inclus.
    Tout ce qui n'est pas un entier positif est ignoré (jamais d'erreur au démarrage)."""
    ids = {owner_id}
    for part in re.split(r"[,\s;]+", (raw or "").strip()):
        if part.isdigit() and int(part) > 0:
            ids.add(int(part))
    return frozenset(ids)


def _int_env(name: str, default: int, env=None) -> int:
    source = os.environ if env is None else env
    try:
        value = int(str(source.get(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


class AccessControl:
    def __init__(self, owner_id: int, env=None, store=None):
        source = os.environ if env is None else env
        self.owner_id = owner_id
        self.store = store
        self.allowed = parse_allowed_ids(source.get("ALLOWED_USER_IDS", ""), owner_id)
        self.per_minute = _int_env("USER_MESSAGES_PER_MINUTE", DEFAULT_PER_MINUTE, source)
        self.per_day = _int_env("USER_MESSAGES_PER_DAY", DEFAULT_PER_DAY, source)
        self.open_access = str(source.get("OPEN_ACCESS", "1")).strip().lower() not in ("0", "false", "non", "no")
        self.open_daily_cap = _int_env("OPEN_DAILY_CAP", DEFAULT_OPEN_DAILY_CAP, source)
        self._open_day = None
        self._open_count = 0
        self._lock = threading.Lock()
        self._hits = OrderedDict()        # identifiant -> horodatages des messages des dernières 24 h
        self._notices = OrderedDict()     # (identifiant, type) -> dernier envoi d'un message d'information

    def is_listed(self, user_id) -> bool:
        """Propriétaire ou identifiant de la liste ALLOWED_USER_IDS."""
        return isinstance(user_id, int) and not isinstance(user_id, bool) and user_id in self.allowed

    def is_allowed(self, user_id) -> bool:
        """Accès au bot : toute personne identifiable si l'accès est ouvert, sinon la liste seulement."""
        if self.is_listed(user_id):
            return True
        return self.open_access and isinstance(user_id, int) and not isinstance(user_id, bool) and user_id > 0

    def _check_open_cap(self, now: float):
        """Plafond global des messages de personnes hors liste. Retourne (True, "") ou (False, kind)."""
        day = int(now // 86400)
        if self.store is not None:
            try:
                count = self.store.incr_with_ttl(store_module.quota_key("opencap", 0, day), 90000)
            except store_module.StoreError as exc:
                logger.error("Plafond global : stockage indisponible, message refusé (%s)", exc)
                return False, "indisponible"
            return (True, "") if count <= self.open_daily_cap else (False, "plafond")
        with self._lock:
            if self._open_day != day:
                self._open_day, self._open_count = day, 0
            if self._open_count >= self.open_daily_cap:
                return False, "plafond"
            self._open_count += 1
            return True, ""

    def is_owner(self, user_id) -> bool:
        return user_id == self.owner_id

    def check_rate(self, user_id: int, now: float = None):
        """Enregistre un message et renvoie (True, "") s'il passe, sinon (False, « minute » ou « jour »).
        Un message refusé n'est pas compté."""
        if self.is_owner(user_id):
            return True, ""
        now = time.time() if now is None else now
        allowed, kind = self._check_user_rate(user_id, now)
        if allowed and not self.is_listed(user_id):
            return self._check_open_cap(now)
        return allowed, kind

    def _check_user_rate(self, user_id: int, now: float):
        if self.store is not None:
            return self._check_rate_persistent(user_id, now)
        with self._lock:
            hits = self._hits.get(user_id)
            if hits is None:
                hits = deque()
                self._hits[user_id] = hits
            else:
                self._hits.move_to_end(user_id)
            while hits and hits[0] <= now - 86400:
                hits.popleft()
            if len(hits) >= self.per_day:
                return False, "jour"
            if sum(1 for stamp in hits if stamp > now - 60) >= self.per_minute:
                return False, "minute"
            hits.append(now)
            while len(self._hits) > MAX_TRACKED_USERS:
                self._hits.popitem(last=False)
            return True, ""

    def _check_rate_persistent(self, user_id: int, now: float):
        """Compteurs dans le stockage persistant. La minute est vérifiée avant le jour, pour qu'un
        message refusé à la minute ne consomme pas le quota du jour."""
        try:
            minute_count = self.store.incr_with_ttl(
                store_module.quota_key("min", user_id, int(now // 60)), 120
            )
            if minute_count > self.per_minute:
                return False, "minute"
            day_count = self.store.incr_with_ttl(
                store_module.quota_key("day", user_id, int(now // 86400)), 90000
            )
            if day_count > self.per_day:
                return False, "jour"
            return True, ""
        except store_module.StoreError as exc:
            logger.error("Quotas : stockage indisponible, message refusé (%s)", exc)
            return False, "indisponible"

    def should_notify(self, user_id, kind: str, now: float = None) -> bool:
        """Vrai si on peut envoyer ce message d'information (évite de répondre en boucle)."""
        now = time.time() if now is None else now
        key = (user_id, kind)
        with self._lock:
            last = self._notices.get(key)
            if last is not None and now - last < NOTICE_INTERVAL_SECONDS:
                return False
            self._notices[key] = now
            self._notices.move_to_end(key)
            while len(self._notices) > MAX_TRACKED_USERS:
                self._notices.popitem(last=False)
            return True


def refusal_text(user_id) -> str:
    return (
        "Ce service est en phase de test, sur invitation. "
        f"Ton identifiant Telegram est {user_id}. "
        "Transmets-le à l'administrateur pour demander l'accès."
    )


def rate_limit_text(kind: str) -> str:
    if kind == "indisponible":
        return "Le service est momentanément indisponible. Réessaie dans quelques minutes."
    if kind == "plafond":
        return "Le service est très sollicité aujourd'hui. Réessaie demain, ou contacte l'administrateur."
    if kind == "jour":
        return "Tu as atteint la limite de messages pour aujourd'hui. Réessaie demain."
    return "Tu écris trop vite. Attends une minute puis réessaie."
