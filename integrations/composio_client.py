"""Composio : exécution d'actions d'applications externes, en LECTURE SEULE dans cette version.

Mécanisme (vérifié dans la documentation et le SDK `composio` 0.22) :
  * une clé API de projet (COMPOSIO_API_KEY) identifie CHARYAM auprès de Composio ;
  * chaque application (Gmail, Notion…) doit en plus être connectée par l'utilisateur via un lien
    OAuth (« Connect Link »). Une clé API seule n'autorise AUCUNE application ;
  * l'exécution se fait avec composio.tools.execute(slug, arguments, user_id=..., version=...).
    Le SDK refuse la version « latest » sans option dangereuse : COMPOSIO_TOOL_VERSION est donc exigée.

Garde-fous : seuls les outils listés dans COMPOSIO_READONLY_TOOLS sont exécutables. Tout autre outil
(écriture, envoi, suppression, paiement…) est refusé : aucune action modifiant des données n'est
possible tant qu'un mécanisme de confirmation explicite n'a pas été ajouté.

Variables : COMPOSIO_API_KEY, COMPOSIO_USER_ID, COMPOSIO_READONLY_TOOLS (slugs séparés par des
virgules, ex. GITHUB_GET_THE_AUTHENTICATED_USER), COMPOSIO_TOOL_VERSION.
"""

import json
import logging

from integrations.common import (
    ToolResult, compact_json, env_int, env_list, env_value, redact_secrets, wrap_untrusted,
)

logger = logging.getLogger("charyam.composio")

MAX_ARGUMENTS_CHARS = 4000


def is_configured(env=None) -> bool:
    return bool(
        env_value("COMPOSIO_API_KEY", env)
        and env_value("COMPOSIO_USER_ID", env)
        and env_list("COMPOSIO_READONLY_TOOLS", env)
    )


def allowed_tools(env=None) -> list:
    return [slug.upper() for slug in env_list("COMPOSIO_READONLY_TOOLS", env)]


def _make_client(env=None):
    from composio import Composio  # import tardif
    return Composio(
        api_key=env_value("COMPOSIO_API_KEY", env),
        timeout=float(env_int("COMPOSIO_TIMEOUT_SECONDS", 30, 5, 120, env)),
        max_retries=1,
    )


def execute_readonly(tool_slug: str, arguments=None, env=None, client_factory=None) -> ToolResult:
    """Exécute un outil Composio de la liste blanche en lecture seule."""
    if not is_configured(env):
        return ToolResult.error(
            "Applications indisponibles : COMPOSIO_API_KEY, COMPOSIO_USER_ID et COMPOSIO_READONLY_TOOLS "
            "doivent être configurées."
        )
    slug = (tool_slug or "").strip().upper()
    if slug not in allowed_tools(env):
        return ToolResult.error(
            f"Action refusée : « {slug or '?'} » n'est pas dans la liste des outils en lecture seule autorisés. "
            "Aucune action modifiant des données n'est possible pour l'instant."
        )
    arguments = arguments if arguments is not None else {}
    if not isinstance(arguments, dict):
        return ToolResult.error("Arguments invalides : un objet JSON est attendu.")
    try:
        if len(json.dumps(arguments, default=str)) > MAX_ARGUMENTS_CHARS:
            return ToolResult.error("Arguments trop volumineux.")
    except (TypeError, ValueError):
        return ToolResult.error("Arguments non sérialisables.")
    version = env_value("COMPOSIO_TOOL_VERSION", env)
    if not version or version.lower() == "latest":
        return ToolResult.error(
            "COMPOSIO_TOOL_VERSION doit être une version précise de l'outil (le SDK refuse « latest » "
            "par sécurité)."
        )

    try:
        client = (client_factory or _make_client)(env)
        response = client.tools.execute(
            slug, arguments, user_id=env_value("COMPOSIO_USER_ID", env), version=version
        )
    except Exception as exc:
        name = type(exc).__name__
        detail = redact_secrets(exc, env)
        logger.warning("Composio : erreur (%s): %s", name, detail[:200])
        lowered = (name + " " + detail).lower()
        if "connected" in lowered and ("not" in lowered or "no " in lowered):
            return ToolResult.error(
                "Application non connectée : le compte doit d'abord être autorisé via le lien de connexion "
                "(voir scripts/composio_connect.py)."
            )
        if "401" in lowered or "403" in lowered or "unauthorized" in lowered or "api key" in lowered:
            return ToolResult.error("Composio a refusé l'accès : clé API invalide ou autorisation manquante.")
        if "timeout" in lowered or "timed out" in lowered:
            return ToolResult.error("Composio n'a pas répondu dans le délai imparti.")
        return ToolResult.error("L'action n'a pas pu être exécutée via Composio.")

    successful = response.get("successful") if isinstance(response, dict) else None
    if not successful:
        error = (response.get("error") if isinstance(response, dict) else None) or "échec sans détail"
        return ToolResult.error(f"L'application a répondu par une erreur : {redact_secrets(error, env)[:300]}")
    data = response.get("data") if isinstance(response, dict) else response
    return ToolResult(True, wrap_untrusted("composio:" + slug, compact_json(data)), {"tool": slug})
