"""Test réel CONTRÔLÉ de la mesure d'usage, avec un projet PostHog de TEST et des identifiants FICTIFS.

N'utilise aucune donnée de client : les « utilisateurs » sont des numéros inventés (ci-dessous).
N'affiche jamais la clé ni le sel. Ne touche ni Telegram, ni Render, ni GitHub.

Usage (variables dans le terminal, jamais dans un fichier du dépôt) :
  POSTHOG_API_KEY=... POSTHOG_SALT=... python scripts/posthog_test_reel.py --confirm-projet-de-test
  POSTHOG_API_KEY=... POSTHOG_SALT=... python scripts/posthog_test_reel.py --confirm-projet-de-test --cle-invalide
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import usage_analytics  # noqa: E402

FAUX_UTILISATEUR = 999000111      # numéro fictif, sans lien avec un vrai compte Telegram
FAUX_PROPRIETAIRE = 999000222


class Immediat:
    """Exécute l'envoi tout de suite pour pouvoir lire le résultat avant de quitter."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


def main(argv) -> int:
    if "--confirm-projet-de-test" not in argv:
        print("Refusé : ajoutez --confirm-projet-de-test pour confirmer que la clé est celle d'un projet PostHog de TEST.")
        return 2
    env = dict(os.environ)
    if "--cle-invalide" in argv:
        env["POSTHOG_API_KEY"] = "phc_cle_volontairement_invalide_pour_test"
    analytics = usage_analytics.UsageAnalytics(env=env, executor=Immediat())
    if not analytics.enabled:
        print("Mesure désactivée : POSTHOG_API_KEY et POSTHOG_SALT (16 caractères minimum) sont requis.")
        return 2
    print("Hôte :", analytics.host)
    print("Identifiant pseudonymisé du faux utilisateur :", analytics.distinct_id(FAUX_UTILISATEUR))
    analytics.capture("menu_used", FAUX_UTILISATEUR, kind="aide", is_owner=False)
    analytics.capture("cv_questionnaire_reply", FAUX_UTILISATEUR, done=False)
    analytics.capture("export_requested", FAUX_UTILISATEUR, format="pdf")
    analytics.capture("payment_requested", FAUX_UTILISATEUR, format="pdf")
    analytics.capture("ai_reply_sent", FAUX_PROPRIETAIRE, cv=True, is_owner=True)
    stats = analytics.snapshot()
    print("Compteurs :", stats)
    print("Mesure suspendue :", analytics.suspended)
    if "--cle-invalide" in argv:
        ok = analytics.suspended and stats["accepted"] == 0 and stats["rejected"] == 1
        print("RÉSULTAT clé invalide :", "OK (401/403 détecté, mesure suspendue)" if ok else "ANOMALIE")
    else:
        ok = stats["accepted"] == 5 and stats["rejected"] == stats["failed"] == 0
        print("RÉSULTAT :", "OK (5 événements acceptés par PostHog)" if ok else "ANOMALIE : voir les compteurs")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
