# Test réel contrôlé de la mesure PostHog (Windows / PowerShell)

But : vérifier avec le vrai PostHog que les événements arrivent et que le bot ne dépend pas de PostHog, **sans client réel**
et **sans toucher à la production**. Tout se passe dans une organisation PostHog **séparée**, nommée `charyam-test`.

## Ce que cette procédure garantit, et ce qu'elle ne garantit pas
- Le script n'envoie que 5 événements **fictifs** (faux numéros 999000111 et 999000222), aucune donnée client.
- Le script refuse de partir sans : un hôte PostHog **explicite** (États-Unis ou Europe, rien par défaut), HTTPS, une clé au
  format `phc_…`, un sel de 16 caractères minimum, et votre **confirmation manuelle** (« charyam-test »).
- **Limite : le code ne permet pas de savoir à quelle organisation appartient une clé.** L'API d'envoi de PostHog répond seulement
  « accepté » ou « refusé », elle n'indique pas le projet. Le script ne peut donc pas empêcher qu'on colle par erreur la clé de
  production. La séparation repose sur votre confirmation et sur une vérification **après** le test : le « Default project »
  (production) doit toujours afficher **zéro événement**.
- Les identifiants sont pseudonymisés, **pas anonymes** : quiconque détient le sel peut recalculer l'identifiant d'un numéro connu.
- `payment_requested` signifie « les consignes de paiement ont été affichées », pas « un paiement a été reçu ».
  `export_requested` ne prouve pas qu'un fichier a été livré.

## 1. Préparer PostHog (à la main, une seule fois)
1. Créez l'organisation `charyam-test` (nom de l'organisation en haut à gauche, puis *Create organization*), avec son projet.
2. **Identifiez la région du projet.** PostHog a deux clouds séparés ; le bon hôte est :
   - `https://us.i.posthog.com` si votre PostHog s'ouvre sur une adresse en `us.posthog.com` (cloud États-Unis) ;
   - `https://eu.i.posthog.com` si elle s'ouvre sur `eu.posthog.com` (cloud Europe).
   En cas de doute, l'extrait de code affiché dans les réglages du projet contient l'adresse d'envoi (`api_host`). Une clé valide
   envoyée à la mauvaise région sera refusée : ce n'est pas un défaut du bot.
3. Copiez la **clé de projet** (*Project API key*, commence par `phc_`) **depuis les réglages de `charyam-test`**, jamais depuis un autre projet.
4. **Activez la suppression de l'adresse IP dans le projet de test** (réglage à faire vous-même, avant le test) :
   *Settings* > *Project* > *General* > **IP data capture configuration** > activer **Discard client IP data**.

### Ce que PostHog dit sur l'adresse IP (documentation officielle, à vérifier dans Live events)
- Par défaut, PostHog enregistre l'adresse IP de l'expéditeur (`$ip`) et en déduit une localisation (propriétés `$geoip_…`, ajoutées
  par la transformation **GeoIP**). Pendant ce test, c'est **votre** adresse IP et votre localisation qui seraient enregistrées dans le projet de test.
  En production, ce serait l'adresse du serveur Render, pas celle des clients.
- **Discard client IP data** empêche de stocker l'adresse IP avec les événements. La documentation parle d'adresses IP « client » et
  ne dit pas explicitement si cela couvre un envoi par l'API, comme ici : **vérifiez** dans Live events que `$ip` n'apparaît plus.
- Ce réglage **ne supprime pas la localisation** : la transformation GeoIP peut encore utiliser l'adresse avant qu'elle soit écartée et
  ajouter `$geoip_country_name` et d'autres champs. Pour l'empêcher, il faut désactiver la transformation GeoIP du projet (menu non confirmé ici : cherchez *Transformations* dans les réglages).
- Une propriété ajoutée à l'événement **ne suffit pas** à empêcher la collecte de l'adresse. `$geoip_disable = true` évite seulement
  la localisation pour cet événement ; le bot ne l'envoie pas aujourd'hui (amélioration possible, non faite).
- La durée de conservation dépend de la formule PostHog ; les événements plus anciens que la durée choisie sont supprimés lors du nettoyage mensuel suivant.

