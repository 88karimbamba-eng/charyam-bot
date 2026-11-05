"""Test réel CONTRÔLÉ de la mesure d'usage, avec un projet PostHog de TEST et des identifiants FICTIFS.

Ce script n'utilise que des numéros inventés. Il n'affiche JAMAIS la clé ni le sel et ne touche ni Telegram,
ni Render, ni GitHub. Il ne lit aucun fichier .env.

Variables d'environnement (à saisir dans PowerShell, voir docs/TEST_REEL_POSTHOG.md) :
  POSTHOG_API_KEY  clé « projet » de l'organisation PostHog de TEST (commence par phc_)
  POSTHOG_SALT     sel de test, 16 caractères minimum, différent de tout sel de production
  POSTHOG_HOST     OBLIGATOIRE, aucune région par défaut : https://us.i.posthog.com ou https://eu.i.posthog.com

Commandes :
  --a-blanc                      construit et affiche les 5 événements, SANS réseau et SANS lire vos variables
  --confirm-projet-de-test       envoie les 5 événements fictifs (confirmation manuelle demandée)
  --confirm-projet-de-test --cle-invalide
                                 envoie UNE requête avec une fausse clé (la vraie clé n'est pas utilisée)

LIMITE IMPORTANTE : ni ce script ni l'API d'envoi de PostHog ne permettent de savoir à quelle organisation
appartient une clé. La séparation test / production repose sur votre confirmation manuelle ci-dessous et sur la
vérification faite après coup dans PostHog.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import usage_analytics  # noqa: E402

FAUX_UTILISATEUR = 999000111      # numéro fictif, sans lien avec un vrai compte Telegram
FAUX_PROPRIETAIRE = 999000222     # numéro fictif présenté comme « propriétaire » du bot
ENVIRONNEMENT = "TEST"
CONFIRMATION_ATTENDUE = "charyam-test"
FAUSSE_CLE = "phc_cle_volontairement_invalide_pour_test"
CLE_ET_SEL_DEMO = ("phc_demo_a_blanc_sans_reseau", "sel-de-demonstration-a-blanc-0000")

# Seuls hôtes acceptés : comparaison exacte, pas de « commence par » (voir docs/TEST_REEL_POSTHOG.md).
HOTES_AUTORISES = {
    "https://us.i.posthog.com": "États-Unis",
    "https://eu.i.posthog.com": "Europe",
}

# Les 5 événements, avec exactement les propriétés que le bot envoie pour chacun (is_owner partout, comme _track).
EVENEMENTS = (
    ("menu_used", FAUX_UTILISATEUR, {"kind": "aide"}),
    ("cv_questionnaire_reply", FAUX_UTILISATEUR, {"done": False}),
    ("export_requested", FAUX_UTILISATEUR, {"format": "pdf"}),
    ("payment_requested", FAUX_UTILISATEUR, {"format": "pdf"}),
    ("ai_reply_sent", FAUX_PROPRIETAIRE, {"cv": True}),
)


class Immediat:
    """Exécute l'envoi tout de suite pour pouvoir lire le résultat avant de quitter."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


def _erreur(message: str) -> int:
    print("REFUSÉ : " + message)
    return 2


def valider_hote(valeur):
    """Retourne (hôte normalisé, None) ou (None, message d'erreur). Aucune région par défaut."""
    brut = (valeur or "").strip()
    if not brut:
        return None, ("POSTHOG_HOST est obligatoire (aucune région par défaut). Utilisez "
                      + " ou ".join(HOTES_AUTORISES) + ".")
    if not brut.lower().startswith("https://"):
        return None, "POSTHOG_HOST doit utiliser HTTPS."
    hote = brut.rstrip("/").lower()
    if hote not in HOTES_AUTORISES:
        return None, "POSTHOG_HOST non autorisé. Hôtes acceptés : " + ", ".join(HOTES_AUTORISES) + "."
    return hote, None


def evenements_a_envoyer():
    """Les 5 événements, avec is_owner cohérent (vrai seulement pour le faux propriétaire)."""
    return [(nom, utilisateur, {**props, "is_owner": utilisateur == FAUX_PROPRIETAIRE})
            for nom, utilisateur, props in EVENEMENTS]


