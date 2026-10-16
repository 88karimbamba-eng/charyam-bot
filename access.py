"""Accès réservé aux testeurs et limite de messages par utilisateur.

Principe : seul le propriétaire et les identifiants Telegram listés dans la variable d'environnement
ALLOWED_USER_IDS (séparés par des virgules) peuvent parler au bot. Chaque testeur est limité en
nombre de messages par minute et par jour, pour maîtriser le coût de l'API Anthropic. Le
propriétaire n'est jamais limité.

Limites connues : les compteurs sont en mémoire (remis à zéro au redémarrage du service, ce qui
arrive après environ 15 minutes d'inactivité sur le plan gratuit de Render). La limite protège donc
d'un emballement, pas d'un abus déterminé : le plafond de dépense du compte Anthropic reste
indispensable.
"""

import os
import re
import threading
import time
from collections import OrderedDict, deque

DEFAULT_PER_MINUTE = 6
DEFAULT_PER_DAY = 60
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
    def __init__(self, owner_id: int, env=None):
        source = os.environ if env is None else env
        self.owner_id = owner_id
        self.allowed = parse_allowed_ids(source.get("ALLOWED_USER_IDS", ""), owner_id)
        self.per_minute = _int_env("USER_MESSAGES_PER_MINUTE", DEFAULT_PER_MINUTE, source)
        self.per_day = _int_env("USER_MESSAGES_PER_DAY", DEFAULT_PER_DAY, source)
        self._lock = threading.Lock()
        self._hits = OrderedDict()        # identifiant -> horodatages des messages des dernières 24 h
        self._notices = OrderedDict()     # (identifiant, type) -> dernier envoi d'un message d'information

    def is_allowed(self, user_id) -> bool:
        return isinstance(user_id, int) and not isinstance(user_id, bool) and user_id in self.allowed

    def is_owner(self, user_id) -> bool:
        return user_id == self.owner_id

    def check_rate(self, user_id: int, now: float = None):
        """Enregistre un message et renvoie (True, "") s'il passe, sinon (False, « minute » ou « jour »).
        Un message refusé n'est pas compté."""
        if self.is_owner(user_id):
            return True, ""
        now = time.time() if now is None else now
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
    if kind == "jour":
        return "Tu as atteint la limite de messages pour aujourd'hui. Réessaie demain."
    return "Tu écris trop vite. Attends une minute puis réessaie."