## 2. Préparer l'ordinateur (Windows PowerShell, dossier du Bureau)
Ouvrez **Windows PowerShell** (menu Démarrer). Python 3.10 ou plus récent est recommandé (testé en 3.13).
```powershell
python --version
cd $HOME\Desktop
Invoke-WebRequest -Uri "https://github.com/88karimbamba-eng/charyam-bot/archive/refs/heads/claude/posthog-mesure-usage.zip" -OutFile charyam-test.zip
Expand-Archive charyam-test.zip -DestinationPath charyam-test
cd (Get-ChildItem .\charyam-test -Directory | Select-Object -First 1).FullName
python -m venv .venv-test
.\.venv-test\Scripts\python.exe -m pip install httpx==0.25.2
```
Contrôle : `Test-Path scripts\posthog_test_reel.py` doit afficher `True`.

## 3. Voir ce qui sera envoyé, sans rien envoyer
```powershell
.\.venv-test\Scripts\python.exe scripts\posthog_test_reel.py --a-blanc
```
Affiche les 5 charges utiles exactes (clé remplacée par `***`). Ce mode n'utilise **pas le réseau** et **ne lit pas vos variables**.

## 4. Saisie sécurisée de la clé et du sel
```powershell
Set-PSReadLineOption -HistorySaveStyle SaveNothing
$s = Read-Host "Collez la cle PostHog de TEST (phc_...)" -AsSecureString
$env:POSTHOG_API_KEY = [System.Net.NetworkCredential]::new('', $s).Password
$b = New-Object byte[] 24; [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
$env:POSTHOG_SALT = [Convert]::ToBase64String($b)
$env:POSTHOG_HOST = "https://eu.i.posthog.com"      # ou https://us.i.posthog.com : voir étape 1
$env:PYTHONUTF8 = "1"
Set-Clipboard -Value " "                              # efface le presse-papiers (la clé y a été copiée)
$env:POSTHOG_API_KEY.StartsWith("phc_")               # doit afficher True
$env:POSTHOG_SALT.Length                              # doit afficher 32
```
Le sel est généré au hasard et n'est jamais affiché ni enregistré : il est différent de tout sel de production.

### Limites honnêtes de cette protection
- La clé et le sel ne sont ni affichés, ni écrits dans un fichier du dépôt, ni dans l'historique de cette fenêtre (réglage `SaveNothing` valable pour cette session ;
  l'historique des **anciennes** sessions n'est pas modifié).