def a_blanc() -> int:
    """Affiche les 5 charges utiles exactes, sans réseau et sans lire POSTHOG_API_KEY / POSTHOG_SALT."""
    cle, sel = CLE_ET_SEL_DEMO
    analytics = usage_analytics.UsageAnalytics(
        env={"POSTHOG_API_KEY": cle, "POSTHOG_SALT": sel, "POSTHOG_HOST": "https://eu.i.posthog.com"})
    print("MODE À BLANC : aucune requête réseau, vos variables d'environnement ne sont pas lues.")
    print("Un sel de démonstration public est utilisé : l'identifiant ci-dessous n'est donc pas celui du vrai test.")
    print("La clé est remplacée par *** : à l'envoi réel, elle ne s'affiche jamais.\n")
    for nom, utilisateur, props in evenements_a_envoyer():
        charge = analytics.build_payload(nom, utilisateur, props)
        charge["api_key"] = "***"
        print(json.dumps(charge, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _presence(valeur):
    """« absente » (non définie), « vide » (définie mais vide) ou « présente »."""
    if valeur is None:
        return "absente"
    return "vide" if not str(valeur).strip() else "présente"


def controler_variables(env, invalide=False):
    """Contrôle LOCAL des 3 variables, sans réseau. Retourne (lignes, corrections, valeurs).
    lignes : (variable, présence, contrôle, PASS/FAIL/N/A). Les valeurs secrètes ne sont jamais mises dans les lignes :
    seulement des oui/non et la LONGUEUR du sel. L'hôte n'est affiché que s'il est autorisé."""
    lignes, corrections = [], []

    brute = env.get("POSTHOG_API_KEY")
    cle = (brute or "").strip()
    if invalide:
        lignes.append(("POSTHOG_API_KEY", _presence(brute), "non utilisée : une fausse clé de test sert à la place", "N/A"))
    elif not cle:
        lignes.append(("POSTHOG_API_KEY", _presence(brute), "format phc_ : non vérifiable", "FAIL"))
        corrections.append("POSTHOG_API_KEY : définir la clé de projet du projet de TEST (commence par phc_).")
    elif not cle.startswith("phc_") or len(cle) < 12:
        lignes.append(("POSTHOG_API_KEY", "présente", "format phc_ : NON respecté", "FAIL"))
        corrections.append("POSTHOG_API_KEY : le format doit être celui d'une clé de projet (phc_…).")
    else:
        lignes.append(("POSTHOG_API_KEY", "présente", "format phc_ : respecté (la valeur n'est pas affichée)", "PASS"))

    brut = env.get("POSTHOG_SALT")
    sel = (brut or "").strip()
    minimum = usage_analytics.MIN_SALT_CHARS
    if not sel:
        lignes.append(("POSTHOG_SALT", _presence(brut), "longueur 0 (minimum %d : non atteint)" % minimum, "FAIL"))
    else:
        atteint = len(sel) >= minimum
        lignes.append(("POSTHOG_SALT", "présent", "longueur %d (minimum %d : %s)" % (len(sel), minimum, "atteint" if atteint else "non atteint"),
                       "PASS" if atteint else "FAIL"))
    if len(sel) < minimum:
        corrections.append("POSTHOG_SALT : %d caractères minimum." % minimum)

    valeur = env.get("POSTHOG_HOST")
    hote, probleme = valider_hote(valeur)
    if probleme:
        lignes.append(("POSTHOG_HOST", _presence(valeur), "hôte REFUSÉ (valeur non affichée par prudence)", "FAIL"))
        corrections.append(probleme)
    else:
        lignes.append(("POSTHOG_HOST", "présent", "%s : autorisé (%s)" % (hote, HOTES_AUTORISES[hote]), "PASS"))

    return lignes, corrections, {"cle": cle, "sel": sel, "hote": hote}


def afficher_tableau(lignes):
    print("CONTRÔLE DES VARIABLES LOCALES (avant toute connexion réseau)")
    print("%-16s %-9s %-62s %s" % ("Variable", "Présence", "Contrôle", "Résultat"))
    for variable, presence, controle, resultat in lignes:
        print("%-16s %-9s %-62s %s" % (variable, presence, controle, resultat))
    print("ENVIRONNEMENT : %s | Projet attendu : %s" % (ENVIRONNEMENT, CONFIRMATION_ATTENDUE))
    print("Limite : le préfixe phc_ ne prouve PAS que la clé appartient à « %s ». Ce script ne peut pas le vérifier ;" % CONFIRMATION_ATTENDUE)
    print("seule votre confirmation ci-dessous, faite dans le tableau de bord PostHog, le garantit.\n")


def verifier_configuration(argv, env, saisie):
    """Retourne (configuration, None) ou (None, code de sortie après affichage du refus).
    Ordre : 1) contrôle local de chaque variable et tableau, 2) confirmation humaine, 3) seulement ensuite l'envoi."""
    if "--confirm-projet-de-test" not in argv:
        return None, _erreur("ajoutez --confirm-projet-de-test pour confirmer que vous visez un projet PostHog de TEST.")
    invalide = "--cle-invalide" in argv
    lignes, corrections, valeurs = controler_variables(env, invalide)
    afficher_tableau(lignes)
    if corrections:
        return None, _erreur("contrôle des variables en échec, rien n'a été envoyé et PostHog n'a pas été contacté.\n  - "
                             + "\n  - ".join(corrections))
    print("Scénario : %s" % ("CLÉ INVALIDE (une fausse clé, la vraie n'est pas utilisée)" if invalide
                             else "5 événements fictifs avec la clé de test"))
    print("Dans votre tableau de bord PostHog, vérifiez VISUELLEMENT que :")
    print("  1. l'organisation sélectionnée est « %s » ;" % CONFIRMATION_ATTENDUE)
    print("  2. le projet sélectionné est le projet de test dédié ;")
    print("  3. la clé copiée provient de ce projet précis ;")
    print("  4. la région (%s) correspond à l'hôte %s ;" % (HOTES_AUTORISES[valeurs["hote"]], valeurs["hote"]))
    print("  5. aucune clé de production n'est utilisée.")
    try:
        reponse = saisie("Si les 5 points sont vrais, tapez exactement « %s » : " % CONFIRMATION_ATTENDUE)
    except (EOFError, KeyboardInterrupt):
        reponse = ""
    if (reponse or "").strip() != CONFIRMATION_ATTENDUE:
        return None, _erreur("confirmation manuelle absente ou incorrecte : rien n'a été envoyé.")
    return {"cle": FAUSSE_CLE if invalide else valeurs["cle"], "sel": valeurs["sel"], "hote": valeurs["hote"],
            "invalide": invalide}, None


def executer(argv, env=None, saisie=input, http=None, sleep=time.sleep) -> int:
    if "--a-blanc" in argv:
        return a_blanc()
    config, code = verifier_configuration(argv, dict(os.environ) if env is None else dict(env), saisie)
    if config is None:
        return code
    analytics = usage_analytics.UsageAnalytics(
        env={"POSTHOG_API_KEY": config["cle"], "POSTHOG_SALT": config["sel"], "POSTHOG_HOST": config["hote"]},
        executor=Immediat(), http=http, sleep=sleep)
    if analytics.host != config["hote"] or not analytics.enabled:
        return _erreur("configuration incohérente : rien n'a été envoyé.")
    print("Identifiant pseudonymisé du faux utilisateur :", analytics.distinct_id(FAUX_UTILISATEUR))
    try:
        for nom, utilisateur, props in evenements_a_envoyer():
            analytics.capture(nom, utilisateur, **props)
    finally:
        client = getattr(analytics, "_http", None)
        if http is None and client is not None:
            client.close()
    stats = analytics.snapshot()
    print("Compteurs :", stats)
    print("Mesure suspendue :", analytics.suspended)
    if config["invalide"]:
        ok = (analytics.suspended and stats["accepted"] == 0 and stats["rejected"] == 1 and stats["dropped"] == 4)
        print("RÉSULTAT clé invalide :", "OK (401/403 détecté, mesure suspendue, 4 événements abandonnés)" if ok
              else "ANOMALIE : la réponse de PostHog ne correspond pas au comportement attendu")
    else:
        ok = stats["accepted"] == 5 and stats["rejected"] == stats["failed"] == stats["dropped"] == 0
        print("RÉSULTAT :", "OK (5 événements acceptés par PostHog)" if ok else "ANOMALIE : voir les compteurs")
        if ok:
            print("« Accepté » = PostHog a répondu 2xx. Vérifiez maintenant la réception dans Live events (projet charyam-test).")
    return 0 if ok else 1


def main(argv) -> int:
    try:
        return executer(argv)
    except Exception as exc:   # jamais le message de l'erreur : il pourrait contenir une valeur sensible
        print("ERREUR inattendue (%s) : rien d'autre n'a été envoyé." % type(exc).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
