"""Réception des Pulses Chariow (webhooks de vente) : signature, dédoublonnage, enregistrement.

Contrat vérifié dans la documentation Chariow (guides Pulses et Pulse Security) :
- en-tête x-chariow-signature = "sha256=" + HMAC-SHA256(corps brut, secret du Pulse "whsec_...") ;
- jamais de re-sérialisation du JSON : on signe les octets reçus ;
- x-pulse-delivery-id = clé d'idempotence, stable entre les tentatives ;
- réponse 2xx sous 30 s, sinon 5 tentatives puis le Pulse est désactivé automatiquement ;
- événements de vente : successful.sale, failed.sale, abandoned.sale.

Règle CHARYAM : seul un événement signé ET successful.sale (status "completed") produit un statut "payée".
Aucun e-mail ni livraison n'est déclenché ici. Le module enregistre et prévient le propriétaire.
"""

import hashlib
import hmac
import logging
import time

logger = logging.getLogger("charyam")

ORDER_TTL_SECONDS = 400 * 24 * 3600
DELIVERY_TTL_SECONDS = 30 * 24 * 3600
SALE_EVENTS = ("successful.sale", "failed.sale", "abandoned.sale")
STATUS_BY_EVENT = {"successful.sale": "paid", "failed.sale": "failed", "abandoned.sale": "abandoned"}


def compute_signature(raw: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def verify_signature(raw: bytes, received: str, secret: str) -> bool:
    if not secret or not received or not received.startswith("sha256="):
        return False
    return hmac.compare_digest(compute_signature(raw, secret), received)


def _amount(obj):
    if isinstance(obj, dict) and isinstance(obj.get("value"), (int, float)) and not isinstance(obj.get("value"), bool):
        return obj["value"], str(obj.get("currency") or "")
    return None, ""


def normalize(event: str, payload: dict) -> dict:
    """Extrait les champs utiles d'un Pulse de vente. Lève ValueError si l'identifiant de vente manque."""
    sale = payload.get("sale") if isinstance(payload.get("sale"), dict) else {}
    sale_id = sale.get("id")
    if not isinstance(sale_id, str) or not sale_id:
        raise ValueError("identifiant de vente absent")
    amount, currency = _amount(sale.get("amount"))
    product = payload.get("product") if isinstance(payload.get("product"), dict) else {}
    customer = payload.get("customer") if isinstance(payload.get("customer"), dict) else {}
    return {
        "sale_id": sale_id,
        "event": event,
        "status": STATUS_BY_EVENT[event],
        "provider_status": sale.get("status"),
        "amount": amount,
        "currency": currency,
        "product_id": product.get("id"),
        "product_name": product.get("name"),
        "customer_id": customer.get("id"),
        "customer_email": customer.get("email"),
        "is_test": sale_id.startswith("test_"),
        "received_at": int(time.time()),
    }


def order_key(sale_id: str) -> str:
    return f"charyam:order:{sale_id}"


def delivery_key(delivery_id: str) -> str:
    return f"charyam:chariow:delivery:{delivery_id}"


def owner_notice(order: dict) -> str:
    amount = "montant inconnu" if order["amount"] is None else f"{order['amount']:g} {order['currency']}".strip()
    label = {"paid": "Vente payée", "failed": "Paiement échoué", "abandoned": "Panier abandonné"}[order["status"]]
    if order["is_test"]:
        label += " (TEST Chariow)"
    return f"{label} : {order.get('product_name') or 'produit inconnu'} — {amount}. Référence {order['sale_id']}."


def process(store, event: str, payload: dict, delivery_id: str, notify, seen_local: set) -> str:
    """Traite une livraison déjà authentifiée. Renvoie : 'ignored', 'duplicate', 'recorded'.
    Le dédoublonnage utilise le stockage persistant s'il existe, sinon la mémoire locale (perdue au redémarrage)."""
    if event not in SALE_EVENTS:
        return "ignored"
    order = normalize(event, payload)
    if store is not None:
        # Ordre voulu : écrire la commande (idempotent par vente) AVANT de réserver la livraison.
        # Une panne du stockage lève StoreError avant la réservation : Chariow réessaiera.
        previous = store.get_json(order_key(order["sale_id"]))
        # Une vente déjà payée ne repasse jamais en échec ou abandon (événements reçus dans le désordre).
        if not (previous and previous.get("status") == "paid" and order["status"] != "paid"):
            store.put_json(order_key(order["sale_id"]), order, ORDER_TTL_SECONDS)
        if not store.claim(delivery_key(delivery_id), DELIVERY_TTL_SECONDS):
            return "duplicate"
        if previous and previous.get("status") == "paid" and order["status"] != "paid":
            return "ignored"
    else:
        if delivery_id in seen_local:
            return "duplicate"
        seen_local.add(delivery_id)
    notify(owner_notice(order))
    return "recorded"
