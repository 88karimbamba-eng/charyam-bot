# CHARYAM + agent managé (test « Emploi »)

Ce que ce lot ajoute : l'activité « 💼 Emploi & Candidatures » peut passer par une session
Claude Managed Agents (bêta) au lieu de l'API Messages. Tout est désactivé par défaut : tant que
`USE_MANAGED_AGENTS` n'est pas à `1`, le bot se comporte exactement comme avant.

## Ce qui change dans le code
- `agent_backend.py` (nouveau) : une session d'agent par chat et par activité, lecture des événements.
- `charyam_telegram.py` : un import et 17 lignes de routage dans `get_claude_response`. Si l'agent échoue
  (erreur, délai, réponse vide, session bloquée), le bot retombe sur l'API Messages.
- `managed_agents/` : définition de l'agent, de l'environnement, et un script de création.
- Tests : `python -m unittest test_charyam_telegram test_agent_backend` (21 tests, sans réseau, passent).

## Ce que le propriétaire doit faire (je n'ai pas ta clé API et je ne dépense pas ton argent)
1. Créer l'agent et l'environnement, au choix :
   - avec la CLI : `ant apply managed_agents/charyam-emploi.md managed_agents/environment.yaml`
     (les identifiants sont écrits dans `claude-lock.json`) ;
   - sans la CLI : `python managed_agents/setup_agent.py` (jamais testé sur l'API réelle).
2. Dans Render > Environment, ajouter `CHARYAM_AGENT_ID`, `CHARYAM_ENVIRONMENT_ID`,
   `MANAGED_AGENT_MODES=emploi`, puis `USE_MANAGED_AGENTS=1`.
3. Pousser les fichiers sur GitHub (Render redéploie), puis tester avec le bouton Emploi.
4. Dans la Console Anthropic, fixer une limite de dépense avant de tester, et regarder le coût
   réel après quelques jours.

## Retour arrière
Mettre `USE_MANAGED_AGENTS=0` dans Render (ou supprimer la variable) : retour immédiat à l'ancien mode.

## Limites connues (honnêtes)
- Jamais exécuté contre l'API réelle : seules les formes d'appel de la documentation et les
  signatures du SDK ont été vérifiées. Le premier vrai test peut révéler un écart.
- Le lien chat -> session est en RAM : après un redémarrage de Render, une nouvelle session démarre.
- Aucun fichier créé par l'agent n'est renvoyé sur Telegram.
- Les outils qui demanderaient une confirmation bloquent la session ; le bot la remplace alors et
  retombe sur l'API Messages.
- La recherche web de l'agent ne fonctionne que sur les hôtes de `environment.yaml` (liste de
  départ courte : à compléter).
- Les paiements, Chariow et le trading ne sont volontairement PAS connectés à l'agent.
