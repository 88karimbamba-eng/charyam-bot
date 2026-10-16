# Intégrations CHARYAM : Perplexity, Firecrawl, Playwright, Composio

Document d'installation et d'exploitation. Dernière vérification des documentations officielles : 16 octobre 2026.

## 1. Principe

```
Telegram → webhook CHARYAM → Claude décide s'il faut un outil → exécution contrôlée → résultat renvoyé à Claude comme DONNÉE → réponse sur Telegram
```

- Claude choisit l'outil (tool use natif de l'API Anthropic). Les quatre ne sont **jamais** déclenchés systématiquement.
- Un outil n'est proposé à Claude que si sa configuration est **complète**. Sans aucune clé, le bot se comporte exactement comme avant.
- Tout contenu venu du web ou d'une application est encadré par `<donnees_externes_non_fiables>` : c'est une donnée, jamais une instruction.
- `/outils` dans Telegram affiche l'état de chaque intégration (activé / non configuré), sans aucun secret.

## 2. Ce qui est réellement connecté, et où

**Ces intégrations s'exécutent dans le serveur CHARYAM (Render), par API ou SDK.** Installer une bibliothèque Python ne connecte rien à Claude Desktop, à Claude Code ni à claude.ai. Ce sont des configurations distinctes, dans les réglages de chaque application.

| Service | Mécanisme utilisé par CHARYAM | Fichier | Variables | Autre mécanisme officiel (hors CHARYAM) |
|---|---|---|---|---|
| Perplexity | API REST **Search** (`POST api.perplexity.ai/search`) | `integrations/perplexity_client.py` | `PERPLEXITY_API_KEY` | Serveur MCP officiel : distant `https://api.perplexity.ai/mcp` ou local `@perplexity-ai/mcp-server` |
| Firecrawl | SDK officiel `firecrawl-py` | `integrations/firecrawl_client.py` | `FIRECRAWL_API_KEY` | Serveur MCP hébergé `https://mcp.firecrawl.dev/v2/mcp-oauth` (connexion OAuth) ou `/v2/mcp` (clé en jeton Bearer) |
| Playwright | Bibliothèque `playwright` (navigateur **distant** par CDP, ou Chromium local) | `integrations/playwright_client.py` | `PLAYWRIGHT_CDP_URL` ou `PLAYWRIGHT_LOCAL=1`, **et** `PLAYWRIGHT_ALLOWED_DOMAINS` | `@playwright/mcp` (navigateur local, pour un client MCP) |
| Composio | SDK officiel `composio`, exécution **lecture seule** | `integrations/composio_client.py` | `COMPOSIO_API_KEY`, `COMPOSIO_USER_ID`, `COMPOSIO_READONLY_TOOLS`, `COMPOSIO_TOOL_VERSION` | Serveurs MCP Composio par toolkit |

Choix notables :

- **Perplexity** : l'endpoint « Sonar Chat Completions » est déprécié par Perplexity (fin de support annoncée le 27 septembre 2026) ; il n'est pas utilisé. L'API Search renvoie titre, URL, extrait et date ; Claude synthétise à partir de ces sources et ne doit citer que ces URL.
- **Playwright** : voir la section 4, c'est le point qui demande une décision de votre part.
- **Composio** : une clé API seule n'autorise **aucune** application. Chaque application doit être connectée par vous via un lien OAuth (section 5).

Brancher aussi ces services dans Claude Desktop ou Claude Code est possible via leurs serveurs MCP, mais c'est indépendant de CHARYAM et non nécessaire à son fonctionnement. Je n'ai pas vérifié pas-à-pas les procédures d'installation côté Claude Desktop pour Firecrawl et Composio (leurs pages ne les détaillent pas) ; référez-vous à leur documentation.

## 3. Obtenir et saisir les clés (Render)

Ne collez jamais une clé dans une conversation, un commit ou un fichier du dépôt.

1. Créez la clé dans le tableau de bord officiel du service : Perplexity (console API, section Keys), Firecrawl, Composio.
2. Render → votre service `charyam-bot` → **Environment** → *Add Environment Variable* → nom et valeur.
3. Enregistrez : Render redéploie. Vérifiez dans Telegram avec `/outils`.

