"""Porte de paiement manuelle : un client (tout sauf le propriétaire) ne reçoit son fichier Word/PDF
qu'après un paiement VALIDÉ par le propriétaire.

Règle CHARYAM : jamais de livraison sans paiement vérifié. Ici la vérification est humaine :
1. le client touche « Word » ou « PDF » : le bot lui donne les consignes de paiement ;
2. le client paie, puis envoie /paye <référence de transaction> ;
3. le propriétaire reçoit la déclaration et répond /valider <identifiant> (ou /refuser <identifiant>) ;
4. le client est prévenu et peut télécharger son fichier pendant PAYMENT_VALID_HOURS heures.

Sécurité :
- sans consignes de paiement configurées (PAYMENT_INSTRUCTIONS), les clients ne reçoivent AUCUN fichier
  (jamais de livraison gratuite par oubli de configuration) ;
- le propriétaire n'est jamais soumis au paiement ;
- l'état est gardé en mémoire : après un redémarrage du service, les validations sont perdues et il faut
  les redonner (on préfère refuser à tort que livrer sans paiement).
"""

import re
import threading
import time
from collections import OrderedDict

DEFAULT_VALID_HOURS = 24
MAX_TRACKED_CHATS = 1000
MAX_REFERENCE_LENGTH = 60

PAYMENT_COMMANDS = ("/paye", "/valider", "/refuser", "/paiements")
OWNER_ONLY_COMMANDS = ("/valider", "/refuser", "/paiements")

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

NOT_CONFIGURED_TEXT = (
    "Le téléchargement des fichiers n'est pas encore ouvert pour les clients. "
    "L'administrateur te contactera."
)
PROOF_USAGE_TEXT = (
    "Après ton paiement, envoie /paye suivi de ta référence de transaction "
    "(exemple : /paye ID123456)."
)


def clean_reference(raw: str) -> str:
    """Référence de transaction nettoyée : sans caractères de contrôle, espaces réduits, longueur bornée."""
    text = _CONTROL_CHARS.sub(" ", raw or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_REFERENCE_LENGTH]


def parse_chat_id(raw: str):
    """Identifiant Telegram positif tapé par le propriétaire, ou None."""
    value = (raw or "").strip()
    return int(value) if value.isdigit() and 0 < int(value) < 10 ** 15 else None


class PaymentGate:
    def __init__(self, owner_id: int, instructions: str = "", valid_hours: int = DEFAULT_VALID_HOURS):
        self.owner_id = owner_id
        self.instructions = (instructions or "").strip()
        self.valid_seconds = max(1, int(valid_hours)) * 3600
        self._lock = threading.Lock()
        self._pending = OrderedDict()     # chat_id -> (référence, horodatage de la déclaration)
        self._paid_until = {}             # chat_id -> horodatage de fin de validité

    @property
    def configured(self) -> bool:
        return bool(self.instructions)

    def is_owner(self, user_id) -> bool:
        return user_id == self.owner_id

    def can_export(self, user_id, chat_id, now: float = None) -> bool:
        """Le propriétaire exporte toujours ; un client seulement si le paiement est validé et non expiré."""
        if self.is_owner(user_id):
            return True
        if not self.configured:
            return False
        now = time.time() if now is None else now
        with self._lock:
            until = self._paid_until.get(chat_id)
            if until is None:
                return False
            if now >= until:
                self._paid_until.pop(chat_id, None)
                return False
            return True

    def payment_request_text(self) -> str:
        if not self.configured:
            return NOT_CONFIGURED_TEXT
        return f"Pour recevoir ton fichier, effectue le paiement :\n{self.instructions}\n\n{PROOF_USAGE_TEXT}"

    def declare(self, chat_id, reference: str, now: float = None):
        """Enregistre la déclaration de paiement d'un client. Retourne la référence nettoyée, ou None si vide."""
        reference = clean_reference(reference)
        if not reference:
            return None
        now = time.time() if now is None else now
        with self._lock:
            self._pending.pop(chat_id, None)
            self._pending[chat_id] = (reference, now)
            while len(self._pending) > MAX_TRACKED_CHATS:
                self._pending.popitem(last=False)
        return reference

    def approve(self, chat_id, now: float = None) -> float:
        """Valide le paiement : retourne l'horodatage de fin de validité."""
        now = time.time() if now is None else now
        with self._lock:
            self._pending.pop(chat_id, None)
            until = now + self.valid_seconds
            self._paid_until[chat_id] = until
            while len(self._paid_until) > MAX_TRACKED_CHATS:
                self._paid_until.pop(next(iter(self._paid_until)))
            return until

    def reject(self, chat_id) -> bool:
        """Refuse la déclaration et retire tout accès. Retourne True s'il y avait quelque chose à retirer."""
        with self._lock:
            had_pending = self._pending.pop(chat_id, None) is not None
            had_paid = self._paid_until.pop(chat_id, None) is not None
            return had_pending or had_paid

    def pending_summary(self) -> list:
        """Déclarations en attente : liste de (chat_id, référence)."""
        with self._lock:
            return [(chat_id, ref) for chat_id, (ref, _) in self._pending.items()]
