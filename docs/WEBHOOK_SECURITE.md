# Sécurité du webhook Telegram

## Règle
`POST /webhook` n'est accepté que si l'en-tête `X-Telegram-Bot-Api-Secret-Token` est identique à la variable
`TELEGRAM_WEBHOOK_SECRET`. Ce contrôle est exécuté avant toute lecture du message : un refus ne déclenche aucun
appel à Claude, aucun envoi Telegram, aucune écriture en mémoire.

| Environnement | Secret | Comportement de `/webhook` |
|---|---|---|
| Production (Render : `RENDER=true`, ou `CHARYAM_ENV=production`) | défini | 403 si absent/faux, traitement si valide |
| Production | **absent ou vide** | **503 pour toute requête** (service fermé), journal CRITICAL au démarrage. `/health` reste 200 pour que le déploiement aboutisse |
| Hors production (poste de développement, tests) | absent | contrôle désactivé, avertissement au démarrage |
| Hors production | défini | identique à la production |

Pourquoi un 503 plutôt qu'un refus de démarrer : un déploiement qui échoue laisse l'ancienne version, éventuellement
non protégée, en ligne. Une version qui démarre mais ferme `/webhook` remplace immédiatement l'ancienne.

## Configuration locale / tests
Aucune variable requise : sans `RENDER` ni `CHARYAM_ENV`, le contrôle est désactivé et la suite de tests
s'exécute telle quelle. Pour reproduire la production en local : `CHARYAM_ENV=production TELEGRAM_WEBHOOK_SECRET=<valeur de test>`.

## Rotation du secret (procédure, aucune valeur ici)
1. Générer une valeur aléatoire d'au moins 32 caractères (lettres, chiffres, `_` et `-` uniquement), sur votre
   machine (`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`). Ne la collez ni dans un chat, ni dans
   GitHub, ni dans Notion, ni dans une commande partagée.
2. Render > service `charyam-bot` > Environment > `TELEGRAM_WEBHOOK_SECRET` > nouvelle valeur > Save. Render redéploie.
3. Au démarrage, le service réenregistre le webhook auprès de Telegram avec la nouvelle valeur. Journal attendu :
   « Secret du webhook : présent et non vide » puis « Webhook configuré … (secret enregistré chez Telegram : oui) ».
4. Vérifier (voir ci-dessous). Ne jamais réutiliser une valeur exposée.
5. Ordre à respecter avec le correctif fail-closed : définir le secret AVANT de déployer ce correctif.

## Vérification en 4 niveaux
1. **Code** : `python3 -m pytest tests/test_webhook_fail_closed.py` (matrice absent/vide/incorrect/valide).
2. **Application** : `python3 -m pytest tests/test_webhook_secret.py tests/test_webhook_fail_closed.py` (réponses et absence de traitement).
3. **Déploiement réel** : `python3 scripts/verify_webhook.py https://charyam-bot.onrender.com/webhook`
   (sans secret 403, mauvais secret 403 ; ajouter `WEBHOOK_SECRET_TO_TEST` dans l'environnement du terminal pour tester aussi le secret valide → 200).
   Le corps envoyé est `{"update_id": 0}` : sans effet, sans coût, même si le contrôle était inactif.
4. **Fonctionnement réel** : envoyer un message au bot depuis un compte autorisé ; dans les journaux Render doivent
   apparaître « Message reçu de <identifiant> » puis « Réponse Claude : … ». Un compte non autorisé doit recevoir son identifiant et aucun appel à Claude.

## Limites connues
- Si le secret est défini mais que l'enregistrement chez Telegram échoue au démarrage, les vrais messages sont refusés (403) jusqu'au prochain démarrage réussi.
- Les compteurs de quotas sont en mémoire : remis à zéro à chaque redémarrage. Ils ne remplacent pas le plafond de dépense Anthropic.
