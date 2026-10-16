"""Briques communes : résultat d'outil, garde-fou d'URL (SSRF), contenu web non fiable, secrets."""

import ipaddress
import json
import os
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit

# Variables d'environnement dont la valeur ne doit jamais apparaître dans un journal ou un message
SECRET_ENV_NAMES = (
    "PERPLEXITY_API_KEY",
    "FIRECRAWL_API_KEY",
    "COMPOSIO_API_KEY",
    "PLAYWRIGHT_CDP_URL",  # peut contenir un jeton d'accès au navigateur distant
)

MAX_RESULT_CHARS = 12000          # taille maximale d'un résultat renvoyé à Claude
MAX_QUERY_CHARS = 400
UNTRUSTED_TAG = "donnees_externes_non_fiables"

_BLOCKED_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp")


@dataclass
class ToolResult:
    """Résultat d'un outil : jamais d'exception, toujours un texte exploitable par Claude."""

    ok: bool
    text: str
    meta: dict = field(default_factory=dict)

    @classmethod
    def error(cls, message: str, **meta) -> "ToolResult":
        return cls(False, message, dict(meta))


class UnsafeURL(ValueError):
    """URL refusée par le garde-fou (schéma, domaine, adresse privée…)"""


def env_value(name: str, env=None) -> str:
    source = os.environ if env is None else env
    return (source.get(name) or "").strip()


def env_int(name: str, default: int, minimum: int, maximum: int, env=None) -> int:
    try:
        value = int(env_value(name, env) or default)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def env_list(name: str, env=None) -> list:
    raw = env_value(name, env)
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def redact_secrets(value, env=None) -> str:
    """Masque les secrets d'intégration dans un texte (journaux, messages d'erreur)"""
    text = str(value)
    source = os.environ if env is None else env
    for name in SECRET_ENV_NAMES:
        secret = (source.get(name) or "").strip()
        if len(secret) >= 6:
            text = text.replace(secret, "***")
    # Filet de sécurité : jetons « Bearer » et clés ressemblant à des clés d'API
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]{10,}", "Bearer ***", text)
    text = re.sub(r"\b(pplx|fc|sk|ak)-[A-Za-z0-9_\-]{12,}", r"\1-***", text)
    return text


def truncate(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n[… contenu tronqué à {limit} caractères]"


def wrap_untrusted(source: str, content: str, url: str = "", limit: int = MAX_RESULT_CHARS) -> str:
    """Encadre un contenu venu du web : ce sont des DONNÉES, jamais des instructions.

    Le marqueur de fermeture est neutralisé dans le contenu pour qu'une page piégée
    ne puisse pas « sortir » de l'encadrement."""
    safe = re.sub(rf"(?i)</?\s*{UNTRUSTED_TAG}", "[balise neutralisée]", content or "")
    safe = truncate(safe, limit)
    attrs = f'source="{source}"'
    if url:
        attrs += f' url="{url.replace(chr(34), "%22")}"'
    return f"<{UNTRUSTED_TAG} {attrs}>\n{safe}\n</{UNTRUSTED_TAG}>"


def _host_matches(host: str, domains: list) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _ip_is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return ip.is_global


def check_public_url(url: str, allowed_domains=None, resolver=socket.getaddrinfo) -> str:
    """Valide une URL avant tout accès réseau et la retourne normalisée.

    Refuse : schémas autres que http(s), identifiants dans l'URL, ports inhabituels, noms
    internes, et tout domaine dont UNE adresse résolue n'est pas publique (réseaux privés,
    boucle locale, lien-local dont les métadonnées cloud 169.254.169.254, etc.).
    Si allowed_domains est non vide, l'hôte doit en faire partie (ou être un sous-domaine).
    Limite connue : un navigateur résout le DNS lui-même ; un changement d'adresse entre
    notre vérification et sa requête (DNS rebinding) n'est pas couvert à 100 %."""
    if not isinstance(url, str) or not url.strip() or len(url) > 2048:
        raise UnsafeURL("URL absente ou trop longue")
    url = url.strip()
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UnsafeURL("URL invalide")
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL("seules les URL http(s) sont autorisées")
    if parts.username or parts.password:
        raise UnsafeURL("identifiants dans l'URL interdits")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeURL("hôte manquant")
    if port not in (None, 80, 443):
        raise UnsafeURL("port non autorisé")
    if host == "localhost" or host.endswith(_BLOCKED_SUFFIXES):
        raise UnsafeURL("nom d'hôte interne interdit")
    if allowed_domains and not _host_matches(host, [d.lower() for d in allowed_domains]):
        raise UnsafeURL("domaine hors de la liste autorisée")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _ip_is_public(str(literal)):
            raise UnsafeURL("adresse IP non publique")
        return url

    try:
        infos = resolver(host, port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, LookupError, OSError):
        raise UnsafeURL("domaine introuvable")
    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise UnsafeURL("domaine sans adresse")
    for address in addresses:
        if not _ip_is_public(address):
            raise UnsafeURL("le domaine pointe vers une adresse non publique")
    return url


def compact_json(data, limit: int = MAX_RESULT_CHARS) -> str:
    try:
        text = json.dumps(data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(data)
    return truncate(text, limit)
