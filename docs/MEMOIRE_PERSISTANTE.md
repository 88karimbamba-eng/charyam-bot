# Mémoire persistante et quotas persistants (Upstash)

Statut : code prêt et testé avec des services SIMULÉS. **Jamais exécuté contre un vrai Upstash.**
Sans les deux variables ci-dessous, le bot se comporte exactement comme avant (mémoire en RAM).

## Ce que ça change
- **Conversation** : sauvegardée dans Redis (clé `charyam:hist:<chat>`, expiration 7 jours renouvelée à
  chaque message). Après un redémarrage de Render, la conversation reprend. Le cache local reste lu en
  premier : une seule lecture Redis par chat après un redémarrage, puis une écriture par message.
- **Quotas des testeurs** : compteurs Redis (`charyam:q:min|day:<utilisateur>:<fenêtre>`), sans aucun
  contenu. Fenêtres FIXES d'une minute et d'un jour (UTC) : différent de la fenêtre glissante de la
  version en mémoire (un testeur peut envoyer 6 messages à la fin d'une minute et 6 au début de la suivante).
- **Commande `/oubli`** : efface la conversation (cache local, copie Redis, dernière réponse exportable).
  Si la copie Redis ne peut pas être effacée, le bot le dit au lieu d'affirmer que c'est fait.
- Le jeton Upstash est masqué dans tous les journaux.

## Comportement en cas de panne d'Upstash
- Conversation : le bot continue avec la copie locale (journal : « Mémoire persistante non sauvegardée »).
- Quotas : les messages des **testeurs sont refusés** avec un message clair (jamais de limite désactivée
  en silence). Le propriétaire n'est jamais limité ni bloqué.

## Activation (par Karim, dans Render > charyam-bot > Environment)
1. Créer une base Redis Upstash (plan gratuit, aucune carte bancaire demandée sur la page de tarifs lue
   le 16 oct. ; à revérifier). Choisir une région proche (Europe).
2. Copier l'URL REST et le jeton REST de la base. Les saisir UNIQUEMENT dans Render :
   `UPSTASH_REDIS_REST_URL` et `UPSTASH_REDIS_REST_TOKEN`. Ne jamais les coller dans un chat ou Notion.
   Préférer, si Upstash le propose, un jeton limité aux commandes nécessaires.
3. Déployer, puis lire le journal de démarrage (aucune ligne d'erreur « Stockage Upstash mal configuré »).

## Test réel à faire après activation (rien de tout cela n'est prouvé aujourd'hui)
1. Envoyer un message au bot, puis dans Render > Manual Deploy (redémarrage), puis « Qu'est-ce que je viens
   de te dire ? » : le bot doit s'en souvenir.
2. Envoyer `/oubli`, puis la même question : le bot ne doit rien savoir.
3. Dans Upstash > Data Browser : une clé `charyam:hist:<chat>` apparaît après un message, disparaît après `/oubli`.
4. Avec un compte testeur autorisé : dépasser la limite par minute, vérifier le refus puis la reprise.

## Points non vérifiés
- Le format exact des requêtes REST d'Upstash (une commande en tableau JSON dans un POST, en-tête
  `Authorization: Bearer`) est écrit d'après la documentation connue, pas testé contre le service.
- Le sort d'une base gratuite inactive (archivage, suppression) n'est pas précisé sur la page de tarifs lue.
- Coût en commandes : environ 1 écriture par message du propriétaire, 3 à 4 commandes par message d'un
  testeur. Le plan gratuit annonce 500 000 commandes par mois : marge confortable, mais à surveiller.

## Données personnelles
Le contenu des conversations (CV, offres, informations personnelles) est stocké chez Upstash pendant 7 jours
au plus. À mentionner aux futurs clients avant toute ouverture publique. Le contenu n'est jamais écrit dans
les journaux Render.
