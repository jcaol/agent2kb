"""SSRF guard for every place agent2kb fetches a user- or agent-supplied URL itself.

Three layers, all fail-closed:

1. ``validate_url``: scheme, blocked names, optional domain allow-list, and DNS
   resolution where *every* returned address must be globally routable. A name
   that does not resolve, or resolves to nothing, is refused.
2. ``safe_get``: fetches with redirects handled by hand. Every hop (the first URL
   and each ``Location``) goes through layer 1 again, up to a hop limit.
3. Pinning: ``safe_get`` resolves the name once, validates the addresses, and
   connects to that exact IP. The URL handed to httpx carries the IP literal; the
   original name travels in the ``Host`` header and in the ``sni_hostname``
   request extension, so virtual hosting and TLS certificate checks still use the
   real name. A second, attacker-controlled DNS answer (DNS rebinding) therefore
   never gets a chance to point the connection at an internal address.

Not covered here: processes that do their own networking once handed a URL
(yt-dlp) and third parties that fetch on our behalf (Jina Reader). See
``docs/mcp.md`` for how those are handled.
"""

from __future__ import annotations

import contextlib
import contextvars
import ipaddress
import re
import socket
import unicodedata
import urllib.request
from typing import Iterable, Optional
from urllib.parse import urljoin, urlsplit

import httpx

from agent2kb import PROJECT_URL, __version__


#: Descriptive User-Agent: the stock httpx one is blocked by sites such as Wikipedia and Medium.
def build_user_agent(version: str = __version__, url: str = PROJECT_URL) -> str:
    """The User-Agent string. A placeholder project URL (``OWNER``) is never sent."""
    contact = "" if "OWNER" in url else f"; +{url}"
    return f"Mozilla/5.0 (compatible; agent2kb/{version}{contact})"


USER_AGENT = build_user_agent()

MAX_REDIRECTS = 5
#: Upper bound on a fetched body. Generous for PDFs, but not unbounded.
MAX_FETCH_BYTES = 100 * 1024 * 1024

class BlockedURLError(ValueError):
    """The guard refused this URL (scheme, name, credentials, allow-list, non-public address).

    A subclass of ``ValueError`` so existing handlers keep working. Callers that have
    a fallback fetch path (Jina Reader) must not use it after this error: the refusal
    is a policy decision, not a network failure.
    """


_BLOCKED_HOSTS = frozenset(
    {
        "169.254.169.254",
        "metadata.google.internal",
        "100.100.100.200",
        "localhost",
        "127.0.0.1",
        "::1",
        "0.0.0.0",
    }
)

_YT_HOSTS = frozenset({"youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com"})

# Optional domain allow-list for the current call chain (set with ``restrict_hosts``).
_allowed_domains: contextvars.ContextVar[Optional[tuple]] = contextvars.ContextVar(
    "kb_allowed_domains", default=None
)


@contextlib.contextmanager
def restrict_hosts(domains: Optional[Iterable[str]]):
    """Within the block, ``validate_url`` only accepts these domains (and subdomains).

    Applies to every hop of a redirect chain. ``None`` or empty means unrestricted.
    """
    cleaned = tuple(d.strip().lower().lstrip(".") for d in (domains or ()) if d.strip())
    token = _allowed_domains.set(cleaned or None)
    try:
        yield
    finally:
        _allowed_domains.reset(token)


def allowlist_active() -> bool:
    """True while a domain allow-list restricts the current call chain."""
    return bool(_allowed_domains.get())


