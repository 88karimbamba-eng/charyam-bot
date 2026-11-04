"""Volets « Ebooks » et « Boutique » : textes et boutons de redirection.

Le bot n'encaisse rien pour ces deux volets : il envoie le client vers la boutique Chariow (ebooks) ou vers
la boutique WhatsApp (vêtements, accessoires, savon bio). Aucun prix ni aucun titre n'est écrit ici : ils
changent, et c'est la boutique qui fait foi. Un lien absent ou mal écrit n'est jamais inventé ni remplacé :
le client est alors prévenu que le lien arrive bientôt.
"""

from urllib.parse import urlparse

DEFAULT_CHARIOW_URL = "https://zsykaits.mychariow.shop"


def clean_url(value) -> str:
    """Retourne l'adresse si c'est un lien https complet, sinon une chaîne vide."""
    value = (value or "").strip()
    if not value or any(ch.isspace() for ch in value):
        return ""
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or "." not in parsed.netloc:
        return ""
    return value


def link_keyboard(label: str, url: str):
    """Bouton-lien sous le message (Telegram l'ouvre directement). None si le lien est absent."""
    url = clean_url(url)
    if not url:
        return None
    return {"inline_keyboard": [[{"text": label, "url": url}]]}


BUTTON_LABEL_EBOOKS = "📚 Voir les ebooks"
BUTTON_LABEL_BOUTIQUE = "🛍️ Ouvrir la boutique WhatsApp"


def ebooks_text(has_link: bool) -> str:
    if not has_link:
        return "📚 Ebooks\nLa boutique d'ebooks n'est pas joignable pour le moment. Réessaie un peu plus tard."
    return (
        "📚 Ebooks\n"
        "Nos ebooks sont en vente sur notre boutique en ligne.\n"
        "1️⃣ Touche le bouton ci-dessous pour ouvrir la boutique.\n"
        "2️⃣ Choisis ton ebook et lis sa description.\n"
        "3️⃣ Le paiement et la livraison se font sur la page de la boutique, pas dans ce chat.\n\n"
        "Une question sur un ebook ? Écris-la ici."
    )


def boutique_text(has_link: bool) -> str:
    base = (
        "🛍️ Boutique : vêtements, accessoires et savon bio\n"
        "Notre boutique est sur WhatsApp.\n"
    )
    if not has_link:
        return base + "Le lien de la boutique WhatsApp arrive bientôt. Reviens ici dans quelques jours."
    return base + (
        "1️⃣ Touche le bouton ci-dessous : WhatsApp s'ouvre sur notre conversation.\n"
        "2️⃣ Dis-nous quels articles t'intéressent (vêtements, accessoires, savon bio).\n"
        "3️⃣ Tu passes ta commande directement avec nous sur WhatsApp.\n\n"
        "Une question avant de commander ? Écris-la ici."
    )
