"""Fetch + extract pipeline behind scrape_url (no JS rendering).

Responsibilities:
  * HTTP fetch with browser-like headers, streamed size cap, one retry on
    transient failures.
  * TLS: certificate verification is always ON. When a server omits its
    intermediate certificate, the missing issuer is fetched from the leaf's
    Authority Information Access (AIA) extension, and the request is retried
    once with a context trusting certifi roots plus that intermediate. The
    leaf plus downloaded issuers are first validated against the certifi
    roots (chain and hostname) with cryptography's WebPKI verifier, and
    nothing is added to a trust store unless that passes, so a hostile AIA
    URL cannot widen trust.
  * SSRF guard: only http(s) to addresses the framework's
    `magone_mcp_core.net.is_blocked_address` accepts (the one blocklist every
    connector shares), re-checked on every redirect hop, with the connection
    pinned to the vetted IP so DNS rebinding between check and connect is
    closed.
  * PDF text extraction (pypdf) with byte, page and char caps.
  * HTML to markdown, keeping tables that trafilatura tends to drop.

Every failure is raised as ScrapeError carrying a stable, actionable code.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
import warnings
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import certifi
import httpx
import structlog
import trafilatura
from magone_mcp_core.net import is_blocked_address

log = structlog.get_logger()

# Error codes surfaced to the agent.
TLS_ERROR = "tls_error"
HTTP_403 = "http_403"
TIMEOUT = "timeout"
PDF_NO_TEXT = "pdf_no_text"
UNSUPPORTED = "unsupported_content_type"
EMPTY = "empty_extraction"
TOO_LARGE = "too_large"
FETCH_FAILED = "fetch_failed"
BLOCKED_URL = "blocked_url"
PDF_PARSE_ERROR = "pdf_parse_error"

MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
MAX_HTML_BYTES = 8 * 1024 * 1024
MAX_PDF_PAGES = 100
MAX_REDIRECTS = 5
EXTRACT_TIMEOUT_S = 30
TOTAL_DEADLINE_S = 60

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.5",
    "Upgrade-Insecure-Requests": "1",
}
_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
_RETRY_STATUS = {429, 502, 503, 504}


class ScrapeError(Exception):
    """A scrape failure with a machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(slots=True, frozen=True)
class Fetched:
    final_url: str
    status: int
    content_type: str
    body: bytes


# --------------------------------------------------------------------------
# TLS: AIA chasing
# --------------------------------------------------------------------------

_AIA_MAX_DEPTH = 3
_AIA_MAX_BYTES = 256 * 1024
_AIA_CACHE_MAX = 256
# AIA url -> PEM of the issuer certificate(s) it served.
_intermediate_cache: dict[str, bytes] = {}
# host -> SSLContext that trusts certifi roots plus that host's intermediates.
_host_ctx_cache: dict[str, ssl.SSLContext] = {}


def _default_ctx() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())


def _is_missing_issuer(exc: BaseException) -> bool:
    """True when the failure is a verify error caused by an absent issuer."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLCertVerificationError):
            # 20 = unable to get local issuer cert, 21 = unable to verify the
            # first certificate. Both mean the chain was incomplete.
            return cur.verify_code in (20, 21) or "unable to get local issuer" in str(cur)
        cur = cur.__cause__ or cur.__context__
    return False


def _is_tls_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLError):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


RESOLVE_TIMEOUT_S = 10


def _is_public_ip(ip) -> bool:
    """The framework's verdict, so this module carries no blocklist of its own."""
    return not is_blocked_address(ip)


def _resolve_public(host: str, port: int) -> list[str]:
    """Resolve host to vetted IPs. Raises BLOCKED_URL if any address is non-global.

    Covers loopback, private, link-local (169.254.169.254), CGNAT, multicast,
    unspecified, NAT64/6to4, bare in-cluster names (api, vault) and localhost.
    """
    bare = host.strip("[]").rstrip(".")
    try:
        ips = [ipaddress.ip_address(bare)]
    except ValueError:
        if bare.lower() == "localhost" or "." not in bare:
            raise ScrapeError(BLOCKED_URL, f"host {host!r} is not a public internet host") from None
        try:
            infos = socket.getaddrinfo(bare, port, type=socket.SOCK_STREAM)
        except OSError as e:
            raise ScrapeError(FETCH_FAILED, f"could not resolve {host}: {e}") from e
        ips = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]
    if not ips:
        raise ScrapeError(FETCH_FAILED, f"could not resolve {host}")
    for ip in ips:
        if not _is_public_ip(ip):
            raise ScrapeError(BLOCKED_URL, f"{host} resolves to a non-public address, refusing")
    return list(dict.fromkeys(str(ip) for ip in ips))


