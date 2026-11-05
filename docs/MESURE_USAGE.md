# Mesure d'usage pseudonymisée (PostHog)

Facultative. Sans **les deux** variables `POSTHOG_API_KEY` et `POSTHOG_SALT`, le bot n'envoie **rien** et fonctionne comme avant.

## Pseudonymisé, pas anonyme
Les données sont **pseudonymisées**, pas totalement anonymes :
- Le numéro Telegram est remplacé par un code (`u_` + 16 caractères) calculé avec HMAC-SHA256 et le secret `POSTHOG_SALT`.
  Sans le secret, on ne peut pas retrouver le numéro à partir du code.
- Mais **quiconque détient `POSTHOG_SALT`** peut recalculer le code d'une personne dont il connaît le numéro Telegram et
  retrouver ses événements. Gardez le secret hors du dépôt et hors des conversations.
- Les événements d'une même personne restent **reliables entre eux** (même code, horodatage) : on peut reconstituer
  son parcours (menu, questionnaire, export, demande de paiement).
- PostHog reçoit aussi l'adresse IP du **serveur** du bot (Render), pas celle du client, et les données sont stockées
  dans la région choisie (États-Unis par défaut).
- Ces données peuvent relever des règles de protection des données personnelles applicables à tes clients. Je n'ai pas
  vérifié ces règles : à contrôler, et à mentionner dans l'information donnée aux clients si nécessaire.

## Activer
1. Dans Render > service `charyam-bot` > Environment, ajouter :
   - `POSTHOG_API_KEY` : clé projet PostHog (commence par `phc_`) ;
   - `POSTHOG_SALT` : valeur aléatoire de 16 caractères ou plus, **indépendante du jeton Telegram**
     (`python -c "import secrets; print(secrets.token_urlsafe(32))"`). Il n'y a volontairement **aucun repli** sur le jeton Telegram.
2. Projet PostHog en Europe : ajouter `POSTHOG_HOST=https://eu.i.posthog.com` (défaut : États-Unis).
3. Redéployer. Désactiver = supprimer l'une des deux variables.

Changer `POSTHOG_SALT` change tous les codes : les événements d'avant et d'après ne sont plus reliables.

## Événements envoyés
Liste fermée : tout autre nom est refusé par le code.

| Événement | Quand | Propriétés |
|---|---|---|
| `menu_used` | un bouton ou une commande du menu est utilisé | `kind` : valeur d'une liste fermée (`ebooks`, `aide`, `start`, `mode_…`…) ; toute commande inconnue (ex. un numéro tapé après « / ») devient `commande_autre` |
| `cv_questionnaire_reply` | le client répond au questionnaire CV | `done` (vrai à la dernière étape) |
| `ai_reply_sent` | une réponse de Claude est envoyée | `cv` (vrai si c'est le CV rédigé) |
| `export_requested` | bouton Word ou PDF touché | `format` |
| `payment_requested` | **les consignes de paiement ont été affichées** : export refusé faute de paiement validé | `format` |

Toutes les propriétés incluent aussi `is_owner` (pour exclure le propriétaire dans PostHog).

`payment_requested` signifie « demande ou intention de paiement » : le client a voulu un fichier et a reçu les consignes.
Il ne dit **pas** qu'il a payé.

## Événements volontairement absents
- `payment_confirmed` (paiement effectivement vérifié) et `delivery_completed` (fichier effectivement livré) **ne sont pas mesurés**.
- Raison : le code n'a pas, aujourd'hui, de preuve assez fiable. La validation d'un paiement est un geste manuel du
  propriétaire (`/valider`), sans montant ni identifiant de commande, et elle est perdue à chaque redémarrage. L'envoi du
  fichier est confirmé par Telegram (`ok`), ce qui ne prouve pas que le client l'a reçu ni ouvert.
- Ne les ajouter qu'après décision explicite sur ce qui compte comme preuve, puis les inscrire dans `EVENT_NAMES`
  (`usage_analytics.py`) avec leurs tests.

## Ce qui n'est jamais envoyé
Numéro Telegram ou identifiant de chat en clair, texte des messages, noms, numéros de téléphone ou de paiement, contenu des
CV, réponses de Claude, liens. Le code bloque aussi tout événement dont le nom ou les propriétés contiendraient l'identifiant
en clair, et ignore toute propriété hors liste blanche.

## Fiabilité des envois
- L'envoi se fait en arrière-plan avec un délai court : il ne retarde jamais la réponse au client.
- PostHog répond 2xx : événement **accepté**. 429, 5xx, 408 et erreurs réseau : jusqu'à 3 tentatives au total
  (attentes de 0,5 s puis 1,5 s, ou `Retry-After` plafonné à 5 s ; l'en-tête peut être un nombre de secondes ou une date HTTP ;
  valeur illisible, infinie, négative ou déjà dépassée : délai de base). Attente totale maximale par événement : 10 s, dans un fil
  d'arrière-plan. 400, 401, 403 et autres erreurs : **définitif**, pas de nouvelle tentative.
- Une clé refusée (401/403) **suspend** la mesure jusqu'au redémarrage du service : plus aucun nouvel événement n'est mis en file,
  et les événements déjà en attente sont abandonnés (compteur `dropped`) sans être envoyés.
- Les échecs sont journalisés (nom d'événement, issue, code HTTP) sans clé, sans secret, sans identifiant ni contenu ;
  un même échec n'est journalisé en avertissement qu'une fois toutes les 5 minutes.
- `capture()` renvoie « vrai » quand l'événement est **mis en file**, pas quand PostHog l'a accepté. Le résultat réel est dans
  `usage.snapshot()` : `accepted`, `rejected`, `failed`, `retried`, `dropped`, `queued`.

## Limites
- Les compteurs sont en mémoire et partent à chaque redémarrage ; ils ne sont pas encore affichés dans le bot.
- Un événement en cours d'envoi est perdu si le service s'arrête à cet instant, et la file est bornée (50) : en cas de panne
  longue de PostHog, des événements sont abandonnés plutôt que gardés.
- Le bot reste sur un seul processus ; plusieurs instances auraient chacune leurs compteurs.
- Non vérifié contre le vrai PostHog depuis cet environnement : les tests simulent ses réponses. À contrôler après activation.

## Test réel contrôlé
Voir `docs/TEST_REEL_POSTHOG.md` (projet PostHog de test, identifiants fictifs, aucun client réel).
