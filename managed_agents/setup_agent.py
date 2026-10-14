#!/usr/bin/env python3
"""
Crée l'agent « CHARYAM Emploi » et son environnement via le SDK Python, puis affiche les deux
identifiants à copier dans Render. Alternative à `ant apply` quand la CLI n'est pas installable.

À lancer UNE SEULE FOIS, par le propriétaire, avec sa clé API :
    export ANTHROPIC_API_KEY="..."
    python managed_agents/setup_agent.py

Attention : ce script crée de vraies ressources dans le compte Console du propriétaire.
Les signatures `agents.create` et `environments.create` ont été vérifiées dans le SDK installé
(anthropic 1.11.0), mais ce script n'a JAMAIS été exécuté contre l'API réelle : si l'API refuse
un champ, lis le message d'erreur et utilise `ant apply` à la place.
"""

import os
import sys
from pathlib import Path

import anthropic

HERE = Path(__file__).resolve().parent
AGENT_FILE = HERE / "charyam-emploi.md"
ALLOWED_HOSTS = ["www.emploi.ci", "emploi.ci", "reliefweb.int"]   # garder synchronisé avec environment.yaml


def read_agent_file(path: Path):
    """Sépare le frontmatter (name, model) du corps (invite système)"""
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        sys.exit("Fichier d'agent invalide : frontmatter absent")
    _, front, body = raw.split("---", 2)
    meta = {}
    for line in front.strip().splitlines():
        if ":" in line and not line.startswith((" ", "-")):
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    return meta, body.strip()


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY n'est pas défini")
    client = anthropic.Anthropic()
    meta, system = read_agent_file(AGENT_FILE)

    agent = client.beta.agents.create(
        name=meta["name"],
        model=meta["model"],
        description=meta.get("description"),
        system=system,
        tools=[{"type": "agent_toolset_20260401"}],
    )
    environment = client.beta.environments.create(
        name="charyam-emploi-env",
        config={
            "type": "cloud",
            "networking": {
                "type": "limited",
                "allowed_hosts": ALLOWED_HOSTS,
                "allow_package_managers": False,
            },
        },
    )
    print("Agent créé :", agent.id)
    print("Environnement créé :", environment.id)
    print()
    print("À ajouter dans Render (Environment) :")
    print(f"  CHARYAM_AGENT_ID={agent.id}")
    print(f"  CHARYAM_ENVIRONMENT_ID={environment.id}")
    print("  MANAGED_AGENT_MODES=emploi")
    print("  USE_MANAGED_AGENTS=1      (mettre 0 pour revenir immédiatement à l'ancien fonctionnement)")


if __name__ == "__main__":
    main()
