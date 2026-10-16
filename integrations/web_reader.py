"""Lecture intégrée d'une page web publique (sans service payant, sans clé).

Principe : requête HTTP simple, texte extrait avec la bibliothèque standard ; PDF lus avec pypdf (pur Python). Limites assumées : ne voit pas le contenu chargé par JavaScript et ne passe pas les pages
qui exigent une connexion (LinkedIn, Facebook, Instagram, TikTok…). Dans ce cas, le résultat dit
clairement pourquoi, au lieu de faire croire que la page est vide.

Sécurité : chaque adresse (y compris chaque redirection) passe par check_public_url ; au plus 5
redirections ; taille lue plafonnée ; seuls les contenus texte/HTML sont acceptés. Le contenu rendu
est encadré comme donnée non fiable. Limite connue : le DNS est résolu une fois pour le contrôle et
une fois par la connexion (rebinding non couvert à 100 %). Le fichier robots.txt n'est pas consulté :
c'est une lecture ponctuelle demandée par l'utilisateur, pas une exploration.
"""

import datetime
import io
import logging
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

from integrations.common import ToolResult, UnsafeURL, check_public_url, redact_secrets, wrap_untrusted

logger = logging.getLogger("charyam.webreader")

MAX_REDIRECTS = 5
MAX_BYTES = 1_500_000
MAX_PDF_BYTES = 12_000_000        # un PDF de termes de référence fait souvent plusieurs Mo
MAX_PDF_PAGES = 40
PDF_RESULT_CHARS = 30000          # un PDF est tronqué plus tard qu'une page (coût en jetons assumé)
MAX_LINKED_DOCS = 8
DOC_EXTENSIONS = (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".zip")
TIMEOUT_SECONDS = 15
MIN_USEFUL_CHARS = 100
USER_AGENT = "Mozilla/5.0 (compatible; CHARYAM-Bot/1.0; lecture ponctuelle a la demande de l'utilisateur)"
LOGIN_HINTS = ("sign in", "log in", "login", "connectez-vous", "se connecter", "créer un compte", "join now")

_SKIP = {"script", "style", "noscript", "svg", "template", "head", "iframe", "canvas"}
_BLOCK = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header", "footer",
          "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "main", "aside", "nav", "form"}


class _TextExtractor(HTMLParser):
    def __init__(self, base_url: str = ""):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links = []          # (libellé, url absolue) des documents liés
        self._href = None
        self._label = []
        self.title = ""
        self.meta = {}
        self.parts = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._label = []
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
        if tag == "a" and self._href:
            label = " ".join("".join(self._label).split())
            target = urljoin(self.base_url, self._href.strip())
            path = target.split("?")[0].split("#")[0].lower()
            if path.endswith(DOC_EXTENSIONS) and (label, target) not in self.links:
                self.links.append((label, target))
            self._href = None
        if tag == "title":
            self._in_title = False
        if tag in _SKIP and self._skip:
            self._skip -= 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._href is not None:
            self._label.append(data)
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
            is_pdf = kind == "application/pdf" or (kind in ("", "application/octet-stream") and url.lower().split("?")[0].endswith(".pdf"))
            if not is_pdf and kind and not (kind.startswith("text/") or kind in ("application/xhtml+xml", "application/xml")):
                raise ValueError(f"type de contenu non pris en charge ({kind})")
            limit = MAX_PDF_BYTES if is_pdf else MAX_BYTES
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > limit:
                    break
            if is_pdf and len(body) > limit:
                raise ValueError("PDF trop volumineux (plus de 12 Mo)")
            return response, bytes(body[:limit]), url, is_pdf
    raise ValueError("trop de redirections")


def read(url: str, env=None) -> ToolResult:
    """Lit UNE page publique et renvoie son texte principal."""
    try:
        url = check_public_url(url)
    except UnsafeURL as exc:
        return ToolResult.error(f"URL refusée : {exc}.")
    try:
        with _new_client() as client:
            response, body, final_url, is_pdf = _fetch(client, url)
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

    if is_pdf:
        return _read_pdf(body, final_url)

    text_html = body.decode(response.encoding or "utf-8", errors="replace")
    parser = _TextExtractor(final_url)
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
    docs = ""
    if parser.links:
        lines = [f"- {label or '(sans libellé)'} : {link}" for label, link in parser.links[:MAX_LINKED_DOCS]]
        docs = ("\n\nDocuments liés sur cette page (lisibles avec lire_page_web si ce sont des PDF) :\n"
                + "\n".join(lines))
    return ToolResult(True, wrap_untrusted("lecture_integree", header + text + docs, final_url),
                      {"url": final_url, "title": title, "fetched_at": fetched, "documents": [u for _, u in parser.links]})


def _read_pdf(body: bytes, url: str) -> ToolResult:
    """Extrait le texte d'un PDF public (pypdf, pur Python). Un PDF scanné (images) n'a pas de texte."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ToolResult.error("Lecture des PDF indisponible sur ce serveur.", url=url)
    try:
        reader = PdfReader(io.BytesIO(body))
        if reader.is_encrypted:
            return ToolResult.error("Ce PDF est protégé par mot de passe.", url=url)
        total = len(reader.pages)
        parts = []
        for index, page in enumerate(reader.pages[:MAX_PDF_PAGES], start=1):
            page_text = (page.extract_text() or "").strip()
            if page_text:
                parts.append(f"[page {index}]\n{page_text}")
    except Exception as exc:
        logger.warning("Lecture PDF : erreur (%s)", type(exc).__name__)
        return ToolResult.error("Le PDF n'a pas pu être lu (fichier corrompu ou format inhabituel).", url=url)
    text = re.sub(r"[ \t]+", " ", "\n\n".join(parts)).strip()
    if len(text) < MIN_USEFUL_CHARS:
        return ToolResult.error(
            "PDF sans texte exploitable (probablement un document scanné). Demande à l'utilisateur de coller le texte.",
            url=url)
    note = f"\n[PDF de {total} pages : seules les {MAX_PDF_PAGES} premières sont lues]" if total > MAX_PDF_PAGES else ""
    fetched = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = f"Document PDF ({total} pages)\nURL : {url}\nLu le : {fetched}\n\n"
    return ToolResult(True, wrap_untrusted("lecture_integree", header + text + note, url, limit=PDF_RESULT_CHARS),
                      {"url": url, "pages": total, "fetched_at": fetched})
