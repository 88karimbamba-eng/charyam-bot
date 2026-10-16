"""Intégrations externes de CHARYAM (recherche web, extraction, navigateur, applications).

Chaque module est isolé, importe son SDK seulement au moment de l'utilisation
(le démarrage du bot et sa consommation mémoire restent inchangés si une clé est absente)
et ne lève jamais d'exception vers l'appelant : il retourne un ToolResult.
"""
