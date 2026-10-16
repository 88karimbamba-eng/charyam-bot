"""Perplexity : recherche web avec sources (API « Search », POST https://api.perplexity.ai/search).

Choix : l'API Search renvoie des résultats bruts (titre, URL, extrait, date) que Claude synthétise
lui-même. Les références viennent donc uniquement de Perplexity, jamais de l'imagination du modèle.
L'ancien endpoint Sonar Chat Completions est déprécié par Perplexity : il n'est pas utilisé.

Variable d'environnement : PERPLEXITY_API_KEY
"""

import logging
import time

import httpx

from integrations.common import (
    MAX_QUERY_CHARS, ToolResult, env_value, redact_secrets, truncate, wrap_untrusted,
)

logger = logging.getLogger("charyam.perplexity")

SEARCH_URL = "https://api.perplexity.ai/search"
RECENCY_VALUES = ("hour", "day", "week", "month", "year")
MAX_RESULTS_CAP = 8
SNIPPET_CHARS = 700


def is_configured(env=None) -> bool:
    return bool(env_value("PERPLEXITY_API_KEY", env))


def _new_client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(20.0, connect=8.0))


def search(query: str, recency: str = None, max_results: int = 5, domains=None,
           env=None, http: httpx.Client = None) -> ToolResult:
    """Recherche web. Retourne toujours un ToolResult (jamais d'exception)."""
    api_key = env_value("PERPLEXITY_API_KEY", env)
    if not api_key:
        return ToolResult.error("Recherche web indisponible : PERPLEXITY_API_KEY n'est pas configurée.")
    query = (query or "").strip()
    if not query:
        return ToolResult.error("Requête de recherche vide.")
    query = query[:MAX_QUERY_CHARS]

    try:
        max_results = int(max_results)
    except (TypeError, ValueError):
        max_results = 5
    payload = {"query": query, "max_results": max(1, min(MAX_RESULTS_CAP, max_results))}
    if recency in RECENCY_VALUES:
        payload["search_recency_filter"] = recency
    if domains:
        payload["search_domain_filter"] = [str(d)[:100] for d in list(domains)[:20]]

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    own_client = http is None
    client = http or _new_client()
    try:
        response = None
        for attempt in (1, 2):  # une seule nouvelle tentative, seulement pour les erreurs transitoires
            try:
                # .request() plutôt que .post() : aucune dépendance à d'éventuels remplacements de .post
                response = client.request("POST", SEARCH_URL, headers=headers, json=payload)
            except httpx.TimeoutException:
                if attempt == 1:
                    time.sleep(1.0)
                    continue
                return ToolResult.error("La recherche web a dépassé le délai. Réessaie plus tard.")
            except httpx.HTTPError as exc:
                logger.warning("Perplexity : erreur réseau (%s)", type(exc).__name__)
                return ToolResult.error("Recherche web injoignable pour le moment.")
            if response.status_code in (429, 500, 502, 503, 504) and attempt == 1:
                time.sleep(1.5)
                continue
            break

        status = response.status_code
        if status in (401, 403):
            return ToolResult.error("Recherche web refusée : clé API Perplexity invalide ou sans droit.", status=status)
        if status == 429:
            return ToolResult.error("Limite d'utilisation Perplexity atteinte. Réessaie plus tard.", status=status)
        if status >= 400:
            logger.warning("Perplexity : HTTP %s", status)
            return ToolResult.error(f"Recherche web indisponible (HTTP {status}).", status=status)

        try:
            data = response.json()
        except ValueError:
            return ToolResult.error("Réponse Perplexity illisible.")
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            return ToolResult.error("Réponse Perplexity inattendue (pas de liste de résultats).")
        return _format_results(query, results)
    except Exception as exc:  # garde-fou : un outil ne doit jamais faire échouer le message
        logger.error("Perplexity : erreur inattendue (%s): %s", type(exc).__name__, redact_secrets(exc, env))
        return ToolResult.error("Erreur inattendue pendant la recherche web.")
    finally:
        if own_client:
            client.close()


def _format_results(query: str, results: list) -> ToolResult:
    lines, sources = [], []
    for item in results:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        number = len(sources) + 1
        sources.append({
            "title": str(item.get("title") or "")[:200],
            "url": str(item["url"]),
            "date": item.get("date") or item.get("last_updated"),
        })
        snippet = truncate(str(item.get("snippet") or "").strip(), SNIPPET_CHARS)
        date = item.get("date") or item.get("last_updated") or "date inconnue"
        lines.append(f"[{number}] {sources[-1]['title'] or '(sans titre)'}\nURL : {item['url']}\nDate : {date}\nExtrait : {snippet}")
    if not sources:
        return ToolResult(True, f"Aucun résultat pour « {query} ».", {"sources": []})
    body = "\n\n".join(lines)
    text = (
        f"Résultats de recherche pour « {query} » ({len(sources)} sources). "
        "Cite uniquement ces URL ; distingue ce que disent les extraits de ton interprétation.\n\n"
        + wrap_untrusted("perplexity", body)
    )
    return ToolResult(True, text, {"sources": sources})
