# Test réel contrôlé de la mesure PostHog

But : vérifier avec le vrai PostHog que les événements arrivent et que le bot ne dépend pas de PostHog, **sans client réel**
et **sans toucher à la production**.

## Précautions
- Créer dans PostHog un projet **séparé et jetable** (« charyam-test »). Ne pas utiliser le projet de production.
- Utiliser un `POSTHOG_SALT` **de test** (16 caractères minimum), différent de celui de production. Ne jamais le coller dans le dépôt, un message ou un rapport.
- Saisir les variables seulement dans le terminal de la session, jamais dans un fichier suivi par git (`.env` est ignoré par git).
- Ne rien ajouter dans Render à cette étape.

## 1. Envoi de 5 événements fictifs
```
POSTHOG_API_KEY=<clé du projet de test> POSTHOG_SALT=<sel de test> \
  python scripts/posthog_test_reel.py --confirm-projet-de-test
```
Attendu : « RÉSULTAT : OK (5 événements acceptés par PostHog) ». Le script utilise des numéros inventés et n'affiche ni la clé ni le sel.

## 2. Vérifier la réception
PostHog > projet de test > *Activity* / *Live events* : 5 événements `menu_used`, `cv_questionnaire_reply`, `export_requested`,
`payment_requested`, `ai_reply_sent`, avec le `distinct_id` affiché par le script (`u_` + 16 caractères).
Contrôler qu'aucune propriété ne contient autre chose que `kind`, `format`, `done`, `cv`, `is_owner`, `$lib`, `$process_person_profile`,
et qu'aucune fiche « personne » n'a été créée. Compter ~1 minute de délai d'affichage.

## 3. Vérifier la réaction à une clé refusée
```
POSTHOG_API_KEY=<n'importe quoi> POSTHOG_SALT=<sel de test> \
  python scripts/posthog_test_reel.py --confirm-projet-de-test --cle-invalide
```
Attendu : « RÉSULTAT clé invalide : OK », `rejected` = 1, mesure suspendue, les autres événements abandonnés sans envoi.

## 4. Désactiver immédiatement
Supprimer (ou vider) `POSTHOG_API_KEY` : sans clé, plus aucune requête n'est faite et le bot se comporte comme avant.
Sur Render, après activation : *Environment* > supprimer `POSTHOG_API_KEY` > redéployer (le redémarrage lève aussi une suspension 401/403).
Supprimer en plus le projet de test dans PostHog si on ne s'en sert plus.

## 5. Avant d'activer en production (Render), après autorisation écrite du propriétaire
1. PR relue et fusionnée par le propriétaire (pas avant).
2. Clé du projet **de production** et un **nouveau** `POSTHOG_SALT` aléatoire (≥ 32 caractères), sauvegardé dans un gestionnaire de secrets : le perdre ou le changer coupe la continuité des identifiants.
3. Région : `POSTHOG_HOST` = `https://eu.i.posthog.com` si le projet est en Europe (défaut : États-Unis).
4. Informer les clients (mention de confidentialité) : mesure **pseudonymisée**, pas anonyme.
5. Ajouter les deux variables sur Render, redéployer, puis contrôler les journaux : aucune ligne « PostHog : événement … » en avertissement.
6. Faire quelques actions avec le compte propriétaire (`is_owner` vrai) et vérifier leur arrivée, puis filtrer `is_owner = true` dans PostHog.
