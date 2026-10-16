"""Lecture intégrée d'une page web publique (sans service payant, sans clé).

Principe : requête HTTP simple, texte extrait avec la bibliothèque standard (aucune dépendance
ajoutée). Limites assumées : ne voit pas le contenu chargé par JavaScript et ne passe pas les pages
qui exigent une connexion (LinkedIn, Facebook, Instagram, TikTok…). Dans ce cas, le résultat dit
clairement pourquoi, au lieu de faire croire que la page est vide.

Sécurité : chaque adresse (y compris chaque redirection) passe par check_public_url ; au plus 5
redirections ; taille lue plafonnée ; seuls les contenus texte/HTML sont acceptés. Le contenu rendu
est encadré comme donnée non fiable. Limite connue : le DNS est résolu une fois pour le contrôle et
une fois par la connexion (rebinding non couvert à 100 %). Le fichier robots.txt n'est pas consulté :
c'est une lecture ponctuelle demandée par l'utilisateur, pas une exploration.
"""

import datetime
import logging
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

from integrations.common import ToolResult, UnsafeURL, check_public_url, redact_secrets, wrap_untrusted

logger = logging.getLogger("charyam.webreader")

MAX_REDIRECTS = 5
MAX_BYTES = 1_500_000
TIMEOUT_SECONDS = 15
MIN_USEFUL_CHARS = 100
USER_AGENT = "Mozilla/5.0 (compatible; CHARYAM-Bot/1.0; lecture ponctuelle a la demande de l'utilisateur)"
LOGIN_HINTS = ("sign in", "log in", "login", "connectez-vous", "se connecter", "créer un compte", "join now")

_SKIP = {"script", "style", "noscript", "svg", "template", "head", "iframe", "canvas"}
_BLOCK = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header", "footer",
          "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "main", "aside", "nav", "form"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta = {}
        self.parts = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            a = {k.lower(): (v or "") for k, v in attrs}
            key = a.get("property") or a.get("name")
            if key in ("og:title", "og:description", "description") and a.get("content"):
                self.meta.setdefault(key, a["content"].strip())
        if tag in _SKIP:
            self._skip += 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in _SKIP and self._skip:
            self._skip -= 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [" ".join(line.split()) for line in raw.split("\n")]
        out, blank = [], False
        for line in lines:
            if line:
                out.append(line)
                blank = False
            elif not blank and out:
                out.append("")
                blank = True
        return "\n".join(out).strip()


def _new_client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=8),
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1",
                 "Accept-Language": "fr,en;q=0.8"},
    )


def _fetch(client, url: str):
    """Suit les redirections à la main en revalidant chaque adresse. Retourne (réponse, octets, url_finale)."""
    for _ in range(MAX_REDIRECTS + 1):
        url = check_public_url(url)
        with client.stream("GET", url) as response:
            if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
                url = urljoin(url, response.headers["location"])
                continue
            kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if kind and not (kind.startswith("text/") or kind in ("application/xhtml+xml", "application/xml")):
                raise ValueError(f"type de contenu non pris en charge ({kind})")
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    break
            return response, bytes(body[:MAX_BYTES]), url
    raise ValueError("trop de redirections")


def read(url: str, env=None) -> ToolResult:
    """Lit UNE page publique et renvoie son texte principal."""
    try:
        url = check_public_url(url)
    except UnsafeURL as exc:
        return ToolResult.error(f"URL refusée : {exc}.")
    try:
        with _new_client() as client:
            response, body, final_url = _fetch(client, url)
    except UnsafeURL as exc:
        return ToolResult.error(f"URL refusée : {exc}.")
    except httpx.TimeoutException:
        return ToolResult.error("La page n'a pas répondu à temps.", url=url)
    except ValueError as exc:
        return ToolResult.error(f"Lecture impossible : {exc}.", url=url)
    except Exception as exc:
        logger.warning("Lecture de page : erreur (%s): %s", type(exc).__name__, redact_secrets(exc, env)[:200])
        return ToolResult.error("Lecture impossible : site injoignable ou connexion refusée.", url=url)

    status = response.status_code
    if status in (401, 403, 429) or status >= 400:
        reason = "accès refusé ou connexion exigée" if status in (401, 403) else f"réponse HTTP {status}"
        return ToolResult.error(f"La page n'a pas pu être lue ({reason}).", url=final_url)

    text_html = body.decode(response.encoding or "utf-8", errors="replace")
    parser = _TextExtractor()
    try:
        parser.feed(text_html)
        parser.close()
    except Exception:
        return ToolResult.error("Le contenu de la page n'a pas pu être analysé.", url=final_url)

    title = " ".join(parser.title.split()) or parser.meta.get("og:title", "")
    text = parser.text()
    extras = [parser.meta[k] for k in ("og:description", "description") if parser.meta.get(k) and parser.meta[k] not in text]
    if extras:
        text = "\n".join(extras[:1]) + ("\n\n" + text if text else "")
    if len(text) < MIN_USEFUL_CHARS:
        lowered = text.lower()
        wall = any(hint in lowered for hint in LOGIN_HINTS)
        why = ("la page semble exiger une connexion" if wall
               else "le contenu est probablement chargé par JavaScript ou protégé")
        return ToolResult.error(f"Contenu insuffisant : {why}. Demande à l'utilisateur de coller le texte.", url=final_url)

    fetched = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = f"Page : {title or '(sans titre)'}\nURL : {final_url}\nLue le : {fetched}\n\n"
    return ToolResult(True, wrap_untrusted("lecture_integree", header + text, final_url),
                      {"url": final_url, "title": title, "fetched_at": fetched})
