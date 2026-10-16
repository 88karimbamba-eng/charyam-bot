"""Firecrawl : récupération du contenu principal d'une page (scrape) ou d'un petit site (crawl).

SDK officiel `firecrawl-py` (import `from firecrawl import Firecrawl`), importé seulement à l'usage.
Le client attend la variable FIRECRAWL_API_KEY. On n'active jamais ignore_robots_txt ni
skip_tls_verification : les règles des sites et la vérification TLS sont respectées.

Variable d'environnement : FIRECRAWL_API_KEY (facultatif : FIRECRAWL_TIMEOUT_SECONDS, défaut 45)
"""

import datetime
import logging

from integrations.common import (
    ToolResult, UnsafeURL, check_public_url, env_int, env_value, redact_secrets, truncate, wrap_untrusted,
)

logger = logging.getLogger("charyam.firecrawl")

MAX_CRAWL_PAGES = 5
CRAWL_TOTAL_TIMEOUT = 90          # secondes, attente maximale d'un crawl complet
PER_PAGE_CHARS = 5000


def is_configured(env=None) -> bool:
    return bool(env_value("FIRECRAWL_API_KEY", env))


def _make_client(env=None):
    from firecrawl import Firecrawl  # import tardif : n'alourdit pas le démarrage
    return Firecrawl(
        api_key=env_value("FIRECRAWL_API_KEY", env),
        timeout=float(env_int("FIRECRAWL_TIMEOUT_SECONDS", 45, 10, 120, env)),
        max_retries=1,
    )


def _error_for(exc: Exception, env=None) -> ToolResult:
    name = type(exc).__name__
    text = str(exc).lower()
    if "401" in text or "unauthorized" in text or "invalid api key" in text:
        return ToolResult.error("Extraction refusée : clé API Firecrawl invalide.")
    if "402" in text or "insufficient credits" in text or "payment" in text:
        return ToolResult.error("Crédits Firecrawl épuisés.")
    if "429" in text or "rate limit" in text:
        return ToolResult.error("Limite d'utilisation Firecrawl atteinte. Réessaie plus tard.")
    if any(marker in text or marker in name.lower() for marker in ("timeout", "timed out", "timedout")):
        return ToolResult.error("L'extraction de la page a dépassé le délai.")
    logger.warning("Firecrawl : erreur (%s): %s", name, redact_secrets(exc, env)[:200])
    return ToolResult.error("Extraction impossible pour cette page (page indisponible ou refusée).")


def _field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _meta_dict(document) -> dict:
    meta = _field(document, "metadata")
    if meta is None:
        return {}
    if isinstance(meta, dict):
        return meta
    dump = getattr(meta, "model_dump", None)
    return dump() if callable(dump) else {}


def scrape(url: str, env=None, client_factory=None) -> ToolResult:
    """Récupère le contenu principal d'UNE page en markdown."""
    if not is_configured(env):
        return ToolResult.error("Extraction web indisponible : FIRECRAWL_API_KEY n'est pas configurée.")
    try:
        url = check_public_url(url)
    except UnsafeURL as exc:
        return ToolResult.error(f"URL refusée : {exc}.")
    try:
        client = (client_factory or _make_client)(env)
        document = client.scrape(url, formats=["markdown"], only_main_content=True)
    except Exception as exc:
        return _error_for(exc, env)

    markdown = (_field(document, "markdown") or "").strip()
    if not markdown:
        return ToolResult.error("Page récupérée mais sans contenu textuel exploitable.", url=url)
    meta = _meta_dict(document)
    final_url = meta.get("source_url") or meta.get("url") or url
    title = meta.get("title") or ""
    fetched = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = f"Page : {title or '(sans titre)'}\nURL : {final_url}\nRécupérée le : {fetched}\n\n"
    return ToolResult(
        True,
        wrap_untrusted("firecrawl", header + markdown, final_url),
        {"url": final_url, "title": title, "fetched_at": fetched},
    )


def crawl(url: str, limit: int = 3, env=None, client_factory=None) -> ToolResult:
    """Explore un site à partir d'une URL, au plus MAX_CRAWL_PAGES pages (consomme des crédits)."""
    if not is_configured(env):
        return ToolResult.error("Extraction web indisponible : FIRECRAWL_API_KEY n'est pas configurée.")
    try:
        url = check_public_url(url)
    except UnsafeURL as exc:
        return ToolResult.error(f"URL refusée : {exc}.")
    try:
        limit = max(1, min(MAX_CRAWL_PAGES, int(limit)))
    except (TypeError, ValueError):
        limit = 3
    try:
        client = (client_factory or _make_client)(env)
        job = client.crawl(
            url,
            limit=limit,
            scrape_options={"formats": ["markdown"], "only_main_content": True},
            poll_interval=3,
            timeout=CRAWL_TOTAL_TIMEOUT,
        )
    except Exception as exc:
        return _error_for(exc, env)

    pages = _field(job, "data") or []
    parts, urls = [], []
    for document in list(pages)[:limit]:
        markdown = (_field(document, "markdown") or "").strip()
        if not markdown:
            continue
        meta = _meta_dict(document)
        page_url = meta.get("source_url") or meta.get("url") or ""
        try:
            check_public_url(page_url)  # on n'expose pas une page dont l'URL serait non publique
        except UnsafeURL:
            continue
        urls.append(page_url)
        parts.append(f"## {meta.get('title') or '(sans titre)'}\nURL : {page_url}\n\n{truncate(markdown, PER_PAGE_CHARS)}")
    if not parts:
        return ToolResult.error("Exploration terminée sans contenu exploitable.", url=url)
    return ToolResult(
        True,
        wrap_untrusted("firecrawl", "\n\n---\n\n".join(parts), url),
        {"url": url, "pages": urls},
    )