- Ils existent pourtant **en clair dans la mémoire de cette fenêtre PowerShell** (variables d'environnement) jusqu'au nettoyage, et tout programme lancé depuis cette fenêtre les reçoit.
- Cela ne protège pas contre un logiciel malveillant sur l'ordinateur, un enregistrement d'écran, un partage d'écran ou une transcription (`Start-Transcript`). Évitez `dir env:`, qui affiche les valeurs.
- Le script n'affiche que le **type** d'une erreur inattendue, jamais son texte. Si une clé a été collée ailleurs (conversation, courriel), considérez-la compromise et supprimez l'organisation de test.

## 5. Test réel (uniquement après autorisation du propriétaire du projet)
```powershell
.\.venv-test\Scripts\python.exe scripts\posthog_test_reel.py --confirm-projet-de-test
```
Le script commence par un **tableau de contrôle des variables locales**, affiché **avant toute connexion réseau** :
```
CONTRÔLE DES VARIABLES LOCALES (avant toute connexion réseau)
Variable         Présence  Contrôle                                                       Résultat
POSTHOG_API_KEY  présente  format phc_ : respecté (la valeur n'est pas affichée)          PASS
POSTHOG_SALT     présent   longueur 32 (minimum 16 : atteint)                             PASS
POSTHOG_HOST     présent   https://eu.i.posthog.com : autorisé (Europe)                   PASS
```
Pour chaque variable : présente, vide ou absente ; format valide ou non ; **longueur** (jamais la valeur) pour le sel ; hôte autorisé ou refusé
(la valeur d'un hôte refusé n'est pas affichée). Si une ligne est en `FAIL`, le script indique la correction attendue et **s'arrête** :
aucune confirmation n'est demandée, PostHog n'est pas contacté. Le préfixe `phc_` ne prouve **pas** que la clé appartient à `charyam-test` :
le script ne peut pas le vérifier.

Si tout est `PASS`, il affiche `ENVIRONNEMENT : TEST` et l'hôte, rappelle les 5 points à constater **visuellement** dans PostHog
(organisation `charyam-test`, projet de test dédié, clé copiée de ce projet, région = hôte, aucune clé de production), puis demande de **taper `charyam-test`**. Il envoie alors **5 événements fictifs** :
`menu_used`, `cv_questionnaire_reply`, `export_requested`, `payment_requested`, `ai_reply_sent`, chacun avec ses propriétés
et `is_owner`, comme le bot. Attendu : `RÉSULTAT : OK (5 événements acceptés par PostHog)`. « Accepté » veut dire que PostHog a répondu 2xx : la **réception** se vérifie à l'étape 7.

## 6. Scénario de clé invalide
```powershell
.\.venv-test\Scripts\python.exe scripts\posthog_test_reel.py --confirm-projet-de-test --cle-invalide
```
Envoie **une seule requête** avec une fausse clé (votre vraie clé n'est pas utilisée). Attendu : `RÉSULTAT clé invalide : OK`, `rejected` = 1,
`dropped` = 4, mesure suspendue. PostHog devrait répondre 401 ou 403 à une clé inconnue ; si ce n'est pas le cas, le script affiche `ANOMALIE`.

## 7. Vérifier la réception dans PostHog
1. Choisissez l'organisation `charyam-test` (en haut à gauche), puis *Activity* (ou *Events*) et l'onglet *Live* s'il existe. Les noms de menus peuvent varier.
2. Retrouvez les 5 événements. Cliquez sur chacun et contrôlez : nom, heure, `distinct_id` (`u_` + 16 caractères, le même que celui affiché par le script),
   propriétés limitées à `kind`, `format`, `done`, `cv`, `is_owner`, `$lib`, `$process_person_profile`, plus des champs ajoutés par PostHog (`$ip`, `$geoip_…`).
3. Aucun nom, téléphone, texte de message, contenu de CV ou secret ne doit apparaître. Aucune fiche « personne » ne doit être créée.
4. **Contre-vérification de la séparation :** le projet « Default project » (production) doit toujours indiquer zéro événement.

## 8. Nettoyage (même si le test a échoué)
```powershell
Remove-Item Env:POSTHOG_API_KEY, Env:POSTHOG_SALT, Env:POSTHOG_HOST, Env:PYTHONUTF8 -ErrorAction SilentlyContinue
Remove-Variable s, b -ErrorAction SilentlyContinue
Set-Clipboard -Value " "
```
Fermer la fenêtre efface aussi les variables. Supprimez ensuite le dossier `charyam-test` du Bureau et, si vous n'en avez plus besoin, l'organisation `charyam-test` dans PostHog (cela invalide la clé).

## 9. Désactiver la mesure
Sans `POSTHOG_API_KEY`, plus aucune requête n'est faite et le bot se comporte comme avant. Sur Render, après une activation : *Environment* > supprimer la clé > redéployer (un redémarrage lève aussi une suspension 401/403).

## 10. Avant d'activer en production (après autorisation écrite du propriétaire)
1. PR relue et fusionnée par le propriétaire (pas avant).
2. Clé du projet **de production** et un **nouveau** sel aléatoire (32 caractères ou plus), conservé dans un gestionnaire de secrets : le perdre ou le changer coupe la continuité des identifiants.
3. `POSTHOG_HOST` explicite. Dans le bot, l'absence de `POSTHOG_HOST` retombe sur la région États-Unis ; à renseigner si le projet est européen.
4. Activer **Discard client IP data** dans le projet de production et décider du sort de la transformation GeoIP.
5. Informer les clients (mention de confidentialité) : mesure **pseudonymisée**, pas anonyme.
6. Ajouter les deux variables sur Render, redéployer, puis contrôler les journaux : aucune ligne « PostHog : événement … » en avertissement.