def host_in_domains(host: str, domains: Iterable[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> Optional[ipaddress.IPv4Address]:
    """IPv4 address smuggled inside an IPv6 one (mapped, 6to4, NAT64)."""
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip.sixtofour:
        return ip.sixtofour
    if ip in ipaddress.ip_network("64:ff9b::/96"):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for globally routable unicast addresses."""
    if isinstance(ip, ipaddress.IPv6Address):
        inner = _embedded_ipv4(ip)
        if inner is not None and not is_public_ip(inner):
            return False
    return ip.is_global and not ip.is_multicast


def resolve_public(host: str, port: int) -> list[str]:
    """Resolve ``host`` and return its addresses, all of which must be public.

    Fails closed: resolution errors, an empty answer, or any non-public address
    raise ``ValueError``.
    """
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except (socket.gaierror, OSError, UnicodeError) as e:
        raise ValueError(f"Blocked URL: cannot resolve '{host}' ({e})") from None
    addrs: list[str] = []
    for info in infos:
        text = info[4][0].split("%", 1)[0]  # drop IPv6 zone id
        try:
            ip = ipaddress.ip_address(text)
        except ValueError:
            raise BlockedURLError(f"Blocked URL: unparseable address for '{host}'") from None
        if not is_public_ip(ip):
            raise BlockedURLError(
                f"Blocked URL: '{host}' resolves to non-public address {text}"
            )
        if text not in addrs:
            addrs.append(text)
    if not addrs:
        raise ValueError(f"Blocked URL: '{host}' resolved to no addresses")
    return addrs


_NUMERIC_LABEL = re.compile(r"0x[0-9a-f]*|[0-9]+")


def _check_static(url: str):
    """Scheme / name / allow-list checks that need no DNS. Returns the parsed URL."""
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise BlockedURLError(f"Unsupported URL scheme: '{parsed.scheme}'")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise BlockedURLError("Blocked URL: no host")
    if parsed.username or parsed.password:
        raise BlockedURLError("Blocked URL: credentials in the URL are not supported")
    if host in _BLOCKED_HOSTS or host.endswith(".localhost"):
        raise BlockedURLError(f"Blocked URL: '{host}'")
    # Shorthand IPv4 forms (2130706433, 0x7f.0.0.1, 0177.0.0.1, 127.1) are read differently by
    # different resolvers (glibc treats a leading 0 as octal, macOS as decimal), so what is
    # checked may not be what another component connects to. Only canonical dotted quads pass.
    # NFKC first: full-width digits (０１７７) would otherwise pass here and become ASCII
    # digits again when the name is IDNA-encoded for the lookup.
    labels = unicodedata.normalize("NFKC", host).split(".")
    if 1 <= len(labels) <= 4 and all(_NUMERIC_LABEL.fullmatch(lb) for lb in labels):
        try:
            canonical = ipaddress.IPv4Address(".".join(labels))
        except ValueError:
            raise BlockedURLError(f"Blocked URL: non-canonical numeric host '{host}'") from None
        if str(canonical) != host:
            raise BlockedURLError(f"Blocked URL: non-canonical numeric host '{host}'")
    # youtube.com.evil.example and friends
    if any(yt in host for yt in ("youtube", "youtu.be")) and host not in _YT_HOSTS:
        raise BlockedURLError(f"Blocked URL: suspicious YouTube-like host '{host}'")
    allowed = _allowed_domains.get()
    if allowed and not host_in_domains(host, allowed):
        raise BlockedURLError(f"Blocked URL: '{host}' is not in the allowed domains")
    try:
        parsed.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        raise ValueError("Blocked URL: invalid port") from None
    return parsed


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def validate_url(url: str) -> None:
    """Raise ``ValueError`` unless ``url`` is a public http(s) URL. Fails closed on DNS."""
    parsed = _check_static(url)
    resolve_public(parsed.hostname, parsed.port or _default_port(parsed.scheme))


def _proxied(parsed) -> bool:
    """True when the environment routes this request through an HTTP proxy.

    The proxy then does the DNS lookup and the connection, so pinning a resolved
    IP is impossible; the proxy is the egress control in that setup.
    """
    try:
        proxy = urllib.request.getproxies().get(parsed.scheme)
        return bool(proxy) and not urllib.request.proxy_bypass(parsed.hostname)
    except Exception:
        return False


def _pinned_request(client: httpx.Client, method: str, url: str, parsed, ip: str):
    """Build the request that connects to ``ip`` but still talks to ``parsed.hostname``."""
    ip_host = f"[{ip}]" if ":" in ip else ip
    port = parsed.port
    netloc = f"{ip_host}:{port}" if port else ip_host
    pinned = parsed._replace(netloc=netloc).geturl()
    host_header = parsed.hostname
    if ":" in host_header:
        host_header = f"[{host_header}]"
    if port:
        host_header = f"{host_header}:{port}"
    extensions = {"sni_hostname": parsed.hostname} if parsed.scheme == "https" else {}
    return client.build_request(
        method,
        pinned,
        headers={"Host": host_header, "User-Agent": USER_AGENT},
        extensions=extensions,
    )


def _read_capped(response: httpx.Response, limit: int) -> httpx.Response:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > limit:
            raise ValueError(f"Response too large (over {limit // (1024 * 1024)} MB)")
        chunks.append(chunk)
    headers = {
        k: v
        for k, v in response.headers.items()
        if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
    }
    return httpx.Response(
        response.status_code,
        headers=headers,
        content=b"".join(chunks),
        request=response.request,
    )


def safe_get(
    url: str,
    *,
    method: str = "GET",
    timeout: float = 30,
    max_redirects: int = MAX_REDIRECTS,
    raise_for_status: bool = True,
    transport: Optional[httpx.BaseTransport] = None,
) -> httpx.Response:
    """GET (or HEAD) ``url``: redirects followed by hand, every hop validated, connection pinned.

    The caller's domain allow-list (``restrict_hosts``) applies to every hop;
    there is deliberately no way to waive it. ``transport`` exists for tests.
    """
    current = url
    # trust_env=False below turns off env-driven TLS config too; keep honouring
    # SSL_CERT_FILE / SSL_CERT_DIR by building the context ourselves.
    verify = httpx.create_ssl_context() if transport is None else True
    for _ in range(max_redirects + 1):
        parsed = _check_static(current)
        addrs = resolve_public(parsed.hostname, parsed.port or _default_port(parsed.scheme))
        proxied = transport is None and _proxied(parsed)
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
            trust_env=proxied,  # env proxies only when we cannot pin; never netrc otherwise
            verify=verify,
        ) as client:
            if proxied or _is_ip_literal(parsed.hostname):
                request = client.build_request(
                    method, current, headers={"User-Agent": USER_AGENT}
                )
            else:
                request = _pinned_request(client, method, current, parsed, addrs[0])
            response = client.send(request, stream=True)
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Redirect without a Location header")
                    current = urljoin(current, location)
                    continue
                if raise_for_status:
                    response.raise_for_status()
                result = _read_capped(response, MAX_FETCH_BYTES)
                # The request URL carries the pinned IP; callers want the real final URL.
                result.extensions["final_url"] = current
                return result
            finally:
                response.close()
    raise ValueError(f"Too many redirects (>{max_redirects}) for URL: {url}")


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False