async def _resolve(host: str, port: int) -> list[str]:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_resolve_public, host, port), timeout=RESOLVE_TIMEOUT_S
        )
    except TimeoutError as e:
        raise ScrapeError(FETCH_FAILED, f"DNS lookup for {host} timed out") from e


def _pin(url: str, ip: str) -> tuple[str, dict[str, str], dict[str, str]]:
    """Rewrite url to connect to the vetted ip, keeping Host and TLS SNI/verification."""
    u = urlparse(url)
    host = u.hostname or ""
    default = 443 if u.scheme == "https" else 80
    port = u.port or default
    hostport = host if port == default else f"{host}:{port}"
    ip_lit = f"[{ip}]" if ":" in ip else ip
    path = u.path or "/"
    target = f"{u.scheme}://{ip_lit}:{port}{path}" + (f"?{u.query}" if u.query else "")
    ext = {"sni_hostname": host} if u.scheme == "https" else {}
    return target, {"Host": hostport}, ext


def precheck_url(url: str) -> None:
    """Refuse what needs no DNS lookup to refuse: a non-http(s) scheme, a URL
    without a host, localhost, and a single-label name such as `api` or
    `vault`, which only a cluster-internal resolver answers. Raises
    ScrapeError(BLOCKED_URL)."""
    _check_url(url)
    host = (urlparse(url).hostname or "").strip("[]").rstrip(".")
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        pass
    if host.lower() == "localhost" or "." not in host:
        raise ScrapeError(BLOCKED_URL, f"host {host!r} is not a public internet host")


def _check_url(url: str) -> None:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ScrapeError(BLOCKED_URL, f"only http and https URLs are allowed: {url}")


def _load_certs(data: bytes) -> list:
    """Parse DER, PEM or PKCS7 bytes into certificates."""
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import pkcs7

    if b"-----BEGIN CERTIFICATE-----" in data:
        certs = x509.load_pem_x509_certificates(data)
    else:
        try:
            certs = [x509.load_der_x509_certificate(data)]
        except ValueError:
            try:
                certs = pkcs7.load_der_pkcs7_certificates(data)
            except ValueError:
                certs = pkcs7.load_pem_pkcs7_certificates(data)
    if not certs:
        raise ValueError("no certificates found")
    return certs


def _ca_issuer_urls(cert) -> list[str]:
    from cryptography import x509
    from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID

    try:
        ext = cert.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS)
    except x509.ExtensionNotFound:
        return []
    return [
        d.access_location.value
        for d in ext.value
        if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS
        and isinstance(d.access_location, x509.UniformResourceIdentifier)
        and d.access_location.value.lower().startswith(("http://", "https://"))
    ]


async def _download_issuer(url: str) -> bytes:
    """Download an issuer certificate (cached). Returns PEM bytes."""
    from cryptography.hazmat.primitives.serialization import Encoding

    cached = _intermediate_cache.get(url)
    if cached is not None:
        return cached
    u = urlparse(url)
    try:
        _check_url(url)
        ips = await _resolve(u.hostname or "", u.port or (443 if u.scheme == "https" else 80))
    except ScrapeError as e:
        raise ScrapeError(TLS_ERROR, f"refusing AIA issuer URL: {e.message}") from e
    target, headers, ext = _pin(url, ips[0])
    async with httpx.AsyncClient(timeout=10, verify=_default_ctx(), trust_env=False) as client:
        async with client.stream(
            "GET", target, headers=headers, extensions=ext, follow_redirects=False
        ) as resp:
            if resp.status_code != 200:
                raise ScrapeError(
                    TLS_ERROR, f"AIA issuer download returned HTTP {resp.status_code}"
                )
            data = b""
            async for chunk in resp.aiter_bytes():
                data += chunk
                if len(data) > _AIA_MAX_BYTES:
                    raise ScrapeError(TLS_ERROR, "AIA issuer certificate too large")
    try:
        certs = _load_certs(data)
    except ValueError as e:
        raise ScrapeError(TLS_ERROR, f"AIA issuer is not a certificate: {e}") from e
    pem = b"".join(c.public_bytes(Encoding.PEM) for c in certs)
    if len(_intermediate_cache) >= _AIA_CACHE_MAX:
        _intermediate_cache.pop(next(iter(_intermediate_cache)))
    _intermediate_cache[url] = pem
    return pem


