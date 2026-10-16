#!/usr/bin/env python3
"""Crée un lien de connexion OAuth Composio pour une application (à lancer sur VOTRE ordinateur).

Usage :
    export COMPOSIO_API_KEY=...        # saisi dans votre terminal, jamais dans un chat ni un dépôt
    export COMPOSIO_USER_ID=...        # identifiant stable de l'utilisateur CHARYAM (le même sur Render)
    python scripts/composio_connect.py <auth_config_id>

<auth_config_id> se crée dans le tableau de bord Composio (Auth configs) pour l'application voulue.
Le script affiche un lien : ouvrez-le, autorisez l'application, puis le script confirme la connexion.

Statut : écrit d'après la signature du SDK (connected_accounts.link / wait_for_connection) ;
NON testé contre le service réel faute de clé API pendant le développement.
"""

import os
import sys


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    api_key = os.environ.get("COMPOSIO_API_KEY", "").strip()
    user_id = os.environ.get("COMPOSIO_USER_ID", "").strip()
    if not api_key or not user_id:
        print("COMPOSIO_API_KEY et COMPOSIO_USER_ID doivent être définies dans l'environnement.")
        return 2
    from composio import Composio

    composio = Composio(api_key=api_key)
    request = composio.connected_accounts.link(user_id, sys.argv[1])
    url = getattr(request, "redirect_url", None)
    if not url:
        print("Le SDK n'a pas renvoyé de lien de connexion (redirect_url) : voir la documentation Composio.")
        return 1
    print("Ouvrez ce lien pour autoriser l'application :\n", url)
    account = request.wait_for_connection(timeout=300)
    print("Connexion établie. Statut :", getattr(account, "status", "inconnu"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
