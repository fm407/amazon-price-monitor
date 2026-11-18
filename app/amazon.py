"""Amazon product identity: ASIN extraction, URL normalization, safe short-link resolution."""

import ipaddress
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
PATH_ASIN_RE = re.compile(
    r"/(?:dp|gp/product|gp/aw/d|exec/obidos/ASIN|o/ASIN|product)/([A-Za-z0-9]{10})(?=[/?#]|$)"
)
MAX_INPUT_LENGTH = 2000
MAX_REDIRECTS = 5


@dataclass(frozen=True)
class Marketplace:
    code: str
    domain: str
    currency: str


# Known marketplaces. Each has its own currency; prices are never compared across them.
MARKETPLACES = {
    m.code: m
    for m in (
        Marketplace("US", "amazon.com", "USD"),
        Marketplace("CA", "amazon.ca", "CAD"),
        Marketplace("UK", "amazon.co.uk", "GBP"),
        Marketplace("DE", "amazon.de", "EUR"),
        Marketplace("FR", "amazon.fr", "EUR"),
        Marketplace("IT", "amazon.it", "EUR"),
        Marketplace("ES", "amazon.es", "EUR"),
        Marketplace("JP", "amazon.co.jp", "JPY"),
        Marketplace("AU", "amazon.com.au", "AUD"),
        Marketplace("MX", "amazon.com.mx", "MXN"),
    )
}
SUPPORTED_MARKETPLACES = {"US"}  # widen once other marketplaces are verified end to end
DOMAIN_TO_CODE = {m.domain: m.code for m in MARKETPLACES.values()}
SHORT_LINK_HOSTS = {"a.co", "amzn.to", "amzn.com"}
HOST_PREFIXES = ("www.", "smile.", "m.")

DnsLookup = Callable[[str], list[str]]


class InvalidProductInput(ValueError):
    pass


@dataclass(frozen=True)
class ProductIdentity:
    asin: str
    marketplace: str

    @property
    def url(self) -> str:
        return canonical_url(self.asin, self.marketplace)


def canonical_url(asin: str, marketplace: str) -> str:
    return f"https://www.{MARKETPLACES[marketplace].domain}/dp/{asin}"


def _marketplace_for_host(host: str) -> str | None:
    for prefix in HOST_PREFIXES:
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    return DOMAIN_TO_CODE.get(host)


def _split(url: str):
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError as exc:
        raise InvalidProductInput("The URL is malformed.") from exc
    if parts.scheme not in ("http", "https"):
        raise InvalidProductInput("Only http and https Amazon links are supported.")
    if parts.username or parts.password:
        raise InvalidProductInput("URLs with embedded credentials are not allowed.")
    if port not in (None, 80, 443):
        raise InvalidProductInput("URLs with a custom port are not allowed.")
    if not host:
        raise InvalidProductInput("The URL has no host.")
    return parts, host


def _parse_amazon_url(url: str) -> ProductIdentity | None:
    """Return the identity for a full Amazon product URL, or None for a short link."""
    parts, host = _split(url)
    if host in SHORT_LINK_HOSTS:
        return None
    marketplace = _marketplace_for_host(host)
    if marketplace is None:
        raise InvalidProductInput("This is not a recognised Amazon product link.")
    if marketplace not in SUPPORTED_MARKETPLACES:
        raise InvalidProductInput(f"The {marketplace} marketplace is not supported yet. Use amazon.com.")
    match = PATH_ASIN_RE.search(parts.path)
    if not match:
        raise InvalidProductInput("No ASIN found in the Amazon link. Use a product page link.")
    return ProductIdentity(match.group(1).upper(), marketplace)


def system_dns_lookup(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)]


def assert_public_host(host: str, dns_lookup: DnsLookup) -> None:
    """Reject hosts that resolve to loopback, private, link-local or otherwise non-public addresses."""
    try:
        addresses = dns_lookup(host)
    except OSError as exc:
        raise InvalidProductInput("The short link host could not be resolved.") from exc
    if not addresses:
        raise InvalidProductInput("The short link host could not be resolved.")
    for address in addresses:
        if not ipaddress.ip_address(address.split("%")[0]).is_global:
            raise InvalidProductInput("The link resolves to a non-public network address.")


def resolve_short_link(url: str, client: httpx.Client, dns_lookup: DnsLookup) -> ProductIdentity:
    """Follow redirects by hand. Every hop must be an allowlisted https host on a public address."""
    for _ in range(MAX_REDIRECTS + 1):
        parts, host = _split(url)
        if parts.scheme != "https":
            raise InvalidProductInput("Short links must use https.")
        identity = _parse_amazon_url(url)  # raises for any host outside the Amazon allowlist
        if identity is not None:
            return identity
        assert_public_host(host, dns_lookup)
        try:
            response = client.get(url, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise InvalidProductInput("The short link could not be resolved.") from exc
        location = response.headers.get("location")
        if response.status_code not in (301, 302, 303, 307, 308) or not location:
            raise InvalidProductInput("The short link did not lead to an Amazon product page.")
        url = urljoin(url, location)
    raise InvalidProductInput("The short link redirected too many times.")


def parse_product_input(
    text: str,
    client: httpx.Client | None = None,
    dns_lookup: DnsLookup = system_dns_lookup,
    marketplace: str = "US",
) -> ProductIdentity:
    """Accept an ASIN, an Amazon product URL (tracking parameters are dropped) or a short link."""
    text = text.strip()
    if not text or len(text) > MAX_INPUT_LENGTH:
        raise InvalidProductInput("Enter an Amazon product link or a 10-character ASIN.")
    if ASIN_RE.match(text.upper()):
        if marketplace not in SUPPORTED_MARKETPLACES:
            raise InvalidProductInput(f"The {marketplace} marketplace is not supported yet.")
        return ProductIdentity(text.upper(), marketplace)
    if "://" not in text:
        text = "https://" + text
    identity = _parse_amazon_url(text)
    if identity is not None:
        return identity
    if client is None:
        raise InvalidProductInput("Short links cannot be resolved here. Use the full product link.")
    return resolve_short_link(text, client, dns_lookup)
