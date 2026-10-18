"""Vérification du contrôle d'authentification de /webhook sur un déploiement RÉEL (niveau 3).

Usage :  python3 scripts/verify_webhook.py https://charyam-bot.onrender.com/webhook
Option : définir WEBHOOK_SECRET_TO_TEST dans l'environnement pour vérifier aussi qu'un secret valide
         est accepté. La valeur n'est jamais affichée ni journalisée ; ne la passez pas en argument.

Traitement contrôlé : le corps envoyé est {"update_id": 0}, sans message. Même si le contrôle était
inactif, l'application l'ignore (« pas de message ») : aucun appel à Claude, aucun envoi Telegram,
aucun coût. Résultats attendus : sans secret 403, mauvais secret 403, secret valide 200.
Code de sortie : 0 si tout est conforme, 1 sinon.
"""

import os
import sys

HEADER = "X-Telegram-Bot-Api-Secret-Token"
BODY = {"update_id": 0}
WRONG = "secret-incorrect-pour-test"


def run_checks(post, valid_secret=None):
    """post(headers: dict) -> code HTTP. Retourne la liste (libellé, attendu, obtenu, conforme)."""
    checks = [("sans secret", 403, post({})), ("mauvais secret", 403, post({HEADER: WRONG}))]
    if valid_secret:
        checks.append(("secret valide", 200, post({HEADER: valid_secret})))
    return [(label, expected, got, got == expected) for label, expected, got in checks]


def main(argv, env=None):
    env = os.environ if env is None else env
    if len(argv) != 2 or not argv[1].startswith("https://"):
        print("Usage : python3 scripts/verify_webhook.py https://<service>/webhook")
        return 2
    import httpx
    url = argv[1]

    def post(headers):
        return httpx.post(url, json=BODY, headers=headers, timeout=60).status_code

    results = run_checks(post, (env.get("WEBHOOK_SECRET_TO_TEST") or "").strip() or None)
    for label, expected, got, ok in results:
        print(f"{'OK  ' if ok else 'ÉCHEC'} {label}: attendu {expected}, obtenu {got}")
    if not env.get("WEBHOOK_SECRET_TO_TEST"):
        print("Info : le cas « secret valide » n'a pas été testé (WEBHOOK_SECRET_TO_TEST non défini).")
    return 0 if all(ok for *_, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
