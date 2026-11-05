# Mesure d'usage anonyme (PostHog)

Facultative. Sans la variable `POSTHOG_API_KEY`, le bot n'envoie **rien** et fonctionne comme avant.

## Activer
1. Dans Render > service `charyam-bot` > Environment, ajouter `POSTHOG_API_KEY` (clé projet PostHog, commence par `phc_`).
2. Si le projet PostHog est en Europe, ajouter aussi `POSTHOG_HOST=https://eu.i.posthog.com` (défaut : États-Unis).
3. Redéployer. Désactiver = supprimer la variable.

## Événements envoyés
| Événement | Quand | Propriétés |
|---|---|---|
| `menu_used` | un bouton ou une commande du menu est utilisé | `kind` (ex. `ebooks`, `aide`, `start`) |
| `cv_questionnaire_reply` | le client répond au questionnaire CV | `done` (vrai à la dernière étape) |
| `ai_reply_sent` | une réponse de Claude est envoyée | `cv` (vrai si c'est le CV rédigé) |
| `export_requested` | bouton Word ou PDF touché | `format` |
| `payment_requested` | export refusé faute de paiement validé | `format` |

Toutes les propriétés incluent aussi `is_owner` (pour exclure le propriétaire dans PostHog).

## Ce qui n'est jamais envoyé
Texte des messages, noms, numéros de téléphone, numéros de paiement, contenu des CV, réponses de Claude, liens.
L'identifiant Telegram est remplacé par un code pseudonymisé (HMAC) ; aucune fiche « personne » n'est créée.

## Limites
- Les événements sont envoyés en arrière-plan avec un délai court ; s'ils échouent, ils sont perdus sans gêner le client.
- Le bot reste sur un seul processus : les compteurs ne sont pas partagés entre plusieurs instances.
- Cette mesure ne dit pas si le client a réellement payé : le paiement est validé à la main.
