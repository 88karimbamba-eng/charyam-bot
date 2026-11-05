"""Volets « Ebooks » et « Boutique » : textes et boutons de redirection.

Le bot n'encaisse rien pour ces deux volets : il envoie le client vers la boutique Chariow (ebooks) ou vers
la boutique WhatsApp (vêtements, accessoires, savon bio). Aucun prix n'est écrit ici : ils changent, et c'est
la boutique qui fait foi. Les ebooks sont présentés un par un (titre, description, lien) dans la liste EBOOKS.
Un lien absent ou mal écrit n'est jamais inventé ni remplacé : le client est alors prévenu que le lien arrive
bientôt.
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

# Fiches des ebooks publiés sur Chariow (vérifiées sur l'API et la page publique de la boutique le 5 nov. 2026 :
# 4 produits publiés, chacun avec un fichier PDF). Aucun prix n'est écrit ici :
# les promotions changent, la page Chariow de chaque produit fait foi. Pour ajouter ou retirer un ebook,
# modifier cette liste (un lien qui n'est pas un https complet est ignoré, jamais remplacé).
EBOOKS = (
    {
        "emoji": "🇬🇧",
        "titre": "1000 Mots pour apprendre l'anglais",
        "bouton": "🛒 Acheter : 1000 Mots d'anglais",
        "description": "Parle anglais sans cours compliqués. Plus de 1000 mots essentiels en 27 thèmes du quotidien "
                       "(famille, travail, voyages, argent…), avec traduction et exemples, à lire sur ton téléphone.",
        "url": "https://zsykaits.mychariow.shop/prd_6rbfwkby",
    },
    {
        "emoji": "⌨️",
        "titre": "Raccourcis Word & Excel : le guide de poche",
        "bouton": "🛒 Acheter : Raccourcis Word & Excel",
        "description": "Gagne du temps à chaque document. Plus de 100 raccourcis classés par situation, "
                       "plus une fiche de 30 raccourcis à imprimer. Idéal pour les étudiants, les candidats à un emploi "
                       "et les employés de bureau.",
        "url": "https://zsykaits.mychariow.shop/prd_5g9kzjwv",
    },
    {
        "emoji": "🚗",
        "titre": "Le Guide Complet du Code de la Route",
        "bouton": "🛒 Acheter : Code de la Route (CI)",
        "description": "Prépare ton code de la route en français simple. 17 chapitres, le permis à points, "
                       "115 QCM corrigés et un examen blanc de 25 questions pour tester ton niveau.",
        "url": "https://zsykaits.mychariow.shop/prd_0bvt51bh",
    },
    {
        "emoji": "🍲",
        "titre": "Gastronomie ivoirienne : 90 plats pour 90 jours",
        "bouton": "🛒 Acheter : Gastronomie ivoirienne",
        "description": "Un plat ivoirien par jour pendant 90 jours. Pour chaque plat : ingrédients, quantités, "
                       "étapes détaillées, astuces et un QCM pour tester ce que tu as appris.",
        "url": "https://zsykaits.mychariow.shop/prd_2j838h8j",
    },
)


def valid_ebooks():
    """Les fiches dont le lien est un https complet (les autres sont ignorées, jamais inventées)."""
    return [e for e in EBOOKS if clean_url(e.get("url"))]


def ebooks_menu(store_url: str):
    """Message du volet Ebooks : (texte, clavier de boutons-liens). Clavier None si aucun lien n'est valide.

    Chaque ebook a sa description et son propre bouton ; un dernier bouton ouvre toute la boutique Chariow."""
    items = valid_ebooks()
    store_url = clean_url(store_url)
    if not items:
        return ebooks_text(bool(store_url)), link_keyboard(BUTTON_LABEL_EBOOKS, store_url)
    lines = ["📚 Nos ebooks AFRICA MINDSET\nChoisis celui qui te convient 👇\n"]
    rows = []
    for item in items:
        lines.append(f"{item.get('emoji', '📘')} {item['titre']}\n{item['description']}\n")
        rows.append([{"text": item["bouton"], "url": clean_url(item["url"])}])
    if store_url:
        rows.append([{"text": BUTTON_LABEL_EBOOKS, "url": store_url}])
    lines.append("Touche « Acheter » sous l'ebook qui t'intéresse : le prix est affiché sur sa page. "
                 "Le paiement et la livraison se font sur la page de la boutique, pas dans ce chat.\n\n"
                 "Une question sur un ebook ? Écris-la ici.")
    return "\n".join(lines), {"inline_keyboard": rows}


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