async def _peer_leaf_der(host: str, port: int) -> bytes:
    """Read the server's leaf certificate with an unverified handshake.

    This only reads the certificate, no request data is sent. Trust is decided
    afterwards by the verified retry, never by this connection.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    writer = None
    last: OSError | None = None
    for ip in await _resolve(host, port):
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port, ssl=ctx, server_hostname=host), timeout=10
            )
            break
        except (OSError, TimeoutError) as e:
            last = e
    if writer is None:
        raise ScrapeError(FETCH_FAILED, f"could not read certificate from {host}: {last}")
    try:
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
    finally:
        writer.close()
    if not der:
        raise ScrapeError(TLS_ERROR, "server presented no certificate")
    return der


_certifi_store_cache = None


def _certifi_store():
    """cryptography Store of the certifi roots (parsed once)."""
    global _certifi_store_cache
    if _certifi_store_cache is None:
        from cryptography import x509
        from cryptography.x509.verification import Store

        pem = Path(certifi.where()).read_text()
        roots = []
        with warnings.catch_warnings():
            # One legacy root has a non-positive serial; it still loads today.
            warnings.simplefilter("ignore")
            for block in re.findall(
                r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", pem, re.S
            ):
                try:
                    roots.append(x509.load_pem_x509_certificate(block.encode("ascii")))
                except ValueError:
                    continue
        _certifi_store_cache = Store(roots)
    return _certifi_store_cache


def _validate_chain(leaf, issuers: list, host: str) -> None:
    """Raise tls_error unless leaf chains to a certifi root via issuers for host."""
    from cryptography.x509 import DNSName, IPAddress
    from cryptography.x509.verification import PolicyBuilder, VerificationError

    try:
        try:
            subject = IPAddress(ipaddress.ip_address(host.strip("[]")))
        except ValueError:
            subject = DNSName(host)
        verifier = PolicyBuilder().store(_certifi_store()).build_server_verifier(subject)
        verifier.verify(leaf, issuers)
    except (VerificationError, ValueError) as e:
        raise ScrapeError(
            TLS_ERROR,
            f"certificate for {host} does not validate against trusted roots even with AIA "
            f"issuers: {e}",
        ) from e


def _ctx_key(url: str) -> str:
    u = urlparse(url)
    return f"{u.hostname}:{u.port or (443 if u.scheme != 'http' else 80)}"


async def build_aia_context(url: str) -> ssl.SSLContext:
    """SSLContext trusting certifi roots plus the intermediates the server omitted.

    url must be the hop that failed verification. Every failure is a ScrapeError.
    """
    try:
        return await _build_aia_context(url)
    except ScrapeError:
        raise
    except (ssl.SSLError, ValueError, IndexError) as e:
        raise ScrapeError(TLS_ERROR, f"AIA chain building failed: {e}") from e
    except (OSError, TimeoutError, httpx.HTTPError) as e:
        raise ScrapeError(FETCH_FAILED, f"AIA chain building failed: {e}") from e


async def _build_aia_context(url: str) -> ssl.SSLContext:
    from cryptography import x509

    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or 443
    key = _ctx_key(url)
    cached = _host_ctx_cache.get(key)
    if cached is not None:
        return cached

    leaf = x509.load_der_x509_certificate(await _peer_leaf_der(host, port))
    chain_pem = b""
    current = leaf
    for _ in range(_AIA_MAX_DEPTH):
        urls = _ca_issuer_urls(current)
        if not urls:
            break
        pem = b""
        for u in urls:
            try:
                pem = await _download_issuer(u)
                break
            except (httpx.HTTPError, ScrapeError) as e:
                log.warning("aia_download_failed", url=u, error=str(e))
        if not pem:
            break
        chain_pem += pem
        current = _load_certs(pem)[0]
        if current.issuer == current.subject:
            break
    if not chain_pem:
        raise ScrapeError(
            TLS_ERROR,
            f"certificate chain for {host} is incomplete and no issuer could be fetched via AIA",
        )
    # Never trust downloaded certs on faith: the leaf must verify for this
    # hostname against the certifi roots using exactly these issuers.
    _validate_chain(leaf, _load_certs(chain_pem), host)
    ctx = _default_ctx()
    ctx.load_verify_locations(cadata=chain_pem.decode("ascii"))
    if len(_host_ctx_cache) >= _AIA_CACHE_MAX:
        _host_ctx_cache.pop(next(iter(_host_ctx_cache)))
    _host_ctx_cache[key] = ctx
    return ctx


# --------------------------------------------------------------------------
# HTTP fetch
# --------------------------------------------------------------------------


async def _stream_get(url: str, ctx: ssl.SSLContext) -> Fetched:
    """GET url with SSRF checks on every hop and the connection pinned to a vetted IP.

    Transport errors carry the failing hop as ``hop_url`` so callers can
    act on the right host after redirects.
    """
    for _ in range(MAX_REDIRECTS + 1):
        _check_url(url)
        u = urlparse(url)
        ips = await _resolve(u.hostname or "", u.port or (443 if u.scheme == "https" else 80))
        for n, ip in enumerate(ips):
            target, host_header, ext = _pin(url, ip)
            try:
                # trust_env=False: an env proxy would resolve the name itself
                # and defeat the pinning above.
                async with httpx.AsyncClient(
                    verify=ctx,
                    headers=BROWSER_HEADERS,
                    timeout=_TIMEOUT,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    async with client.stream(
                        "GET", target, headers=host_header, extensions=ext
                    ) as resp:
                        if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get(
                            "location"
                        ):
                            url = urljoin(url, resp.headers["location"])
                            break
                        return await _read_response(url, resp)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                if _is_tls_error(e) or n == len(ips) - 1:
                    e.hop_url = url  # type: ignore[attr-defined]
                    raise
                continue  # unreachable address, try the next vetted one
            except httpx.TransportError as e:
                e.hop_url = url  # type: ignore[attr-defined]
                raise
    raise ScrapeError(FETCH_FAILED, f"too many redirects (more than {MAX_REDIRECTS})")


async def _read_response(url: str, resp: httpx.Response) -> Fetched:
    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if resp.status_code != 200:
        return Fetched(url, resp.status_code, ctype, b"")
    declared = resp.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
        raise ScrapeError(
            TOO_LARGE,
            f"file is {int(declared) // 1024 // 1024} MB, limit is "
            f"{MAX_DOWNLOAD_BYTES // 1024 // 1024} MB",
        )
    buf = bytearray()
    async for chunk in resp.aiter_bytes():
        buf += chunk
        if len(buf) > MAX_DOWNLOAD_BYTES:
            raise ScrapeError(
                TOO_LARGE, f"download exceeded {MAX_DOWNLOAD_BYTES // 1024 // 1024} MB limit"
            )
    return Fetched(url, 200, ctype, bytes(buf))


async def _get_once(url: str) -> Fetched:
    """One logical attempt: default trust store, then one AIA-assisted retry.

    A previously built AIA context for this host is tried first so a known
    incomplete-chain host does not repeat the failing handshake. A stale
    context is evicted and rebuilt once.
    """
    key = _ctx_key(url)
    cached = _host_ctx_cache.get(key)
    if cached is not None:
        try:
            return await _stream_get(url, cached)
        except httpx.TransportError as e:
            if not _is_tls_error(e):
                raise
            _host_ctx_cache.pop(key, None)
    hop = url
    try:
        return await _stream_get(url, _default_ctx())
    except httpx.TransportError as e:
        if not _is_missing_issuer(e):
            raise
        hop = getattr(e, "hop_url", url)
        log.info("tls_missing_issuer_trying_aia", url=hop)
    ctx = await build_aia_context(hop)
    try:
        return await _stream_get(url, ctx)
    except httpx.TransportError as e:
        if _is_tls_error(e):
            _host_ctx_cache.pop(_ctx_key(hop), None)
            raise ScrapeError(
                TLS_ERROR, f"certificate verification failed for {hop} even after AIA: {e}"
            ) from e
        raise


async def fetch_url(url: str) -> Fetched:
    """Fetch url with one retry on transient errors. Raises ScrapeError."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ScrapeError(BLOCKED_URL, f"only http and https URLs are allowed: {url}")

    async def attempt() -> Fetched:
        return await _get_once(url)

    last: Exception | None = None
    try:
        async with asyncio.timeout(TOTAL_DEADLINE_S):
            for i in range(2):
                try:
                    res = await attempt()
                except ScrapeError:
                    raise
                except httpx.TimeoutException as e:
                    last = e
                except httpx.TransportError as e:
                    if _is_tls_error(e):
                        raise ScrapeError(
                            TLS_ERROR, f"TLS failure for {parsed.hostname}: {e}"
                        ) from e
                    last = e
                else:
                    if res.status in _RETRY_STATUS and i == 0:
                        last = None
                        await asyncio.sleep(0.5)
                        continue
                    _raise_for_status(res)
                    return res
                if i == 0:
                    await asyncio.sleep(0.5)
    except TimeoutError as e:
        raise ScrapeError(TIMEOUT, f"timed out fetching {url}") from e
    if isinstance(last, httpx.TimeoutException):
        raise ScrapeError(TIMEOUT, f"timed out fetching {url}") from last
    raise ScrapeError(FETCH_FAILED, f"could not connect to {parsed.hostname}: {last}") from last


