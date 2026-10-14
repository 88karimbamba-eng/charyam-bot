"""
Pont entre le bot Telegram CHARYAM et Claude Managed Agents (bêta).

Principe : le bot Flask reste la passerelle (webhook, filtre sur l'identifiant du propriétaire,
Telegram). Pour les activités choisies, la demande est envoyée à une SESSION d'agent managé
(une session par chat et par activité). Si quoi que ce soit échoue, l'appelant retombe sur
l'API Messages habituelle : le bot ne reste donc jamais muet à cause de ce module.

Désactivé par défaut. Variables d'environnement
  USE_MANAGED_AGENTS        1 / true / yes / on pour activer (défaut : désactivé)
  CHARYAM_AGENT_ID          identifiant de l'agent (sortie de `ant apply` ou de setup_agent.py)
  CHARYAM_ENVIRONMENT_ID    identifiant de l'environnement
  MANAGED_AGENT_MODES       activités routées vers l'agent, séparées par des virgules (défaut : emploi)
  MANAGED_AGENT_TIMEOUT     durée maximale d'un tour en secondes (défaut : 240, borné 30 à 600)

Limites connues
  - L'association chat -> session est gardée en RAM : après un redémarrage de Render, une nouvelle
    session est créée (l'ancienne reste chez Anthropic mais n'est plus reliée).
  - Aucun fichier produit par l'agent n'est renvoyé sur Telegram (non implémenté).
"""

import os
import time
import logging
import threading

import anthropic

logger = logging.getLogger("charyam.agent")


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


ENABLED = _env_flag("USE_MANAGED_AGENTS")
AGENT_ID = os.environ.get("CHARYAM_AGENT_ID", "").strip()
ENVIRONMENT_ID = os.environ.get("CHARYAM_ENVIRONMENT_ID", "").strip()
AGENT_MODES = {
    mode.strip().lower()
    for mode in os.environ.get("MANAGED_AGENT_MODES", "emploi").split(",")
    if mode.strip()
}
TURN_TIMEOUT = _env_int("MANAGED_AGENT_TIMEOUT", 240, 30, 600)


class AgentBackendError(Exception):
    """Le tour d'agent n'a pas abouti : l'appelant doit utiliser le repli."""


_client = None
_client_lock = threading.Lock()
_sessions = {}                 # (chat_id, mode) -> identifiant de session
_sessions_lock = threading.Lock()


def is_configured() -> bool:
    return bool(ENABLED and AGENT_ID and ENVIRONMENT_ID)


def should_use_agent(mode) -> bool:
    """Vrai seulement si le module est activé, configuré, et que l'activité est routée"""
    return bool(is_configured() and mode and str(mode).lower() in AGENT_MODES)


def _get_client():
    """Client dédié : délai plus long que le client de chat, un seul nouvel essai réseau"""
    global _client
    with _client_lock:
        if _client is None:
            _client = anthropic.Anthropic(
                api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
                timeout=float(TURN_TIMEOUT),
                max_retries=1,
            )
        return _client


def forget_session(chat_id, mode):
    """Oublie la session d'un chat (elle sera recréée au prochain message)"""
    with _sessions_lock:
        _sessions.pop((chat_id, mode), None)


def _get_or_create_session(client, chat_id, mode):
    """Retourne (identifiant de session, vrai si la session vient d'être créée)"""
    key = (chat_id, mode)
    with _sessions_lock:
        session_id = _sessions.get(key)
    if session_id:
        return session_id, False
    session = client.beta.sessions.create(
        agent=AGENT_ID,
        environment_id=ENVIRONMENT_ID,
        title=f"CHARYAM {mode} (chat {chat_id})",
    )
    with _sessions_lock:
        _sessions[key] = session.id
    logger.info("Session d'agent créée pour le chat %s (activité %s)", chat_id, mode)
    return session.id, True


def _history_ids(client, session_id) -> set:
    """Identifiants des événements déjà présents : on ne relit pas les anciennes réponses"""
    try:
        history = client.beta.sessions.events.list(session_id)
        items = getattr(history, "data", history)
        return {getattr(event, "id", None) for event in items if getattr(event, "id", None)}
    except Exception as e:  # un échec ici ne doit pas bloquer la réponse
        logger.warning("Historique de session illisible (%s) : poursuite sans filtre", type(e).__name__)
        return set()


def _text_of(event) -> str:
    parts = []
    for block in getattr(event, "content", None) or []:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", None)
            if isinstance(text, str) and text:
                parts.append(text)
    return "".join(parts)


def ask_agent(chat_id, mode, user_message: str, first_name: str = "") -> str:
    """Envoie le message à la session de l'agent et retourne le texte de sa réponse.
    Lève AgentBackendError (ou toute autre exception) si le tour n'aboutit pas."""
    if not is_configured():
        raise AgentBackendError("agents managés non configurés")
    client = _get_client()
    session_id, created = _get_or_create_session(client, chat_id, mode)

    text = user_message
    if created and first_name:
        text = f"[Contexte : la personne qui t'écrit s'appelle {first_name}.]\n\n{user_message}"

    started = time.monotonic()
    deadline = started + TURN_TIMEOUT
    answer_parts = []
    tool_calls = 0
    try:
        # On ouvre le flux AVANT d'envoyer, pour ne manquer aucun événement
        with client.beta.sessions.events.stream(session_id) as stream:
            seen = set() if created else _history_ids(client, session_id)
            client.beta.sessions.events.send(
                session_id,
                events=[{"type": "user.message", "content": [{"type": "text", "text": text}]}],
            )
            for event in stream:
                event_type = getattr(event, "type", None)
                if event_type in ("event_start", "event_delta"):
                    continue
                event_id = getattr(event, "id", None)
                if event_id in seen:
                    continue
                if event_id:
                    seen.add(event_id)

                if event_type == "agent.message":
                    answer_parts.append(_text_of(event))
                elif event_type in ("agent.tool_use", "agent.mcp_tool_use"):
                    tool_calls += 1
                elif event_type == "session.error":
                    error = getattr(event, "error", None)
                    message = getattr(error, "message", None) or "erreur inconnue"
                    raise AgentBackendError(f"erreur de session : {message}")
                elif event_type == "session.status_idle":
                    stop = getattr(getattr(event, "stop_reason", None), "type", None)
                    if stop == "end_turn" or stop is None:
                        break
                    # requires_action : un outil attend une confirmation que ce bot ne gère pas.
                    # budget_reached : session en pause. Dans les deux cas, la session est bloquée.
                    forget_session(chat_id, mode)
                    raise AgentBackendError(f"session arrêtée ({stop})")

                if time.monotonic() > deadline:
                    try:
                        client.beta.sessions.events.send(session_id, events=[{"type": "user.interrupt"}])
                    except Exception:
                        pass
                    raise AgentBackendError(f"délai de {TURN_TIMEOUT} s dépassé")
    except AgentBackendError:
        raise
    except anthropic.APIStatusError as e:
        # Session introuvable ou refusée : on repart d'une session neuve au prochain message
        if getattr(e, "status_code", None) in (400, 404):
            forget_session(chat_id, mode)
        raise

    answer = "".join(answer_parts).strip()
    logger.info(
        "Tour d'agent terminé : chat=%s activité=%s durée=%.1fs appels_outils=%s caractères=%s",
        chat_id, mode, time.monotonic() - started, tool_calls, len(answer),
    )
    if not answer:
        raise AgentBackendError("réponse vide")
    return answer
