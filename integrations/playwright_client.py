"""Playwright : lecture d'une page dans un vrai navigateur (pages qui exigent du JavaScript).

IMPORTANT : installer le paquet `playwright` n'ajoute AUCUN connecteur à Claude. Ce module est une
fonction côté serveur CHARYAM, appelée par le routeur d'outils.

Deux modes (au moins un doit être configuré, sinon l'outil reste désactivé) :
  * PLAYWRIGHT_CDP_URL=wss://...   navigateur DISTANT (service dédié) : recommandé sur Render.
  * PLAYWRIGHT_LOCAL=1             Chromium local. Il consomme ~400-500 Mo de RAM : NE PAS activer
                                   sur l'offre gratuite de Render (512 Mo), le service serait tué.
Obligatoire dans les deux cas :
  * PLAYWRIGHT_ALLOWED_DOMAINS=exemple.com,autre.org   (liste blanche ; vide = outil désactivé)

Sécurité : chaque requête du navigateur est contrôlée (domaine autorisé pour les navigations,
adresses publiques seulement pour tout le reste ; images/médias/polices bloqués), un seul
navigateur à la fois, délais bornés, fermeture systématique. Lecture seule : aucun clic ni saisie.
"""

import logging
import threading

from integrations.common import (
    ToolResult, UnsafeURL, check_public_url, env_int, env_list, env_value, redact_secrets, truncate,
    wrap_untrusted,
)

logger = logging.getLogger("charyam.playwright")

_browser_slot = threading.BoundedSemaphore(1)   # un seul navigateur à la fois (mémoire)
_BLOCKED_RESOURCES = {"image", "media", "font"}
NAV_TIMEOUT_MS = 25000
ACTION_TIMEOUT_MS = 15000
SLOT_WAIT_SECONDS = 5


def _mode(env=None):
    if env_value("PLAYWRIGHT_CDP_URL", env):
        return "cdp"
    if env_value("PLAYWRIGHT_LOCAL", env) == "1":
        return "local"
    return None


def is_configured(env=None) -> bool:
    return _mode(env) is not None and bool(env_list("PLAYWRIGHT_ALLOWED_DOMAINS", env))


def read_page(url: str, wait_for_selector: str = None, extract_selector: str = None,
              env=None, _validator=None) -> ToolResult:
    """Ouvre une page, attend son chargement et en lit le texte.

    `_validator` sert uniquement aux tests réels (pages locales) ; en production il vaut toujours
    le garde-fou par défaut et n'est pas modifiable par l'environnement."""
    mode = _mode(env)
    allowed = env_list("PLAYWRIGHT_ALLOWED_DOMAINS", env)
    if mode is None:
        return ToolResult.error("Navigateur indisponible : aucun navigateur (distant ou local) n'est configuré.")
    if not allowed:
        return ToolResult.error("Navigateur indisponible : PLAYWRIGHT_ALLOWED_DOMAINS est vide (liste blanche obligatoire).")

    validate = _validator or (lambda u, domains=None: check_public_url(u, allowed_domains=domains))
    try:
        url = validate(url, allowed)
    except UnsafeURL as exc:
        return ToolResult.error(f"URL refusée : {exc}.")

    for selector in (wait_for_selector, extract_selector):
        if selector is not None and (not isinstance(selector, str) or len(selector) > 200):
            return ToolResult.error("Sélecteur CSS invalide.")

    if not _browser_slot.acquire(timeout=SLOT_WAIT_SECONDS):
        return ToolResult.error("Le navigateur est occupé par une autre demande. Réessaie dans un instant.")
    try:
        return _run(mode, url, wait_for_selector, extract_selector, allowed, validate, env)
    finally:
        _browser_slot.release()


def _run(mode, url, wait_for_selector, extract_selector, allowed, validate, env) -> ToolResult:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ToolResult.error("Navigateur indisponible : le paquet playwright n'est pas installé.")

    browser = context = None
    host_cache = {}

    def request_allowed(request_url: str, navigation: bool) -> bool:
        if request_url.startswith(("data:", "blob:", "about:")):
            return True
        key = (request_url.split("/", 3)[2] if "://" in request_url else request_url, navigation)
        if key not in host_cache:
            try:
                validate(request_url, allowed if navigation else None)
                host_cache[key] = True
            except UnsafeURL:
                host_cache[key] = False
        return host_cache[key]

    def route_handler(route):
        request = route.request
        try:
            navigation = request.is_navigation_request() and request.frame.parent_frame is None
            if request.resource_type in _BLOCKED_RESOURCES or not request_allowed(request.url, navigation):
                route.abort()
            else:
                route.continue_()
        except Exception:
            try:
                route.abort()
            except Exception:
                pass

    try:
        with sync_playwright() as pw:
            try:
                if mode == "cdp":
                    browser = pw.chromium.connect_over_cdp(
                        env_value("PLAYWRIGHT_CDP_URL", env), timeout=15000
                    )
                else:
                    browser = pw.chromium.launch(headless=True)
                context = browser.new_context(accept_downloads=False, service_workers="block")
                context.route("**/*", route_handler)
                page = context.new_page()
                page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
                page.set_default_timeout(ACTION_TIMEOUT_MS)

                page.goto(url, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except PlaywrightTimeout:
                    pass  # pages à scripts persistants : on lit ce qui est affiché
                if wait_for_selector:
                    page.wait_for_selector(wait_for_selector, timeout=ACTION_TIMEOUT_MS)

                final_url = page.url
                try:
                    validate(final_url, allowed)   # une redirection ne doit pas sortir de la liste blanche
                except UnsafeURL:
                    return ToolResult.error("La page a redirigé hors des domaines autorisés ; lecture annulée.")
                title = page.title()
                body = page.inner_text(extract_selector or "body", timeout=ACTION_TIMEOUT_MS)
            finally:
                for resource in (context, browser):
                    if resource is not None:
                        try:
                            resource.close()
                        except Exception:
                            pass
    except PlaywrightTimeout:
        return ToolResult.error("La page n'a pas fini de charger dans le délai imparti.")
    except PlaywrightError as exc:
        logger.warning("Playwright : erreur navigateur (%s): %s", type(exc).__name__, redact_secrets(exc, env)[:200])
        return ToolResult.error("Impossible d'ouvrir la page dans le navigateur.")
    except Exception as exc:
        logger.error("Playwright : erreur inattendue (%s): %s", type(exc).__name__, redact_secrets(exc, env)[:200])
        return ToolResult.error("Erreur inattendue du navigateur.")

    body = (body or "").strip()
    if not body:
        return ToolResult.error("Page ouverte mais sans texte visible.", url=final_url)
    text = f"Page : {title or '(sans titre)'}\nURL : {final_url}\n\n{truncate(body, 10000)}"
    return ToolResult(True, wrap_untrusted("navigateur", text, final_url), {"url": final_url, "title": title})