Ordre conseillé : **Perplexity d'abord**, c'est le parcours le plus simple (aucune dépendance supplémentaire à l'exécution), puis Firecrawl.

## 4. Playwright : pourquoi il est désactivé par défaut

Mesure réelle faite pendant l'intégration : un Chromium headless occupe **environ 400 à 500 Mo** dès une page simple (somme des RSS, qui surestime légèrement la mémoire partagée). Votre service Render est sur l'offre **gratuite : 512 Mo de RAM**, déjà utilisés en partie par Gunicorn et Python (≈130 Mo). Un Chromium local y serait tué par manque de mémoire. De plus, un échange du forum Render indique que le runtime Python natif n'offre pas certains outils système (par exemple `xauth`) et suggère une image Docker pour installer navigateurs et dépendances ; je n'ai pas trouvé de procédure vérifiée pour installer Chromium sur ce runtime natif. La page tarifaire consultée indique aussi 512 Mo pour le premier plan payant : passer à un plan payant d'entrée ne règle donc pas la question (vérifiez les offres sur render.com/pricing).

Deux options supportées par le code :

- **Navigateur distant (recommandé)** : un service de navigateur dédié expose une adresse CDP (`wss://…`). Renseignez `PLAYWRIGHT_CDP_URL` ; CHARYAM s'y connecte sans lancer de Chromium. **Coût et conditions : à vérifier chez le fournisseur choisi, je n'en ai pas sélectionné.**
- **Chromium local** : `PLAYWRIGHT_LOCAL=1`, uniquement sur une machine ou un plan avec assez de mémoire (par exemple un service Docker plus gros).

Dans les deux cas, `PLAYWRIGHT_ALLOWED_DOMAINS` est **obligatoire** (liste blanche). Garde-fous : navigation limitée à ces domaines (redirections comprises), toute requête vers une adresse privée ou les métadonnées cloud est bloquée, images/médias/polices bloqués, un seul navigateur à la fois, lecture seule (aucun clic ni saisie), fermeture systématique. Limite connue : le navigateur résout le DNS lui-même ; un changement d'adresse entre notre contrôle et sa requête (DNS rebinding) n'est pas couvert à 100 %.

## 5. Composio : première intégration non destructive

1. Dans le tableau de bord Composio, créez une *auth config* pour **une** application de votre choix et relevez son identifiant.
2. Sur **votre ordinateur** (pas dans un chat) :
   ```bash
   export COMPOSIO_API_KEY=...      # saisi dans votre terminal
   export COMPOSIO_USER_ID=...      # identifiant stable, le même que sur Render
   python scripts/composio_connect.py <auth_config_id>
   ```
   Ouvrez le lien affiché et autorisez l'application. *Ce script suit la signature du SDK (`connected_accounts.link` / `wait_for_connection`) mais n'a pas pu être testé contre le service réel faute de clé : s'il échoue, la documentation Composio fait foi.*
3. Sur Render, définissez `COMPOSIO_USER_ID`, `COMPOSIO_READONLY_TOOLS` (identifiants d'outils **en lecture seule**, séparés par des virgules) et `COMPOSIO_TOOL_VERSION` (version précise : le SDK refuse « latest »).

**Aucune action modifiant des données n'est possible dans cette version.** Tout outil hors de `COMPOSIO_READONLY_TOOLS` est refusé avant tout appel. Écrire, envoyer ou supprimer exigerait d'abord un mécanisme de confirmation explicite par message Telegram, volontairement non construit ici.

## 6. Sécurité en place

- Aucune clé dans le code ; clés lues dans l'environnement ; masquées dans les journaux (valeurs configurées et formes `Bearer …`, `pplx-…`, `fc-…`). Les journaux d'outils ne contiennent ni arguments ni contenu, seulement nom, issue et durée.
- Garde-fou d'URL : seuls `http`/`https`, ports 80/443, pas d'identifiants dans l'URL, pas de nom interne ; **toutes** les adresses résolues doivent être publiques (bloque réseaux privés, boucle locale, lien-local dont `169.254.169.254`, IPv6 mappé).
- Contenus externes encadrés et neutralisés (une page ne peut pas refermer l'encadrement) ; consigne système explicite de ne jamais obéir à un contenu récupéré.
- Limites : 4 tours d'outils maximum par message, 3 appels par tour, délai par outil, 60 appels d'outils par heure (`TOOL_CALLS_PER_HOUR`), résultats tronqués à 12 000 caractères, 5 pages maximum par exploration.
- TLS jamais désactivé ; `ignore_robots_txt` et `skip_tls_verification` de Firecrawl jamais utilisés. Seul l'utilisateur autorisé (`USER_ID`) peut déclencher le bot, donc les outils.

## 7. Tests

```bash
pip install -r requirements.txt
python -m unittest test_charyam_telegram                      # tests d'origine (12)
python -m unittest discover -s tests -t . -v                  # intégrations
RUN_LIVE_TESTS=1 python -m unittest tests.test_live_services -v   # RÉELS, avec vos clés
```

- **Simulés** : `test_common`, `test_clients_mocked`, `test_router`, `test_webhook_routing` (aucun appel externe).
- **Réels locaux** : `test_real_playwright` (vrai Chromium, vrai serveur local ; ignoré si Chromium est absent).
- **Réels distants** : `test_live_services`. Ignorés tant que `RUN_LIVE_TESTS=1` et les clés ne sont pas fournies : **un test ignoré n'est pas un test réussi**.

## 8. Coûts et limites à vérifier

Je n'ai pas vérifié les tarifs ni les quotas gratuits actuels de Perplexity, Firecrawl, Composio ni d'un fournisseur de navigateur distant. Contrôlez-les sur leurs pages officielles avant d'activer. Render gratuit : mise en veille après 15 minutes sans trafic (premier message ≈ 1 minute de réveil), 750 heures gratuites par mois et par espace de travail, système de fichiers éphémère.

## 9. Retour arrière

- **Désactiver une intégration** : supprimer sa variable dans Render (l'outil disparaît, le bot continue). Aucun redéploiement de code nécessaire au-delà du redémarrage automatique.
- **Annuler le code** : dans Render, *Deploys* → redéployer le déploiement précédent (commit `ff676ee`), ou `git revert` du merge de la branche.
- **Clé compromise** : la révoquer dans le tableau de bord du service puis la remplacer dans Render.

## 10. Diagnostic du webhook Telegram (sans manipuler le jeton)

À chaque démarrage, CHARYAM journalise une ligne `État du webhook Telegram: …` : `url_conforme` (l'URL enregistrée chez Telegram est-elle la bonne ?), `en_attente` (messages que Telegram n'a pas pu livrer), `derniere_erreur` (date et message de la dernière tentative de livraison échouée) et `mises_a_jour_autorisees`. Ni le jeton ni le secret n'y figurent.

Chaque message reçu mais non traité laisse aussi une trace avec sa **raison** (« pas de message », « sans texte exploitable », « commande non gérée »…), sans jamais écrire le contenu. Pour diagnostiquer un bot muet : Render → Logs → chercher `webhook` puis `ignoré`.

## 11. Lecture intégrée de liens (sans clé)

`lire_page_web` est désormais **toujours disponible** : `integrations/web_reader.py` lit une page publique avec une simple requête HTTP (bibliothèque standard + httpx, aucune dépendance ajoutée). Si elle échoue et que `FIRECRAWL_API_KEY` est configurée, Firecrawl prend le relais (sauf si l'URL a été refusée par le garde-fou).

- **Marche** : articles, offres d'emploi, fiches produit, sites vitrines classiques (vérifié sur de vraies pages : github.com, example.com).
- **Ne marche pas** : pages qui exigent une connexion (LinkedIn, Facebook, Instagram, TikTok…) ou chargées par JavaScript. Le bot le dit et demande de coller le texte ; il n'invente rien. Firecrawl ou Playwright peuvent aider pour le JavaScript, pas pour les connexions.
- **Garde-fous** : chaque adresse et chaque redirection passent par `check_public_url` (5 redirections max), 1,5 Mo lus au maximum, texte/HTML uniquement, contenu encadré comme donnée non fiable. `robots.txt` n'est pas consulté (lecture ponctuelle demandée par l'utilisateur). Limite connue : DNS rebinding non couvert à 100 %.
- **Effet de bord à connaître** : comme un outil est maintenant toujours exposé, chaque message envoie à Claude la définition de l'outil et les règles d'usage (quelques centaines de jetons de plus par message). `/outils` affiche « Lecture de pages (intégrée) : activé ».

### 11 bis. PDF et documents liés : NON lus par défaut

**Règle de fonctionnement (décision de Karim, 16/10/2026)** : le bot ne lit pas les PDF, pour limiter les coûts. Si le lien est un PDF, ou si l'offre complète n'est que dans un PDF lié, il le dit et demande de copier-coller le texte. Une page qui contient l'offre directement est lue normalement. Le fichier n'est même pas téléchargé, et Firecrawl n'est pas utilisé en secours pour un PDF. Pour réactiver la lecture : variable `PDF_READING=1` sur Render (à ne faire qu'en connaissance du coût ci-dessous).

Capacité conservée, désactivée par défaut : `lire_page_web` peut lire les **PDF publics** (`pypdf==6.19.0`, pur Python) : 12 Mo maximum, 40 pages maximum, texte tronqué à 30 000 caractères. Un PDF **scanné** (images sans texte) ou protégé par mot de passe est refusé avec un message clair.
- Quand une page contient des liens vers des documents (`.pdf`, `.docx`, `.xlsx`…), ils sont listés à la fin du texte pour que Claude puisse ouvrir le PDF. Seuls les PDF sont lisibles ; les autres formats sont seulement signalés.
- Mesuré sur un vrai PDF de 8,9 Mo / 18 pages : 2,7 s, environ +31 Mo de mémoire (Render gratuit : 512 Mo).
- **Coût** : un PDF de 30 000 caractères représente de l'ordre de 8 000 à 10 000 jetons d'entrée (estimation, pas une mesure) pour ce message et les suivants de la même conversation, tant qu'ils restent dans l'historique.