def _raise_for_status(res: Fetched) -> None:
    if res.status == 200:
        return
    if res.status == 403:
        raise ScrapeError(
            HTTP_403, "server refused access (HTTP 403), it likely blocks automated clients"
        )
    raise ScrapeError(f"http_{res.status}", f"server returned HTTP {res.status}")


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


def is_pdf(res: Fetched) -> bool:
    return "pdf" in res.content_type or res.body[:1024].lstrip().startswith(b"%PDF")


def _cut(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text.rfind(".", 0, max_chars)
    cut = cut + 1 if cut > max_chars // 2 else max_chars
    return text[:cut]


def _tidy_pdf_text(text: str) -> str:
    """Strip per-line padding and collapse the blank runs PDFs are full of."""
    lines = [ln.strip() for ln in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def extract_pdf(body: bytes, max_chars: int, max_pages: int = MAX_PDF_PAGES) -> str:
    import io

    import pypdf

    try:
        reader = pypdf.PdfReader(io.BytesIO(body))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ScrapeError(PDF_NO_TEXT, "PDF is password protected, no text can be extracted")
        total = len(reader.pages)
        parts: list[str] = []
        size = 0
        done = 0
        for i, page in enumerate(reader.pages):
            if i >= max_pages or size >= max_chars:
                break
            text = _tidy_pdf_text(page.extract_text() or "")
            done = i + 1
            if text:
                parts.append(f"[Page {i + 1}]\n{text}")
                size += len(text) + 12
    except ScrapeError:
        raise
    except Exception as e:  # pypdf raises a wide, undocumented range on hostile input
        raise ScrapeError(PDF_PARSE_ERROR, f"PDF could not be parsed ({type(e).__name__})") from e
    if not parts:
        raise ScrapeError(
            PDF_NO_TEXT,
            f"PDF has no extractable text ({total} pages), it is likely a scanned image",
        )
    out = "\n\n".join(parts)
    if done < total or len(out) > max_chars:
        note = f"\n\n[Truncated: showing text from pages 1-{done} of {total}]"
        return _cut(out, max(max_chars - len(note), 0)) + note
    return out


_WS = re.compile(r"\s+")
_DROP_TAGS = {
    "script", "style", "noscript", "nav", "footer", "aside", "svg", "template",
    "iframe", "select", "option", "canvas", "head",
}  # fmt: skip
_BLOCK_TAGS = {
    "p", "div", "section", "article", "main", "ul", "ol", "li", "dl", "dt", "dd",
    "blockquote", "pre", "figure", "figcaption", "form", "fieldset", "address",
    "details", "summary", "header", "br", "hr", "tr",
}  # fmt: skip
_HEADINGS = {f"h{i}": i for i in range(1, 7)}


def _ws(text: str) -> str:
    return _WS.sub(" ", text.replace("\xa0", " ")).strip()


_ICON_TOKENS = {"fa", "fas", "far", "fab", "fal", "fad", "mat-icon", "glyphicon"}
_ICON_PREFIXES = ("material-icons", "material-symbols", "fa-", "glyphicon-")


def _is_icon(el) -> bool:
    """Exact class-token match for icon-font glyph elements (not icon-box etc.)."""
    return any(
        t in _ICON_TOKENS or t.startswith(_ICON_PREFIXES) for t in (el.get("class") or "").split()
    )


def _hidden(el) -> bool:
    if el.get("hidden") is not None:
        return True
    # Icon-font glyphs are plain words in the DOM ("favorite_border", "email").
    if _is_icon(el):
        return True
    style = (el.get("style") or "").replace(" ", "").lower()
    return "display:none" in style


def _table_md(table) -> str:
    rows: list[list[str]] = []
    for tr in table.iter("tr"):
        # Skip rows that belong to a nested table.
        owner = next((a for a in tr.iterancestors() if a.tag == "table"), None)
        if owner is not table:
            continue
        cells = [c for c in tr if c.tag in ("td", "th")]
        if not cells:
            continue
        rows.append([_ws(c.text_content()).replace("|", "/") for c in cells])
    if not rows or sum(1 for r in rows for c in r if c) == 0:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def html_to_markdown(html: str) -> str:
    """Whole-page DOM to markdown: headings, lists, tables, boilerplate dropped."""
    from lxml import html as lxml_html

    try:
        doc = lxml_html.fromstring(html)
    except (ValueError, lxml_html.etree.ParserError):
        return ""
    for icon in [e for e in doc.iter() if isinstance(e.tag, str) and _hidden(e)]:
        icon.drop_tree()
    body = doc.find("body")
    root = body if body is not None else doc
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        text = _ws(" ".join(buf))
        buf.clear()
        if text:
            out.append(text)

    def visit(e) -> None:
        tag = e.tag
        if not isinstance(tag, str) or tag in _DROP_TAGS or _hidden(e):
            return
        if tag in _HEADINGS:
            flush()
            text = _ws(e.text_content())
            if text:
                out.append("#" * _HEADINGS[tag] + " " + text)
            return
        if tag == "table":
            flush()
            md = _table_md(e)
            if md:
                out.append(md)
            return
        block = tag in _BLOCK_TAGS
        start = len(out)
        if block:
            flush()
            start = len(out)
        if e.text:
            buf.append(e.text)
        for c in e:
            visit(c)
            if c.tail:
                buf.append(c.tail)
        if block:
            flush()
            if tag == "li" and len(out) > start:
                out[start] = "- " + out[start]

    visit(root)
    flush()
    deduped = [b for i, b in enumerate(out) if i == 0 or b != out[i - 1]]
    return "\n\n".join(deduped)


_LAYOUT_ANCESTORS = {"nav", "footer", "aside", "header"}
_LAYOUT_HINT = re.compile(
    r"cookie|consent|gdpr|cmplz|onetrust|sidebar|breadcrumb|navbar|menu|calendar-widget", re.I
)
_HEADING_XPATH = (
    "preceding::*[self::h1 or self::h2 or self::h3 or self::h4 or self::h5 or self::h6][1]"
)


def _hinted(el) -> bool:
    return bool(_LAYOUT_HINT.search(f"{el.get('class') or ''} {el.get('id') or ''}"))


def _is_data_table(t) -> bool:
    """A real data table: not nested, not in page chrome/cookie UI, 2+ columns, 4+ cells."""
    if t.xpath(".//table") or t.xpath("ancestor::table") or t.get("role") == "presentation":
        return False
    if _hinted(t) or any(a.tag in _LAYOUT_ANCESTORS or _hinted(a) for a in t.iterancestors()):
        return False
    rows = [[c for c in tr if c.tag in ("td", "th")] for tr in t.iter("tr")]
    if max((len(r) for r in rows), default=0) < 2:
        return False
    texts = [_ws(c.text_content()) for r in rows for c in r]
    # File-listing style tables (mostly empty cells / bare links) are page chrome.
    if sum(1 for x in texts if x) < len(texts) * 0.6:
        return False
    return len({x for x in texts if len(x) > 1}) >= 4


def _dropped_tables(doc, extracted: str) -> list:
    """Data tables whose cell text is mostly absent from the extracted text."""
    out = []
    for t in doc.iter("table"):
        if not _is_data_table(t):
            continue
        cells = [c for c in {_ws(c.text_content()) for c in t.iter("td", "th")} if len(c) > 1]
        if sum(1 for c in cells if c in extracted) / len(cells) < 0.6:
            out.append(t)
    return out


def decode_html(res: Fetched) -> str:
    body = res.body[:MAX_HTML_BYTES]
    m = re.search(r"charset=([\w-]+)", res.content_type)
    enc = m.group(1) if m else None
    if enc is None:
        m2 = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.I)
        enc = m2.group(1).decode("ascii") if m2 else "utf-8"
    try:
        return body.decode(enc, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


# When this many real data tables were dropped, the extractor misread the page
# (not a clean article): fall back to the whole-page DOM walk instead of
# patching individual tables onto its output.
_WALKER_TABLE_THRESHOLD = 3
_MIN_TEXT = 50


def extract_html(html: str, max_chars: int) -> str:
    """trafilatura output; real data tables it dropped are recovered.

    Few dropped tables are appended (under their nearest heading) to the
    untouched trafilatura text. Layout, nested, nav/footer/aside and cookie
    tables are ignored so ordinary pages keep their clean output.
    """
    from lxml import html as lxml_html

    kw = {"include_links": True, "include_tables": True, "output_format": "markdown"}
    text = trafilatura.extract(html, **kw) or ""
    if len(text) < _MIN_TEXT:
        # Default precision found (almost) nothing: retry favouring recall.
        text = trafilatura.extract(html, favor_recall=True, **kw) or text
    try:
        doc = lxml_html.fromstring(html)
    except (ValueError, lxml_html.etree.ParserError):
        return _cut(text, max_chars)
    dropped = _dropped_tables(doc, text)
    if len(dropped) >= _WALKER_TABLE_THRESHOLD:
        full = html_to_markdown(html)
        if len(full) > len(text):
            text = full
    elif dropped:
        parts = [text] if text else []
        for t in dropped:
            md = _table_md(t)
            if not md:
                continue
            head = t.xpath(_HEADING_XPATH)
            title = _ws(head[0].text_content()) if head else ""
            parts.append(f"## {title}\n\n{md}" if title else md)
        text = "\n\n".join(parts)
    return _cut(text, max_chars)


def extract(res: Fetched, max_chars: int) -> tuple[str, str]:
    """Return (content, kind) where kind is 'pdf' or 'html'. Raises ScrapeError."""
    if is_pdf(res):
        return extract_pdf(res.body, max_chars), "pdf"
    ct = res.content_type
    textual = (
        not ct
        or ct.startswith("text/")
        or "html" in ct
        or "xml" in ct
        or ct in ("application/json", "application/xhtml+xml")
    )
    if not textual:
        raise ScrapeError(
            UNSUPPORTED, f"content type {ct!r} cannot be read as text (supported: HTML, PDF, text)"
        )
    if b"\x00" in res.body[:1024]:
        raise ScrapeError(UNSUPPORTED, "response looks like binary data, not text")
    raw = decode_html(res)
    if ct.startswith("text/plain") or ct == "application/json":
        text = raw.strip()
        return _cut(text, max_chars), "text"
    return extract_html(raw, max_chars), "html"


async def extract_async(res: Fetched, max_chars: int) -> tuple[str, str]:
    """extract() off the event loop with a time budget.

    Threads cannot be cancelled, so a runaway parse is abandoned rather than
    killed; the byte and page caps keep that bounded.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(extract, res, max_chars), timeout=EXTRACT_TIMEOUT_S
        )
    except TimeoutError as e:
        raise ScrapeError(TIMEOUT, f"content extraction exceeded {EXTRACT_TIMEOUT_S}s") from e
