"""Tests for scrape_url: PDF path, TLS/AIA retry, error codes, HTML tables.

No live network: the transport layer (`_scrape._stream_get`) and the AIA
helpers are mocked, and PDF/HTML bodies come from tests/fixtures/.
"""

import asyncio
import datetime
import ssl
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import AuthorityInformationAccessOID, NameOID
import importlib

from connectors.web_search import _scrape
from connectors.web_search._scrape import Fetched, ScrapeError

search_tools = importlib.import_module("connectors.web_search.server")

FIX = Path(__file__).parent / "fixtures"
URL = "https://example.test/doc"


def _run(coro):
    return asyncio.run(coro)


def _fetched(body: bytes, ctype: str = "text/html", status: int = 200) -> Fetched:
    return Fetched(URL, status, ctype, body)


def _scrape_with(fetched_or_exc, **kw):
    """Run scrape_url with the transport mocked to return/raise one thing."""
    mock = AsyncMock(side_effect=fetched_or_exc if isinstance(fetched_or_exc, list) else None)
    if not isinstance(fetched_or_exc, list):
        if isinstance(fetched_or_exc, BaseException):
            mock.side_effect = fetched_or_exc
        else:
            mock.return_value = fetched_or_exc
    with patch.object(_scrape, "_stream_get", mock):
        return _run(search_tools.scrape_url({}, URL, **kw)), mock


DNS = {
    "example.test": "93.184.216.34",
    "leaf.test": "93.184.216.35",
    "ca.test": "93.184.216.36",
    "rebind.test": "10.1.2.3",
    "api.internal.test": "10.0.0.7",
    "mapped.test": "::ffff:127.0.0.1",
    "metadata.test": "169.254.169.254",
}


def _fake_getaddrinfo(host, port, *a, **kw):
    if host not in DNS:
        raise OSError(f"no such host {host}")
    return [(0, 0, 0, "", (DNS[host], port))]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    _scrape._host_ctx_cache.clear()
    _scrape._intermediate_cache.clear()
    monkeypatch.setattr(search_tools, "CRAWL4AI_URL", "")
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(_scrape.socket, "getaddrinfo", _fake_getaddrinfo)


# ---- PDF -----------------------------------------------------------------


def test_pdf_by_content_type():
    res, _ = _scrape_with(_fetched((FIX / "sample.pdf").read_bytes(), "application/pdf"))
    assert res["content_type"] == "pdf"
    assert "[Page 1]" in res["content"] and "[Page 2]" in res["content"]
    assert "Overall inspection rating: Good" in res["content"]
    assert res["char_count"] == len(res["content"])


def test_pdf_detected_by_magic_when_header_is_wrong():
    # e.g. KHDA serves "...pdf.aspx" with a generic content type.
    res, _ = _scrape_with(_fetched((FIX / "sample.pdf").read_bytes(), "application/octet-stream"))
    assert res["content_type"] == "pdf"
    assert "Wellbeing rating Very good" in res["content"]


def test_pdf_max_chars_truncates_with_note():
    res, _ = _scrape_with(
        _fetched((FIX / "sample.pdf").read_bytes(), "application/pdf"), max_chars=40
    )
    assert "Truncated" in res["content"]
    assert "Wellbeing" not in res["content"]


def test_pdf_scanned_has_clear_error():
    res, _ = _scrape_with(_fetched((FIX / "scanned.pdf").read_bytes(), "application/pdf"))
    assert res["error_code"] == "pdf_no_text"
    assert "scanned" in res["error"]


def test_pdf_encrypted_has_clear_error():
    res, _ = _scrape_with(_fetched((FIX / "encrypted.pdf").read_bytes(), "application/pdf"))
    assert res["error_code"] == "pdf_no_text"
    assert "password" in res["error"]


def test_pdf_garbage_is_pdf_no_text():
    res, _ = _scrape_with(_fetched(b"%PDF-1.4 not really", "application/pdf"))
    assert res["error_code"] == "pdf_parse_error"


def test_pdf_parser_crash_of_any_type_is_pdf_parse_error():
    boom = RecursionError("deep")
    with patch("pypdf.PdfReader", side_effect=boom):
        with pytest.raises(ScrapeError) as ei:
            _scrape.extract_pdf(b"%PDF-1.4", 100)
    assert ei.value.code == "pdf_parse_error"


def test_extraction_runs_off_loop_with_timeout(monkeypatch):
    import time

    monkeypatch.setattr(_scrape, "EXTRACT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(_scrape, "extract", lambda r, m: time.sleep(0.5) or ("x", "html"))
    with pytest.raises(ScrapeError) as ei:
        _run(_scrape.extract_async(_fetched(b"<p>x</p>"), 100))
    assert ei.value.code == "timeout"


def test_pdf_max_pages_cap():
    text = _scrape.extract_pdf((FIX / "sample.pdf").read_bytes(), 8000, max_pages=1)
    assert "[Page 1]" in text and "[Page 2]" not in text


def test_download_cap_aborts_stream(monkeypatch):
    monkeypatch.setattr(_scrape, "MAX_DOWNLOAD_BYTES", 10)

    def handler(request):
        return httpx.Response(200, content=b"x" * 100, headers={"content-type": "application/pdf"})

    real = httpx.AsyncClient

    class Client(real):
        def __init__(self, **kw):
            super().__init__(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(_scrape.httpx, "AsyncClient", Client)
    with pytest.raises(ScrapeError) as ei:
        _run(_scrape._stream_get(URL, ssl.create_default_context()))
    assert ei.value.code == "too_large"


# ---- error codes ---------------------------------------------------------


def test_http_403():
    res, _ = _scrape_with(_fetched(b"", status=403))
    assert res["error_code"] == "http_403"


def test_http_404_code():
    res, _ = _scrape_with(_fetched(b"", status=404))
    assert res["error_code"] == "http_404"


def test_unsupported_content_type():
    res, _ = _scrape_with(_fetched(b"PK\x03\x04", "application/zip"))
    assert res["error_code"] == "unsupported_content_type"


def test_empty_extraction():
    res, _ = _scrape_with(_fetched(b"<html><body><script>1</script></body></html>"))
    assert res["error_code"] == "empty_extraction"
    assert res["error"].startswith("Failed to scrape URL")


def test_timeout_after_one_retry():
    res, mock = _scrape_with(httpx.ReadTimeout("slow"))
    assert res["error_code"] == "timeout"
    assert mock.call_count == 2


def test_transient_503_retried_once_then_succeeds():
    ok = _fetched((FIX / "ratings_page.html").read_bytes())
    res, mock = _scrape_with([_fetched(b"", status=503), ok])
    assert mock.call_count == 2
    assert "Example School" in res["content"]


def test_connect_error_is_fetch_failed():
    res, mock = _scrape_with(httpx.ConnectError("refused"))
    assert res["error_code"] == "fetch_failed"
    assert mock.call_count == 2


def test_success_shape_is_backward_compatible():
    res, _ = _scrape_with(_fetched((FIX / "ratings_page.html").read_bytes()))
    assert {"url", "content", "char_count"} <= set(res)
    assert res["url"] == URL


# ---- TLS / AIA -----------------------------------------------------------


def _verify_error(code: int = 20) -> httpx.ConnectError:
    cause = ssl.SSLCertVerificationError(code, "unable to get local issuer certificate")
    cause.verify_code = code
    err = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]")
    err.__cause__ = cause
    return err


def test_aia_retry_succeeds_with_verified_context():
    ok = _fetched((FIX / "ratings_page.html").read_bytes())
    aia_ctx = ssl.create_default_context()
    build = AsyncMock(return_value=aia_ctx)
    mock = AsyncMock(side_effect=[_verify_error(), ok])
    with (
        patch.object(_scrape, "_stream_get", mock),
        patch.object(_scrape, "build_aia_context", build),
    ):
        res = _run(search_tools.scrape_url({}, URL))
    assert "Example School" in res["content"]
    build.assert_awaited_once()
    first_ctx, second_ctx = (c.args[1] for c in mock.call_args_list)
    assert second_ctx is aia_ctx
    for ctx in (first_ctx, second_ctx):
        assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def test_aia_failure_gives_tls_error_and_never_disables_verification():
    mock = AsyncMock(side_effect=_verify_error())
    build = AsyncMock(side_effect=ScrapeError("tls_error", "no AIA"))
    with (
        patch.object(_scrape, "_stream_get", mock),
        patch.object(_scrape, "build_aia_context", build),
    ):
        res = _run(search_tools.scrape_url({}, URL))
    assert res["error_code"] == "tls_error"
    assert all(c.args[1].verify_mode == ssl.CERT_REQUIRED for c in mock.call_args_list)


def test_other_tls_errors_do_not_trigger_aia():
    cause = ssl.SSLCertVerificationError(10, "certificate has expired")
    cause.verify_code = 10
    err = httpx.ConnectError("expired")
    err.__cause__ = cause
    build = AsyncMock()
    with (
        patch.object(_scrape, "_stream_get", AsyncMock(side_effect=err)),
        patch.object(_scrape, "build_aia_context", build),
    ):
        res = _run(search_tools.scrape_url({}, URL))
    assert res["error_code"] == "tls_error"
    build.assert_not_called()


def _make_chain():
    now = datetime.datetime.now(datetime.timezone.utc)

    def name(cn):
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])

    def build(subject, issuer, pub, signer, ca, aia=None):
        b = (
            x509.CertificateBuilder()
            .subject_name(name(subject))
            .issuer_name(name(issuer))
            .public_key(pub)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        )
        b = b.add_extension(
            x509.SubjectKeyIdentifier.from_public_key(pub), critical=False
        ).add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()),
            critical=False,
        )
        if ca:
            b = b.add_extension(
                x509.KeyUsage(False, False, False, False, False, True, True, False, False),
                critical=True,
            )
        if not ca:
            b = b.add_extension(
                x509.SubjectAlternativeName([x509.DNSName(subject)]), critical=False
            )
        if aia:
            b = b.add_extension(
                x509.AuthorityInformationAccess(
                    [
                        x509.AccessDescription(
                            AuthorityInformationAccessOID.CA_ISSUERS,
                            x509.UniformResourceIdentifier(aia),
                        )
                    ]
                ),
                critical=False,
            )
        return b.sign(signer, hashes.SHA256())

    root_k, int_k, leaf_k = (ec.generate_private_key(ec.SECP256R1()) for _ in range(3))
    root = build("Root", "Root", root_k.public_key(), root_k, True)
    inter = build("Inter", "Root", int_k.public_key(), root_k, True)
    leaf = build(
        "leaf.test", "Inter", leaf_k.public_key(), int_k, False, "http://ca.test/inter.crt"
    )
    return root, inter, leaf


def _build_with(chain, host="leaf.test", store_roots=None, download=None):
    root, inter, leaf = chain
    der = leaf.public_bytes(serialization.Encoding.DER)
    from cryptography.x509.verification import Store

    roots = [root] if store_roots is None else store_roots
    dl = download or AsyncMock(return_value=inter.public_bytes(serialization.Encoding.PEM))
    with (
        patch.object(_scrape, "_peer_leaf_der", AsyncMock(return_value=der)),
        patch.object(_scrape, "_download_issuer", dl),
        patch.object(_scrape, "_certifi_store", lambda: Store(roots)),
    ):
        return _run(_scrape.build_aia_context(f"https://{host}/")), dl


def test_build_aia_context_validates_then_trusts_issuer_and_caches():
    chain = _make_chain()
    assert _scrape._ca_issuer_urls(chain[2]) == ["http://ca.test/inter.crt"]
    ctx, dl = _build_with(chain)
    again = _run(_scrape.build_aia_context("https://leaf.test/x"))
    assert ctx is again  # per-host cache
    dl.assert_awaited_once_with("http://ca.test/inter.crt")
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert "Inter" in {tuple(x["subject"][0])[0][1] for x in ctx.get_ca_certs()}


def test_aia_chain_rooted_outside_trusted_roots_is_rejected():
    other_root = _make_chain()[0]  # an unrelated root, i.e. "certifi" lacks the real one
    with pytest.raises(ScrapeError) as ei:
        _build_with(_make_chain(), store_roots=[other_root])
    assert ei.value.code == "tls_error"
    assert not _scrape._host_ctx_cache


def test_aia_wrong_hostname_is_rejected():
    with pytest.raises(ScrapeError) as ei:
        _build_with(_make_chain(), host="other.test")
    assert ei.value.code == "tls_error"


def test_aia_self_signed_issuer_is_rejected():
    root, _, leaf = _make_chain()
    rogue_pem = root.public_bytes(serialization.Encoding.PEM)  # self-signed, not the leaf's issuer
    with pytest.raises(ScrapeError) as ei:
        _build_with(
            (root, root, leaf),
            store_roots=[_make_chain()[0]],
            download=AsyncMock(return_value=rogue_pem),
        )
    assert ei.value.code == "tls_error"


def test_build_aia_context_without_aia_is_tls_error():
    root, _, _ = _make_chain()
    der = root.public_bytes(serialization.Encoding.DER)
    with patch.object(_scrape, "_peer_leaf_der", AsyncMock(return_value=der)):
        with pytest.raises(ScrapeError) as ei:
            _run(_scrape.build_aia_context("https://leaf.test/"))
    assert ei.value.code == "tls_error"


def test_load_certs_handles_der_and_pem():
    _, inter, _ = _make_chain()
    assert _scrape._load_certs(inter.public_bytes(serialization.Encoding.DER))[0] == inter
    assert _scrape._load_certs(inter.public_bytes(serialization.Encoding.PEM))[0] == inter


def test_aia_download_refuses_private_hosts():
    for u in ("http://127.0.0.1/ca.crt", "http://rebind.test/ca.crt"):
        with pytest.raises(ScrapeError) as ei:
            _run(_scrape._download_issuer(u))
        assert ei.value.code == "tls_error"


# ---- SSRF ----------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.test/x",
        "gopher://example.test/",
        "http://127.0.0.1:8000/",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://100.64.0.1/",
        "http://[::1]/",
        "http://api:8000/api/v1",
        "http://vault:8200/v1/sys/health",
        "http://api.internal.test/",
        "http://rebind.test/",
        "http://metadata.test/",
        "http://mapped.test/",
        "http://0.0.0.0/",
    ],
)
def test_ssrf_blocked_before_any_request(url):
    mock = AsyncMock()
    with patch.object(_scrape.httpx, "AsyncClient", mock):
        res = _run(search_tools.scrape_url({}, url))
    assert res["error_code"] == "blocked_url"
    mock.assert_not_called()


def test_blocked_url_never_falls_back_to_crawl4ai(monkeypatch):
    monkeypatch.setattr(search_tools, "CRAWL4AI_URL", "http://crawl4ai:11235")
    fb = AsyncMock(return_value="x" * 200)
    with patch.object(search_tools, "_fetch_via_crawl4ai", fb):
        res = _run(search_tools.scrape_url({}, "http://169.254.169.254/"))
    assert res["error_code"] == "blocked_url"
    fb.assert_not_called()


def _mock_client(monkeypatch, handler):
    real = httpx.AsyncClient
    seen = []

    class Client(real):
        def __init__(self, **kw):
            kw.pop("transport", None)
            super().__init__(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(_scrape.httpx, "AsyncClient", Client)
    return seen


def test_redirect_to_internal_host_is_blocked(monkeypatch):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"})

    _mock_client(monkeypatch, handler)
    with pytest.raises(ScrapeError) as ei:
        _run(_scrape._stream_get("https://example.test/a", ssl.create_default_context()))
    assert ei.value.code == "blocked_url"
    assert len(calls) == 1


def test_redirect_to_non_http_scheme_is_blocked(monkeypatch):
    _mock_client(
        monkeypatch, lambda r: httpx.Response(302, headers={"location": "file:///etc/passwd"})
    )
    with pytest.raises(ScrapeError) as ei:
        _run(_scrape._stream_get("https://example.test/a", ssl.create_default_context()))
    assert ei.value.code == "blocked_url"


def test_relative_redirects_followed_and_connection_pinned(monkeypatch):
    seen = []

    def handler(request):
        seen.append(
            (str(request.url), request.headers["host"], request.extensions.get("sni_hostname"))
        )
        if request.url.path == "/a":
            return httpx.Response(302, headers={"location": "/b?x=1"})
        return httpx.Response(200, content=b"<p>ok</p>", headers={"content-type": "text/html"})

    _mock_client(monkeypatch, handler)
    res = _run(_scrape._stream_get("https://example.test/a", ssl.create_default_context()))
    assert res.final_url == "https://example.test/b?x=1"
    # Connects to the vetted IP, but Host and SNI stay the original name.
    assert seen[0] == ("https://93.184.216.34/a", "example.test", "example.test")
    assert seen[1][0] == "https://93.184.216.34/b?x=1"


def test_redirect_loop_capped(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(302, headers={"location": "/again"})

    _mock_client(monkeypatch, handler)
    with pytest.raises(ScrapeError) as ei:
        _run(_scrape._stream_get("https://example.test/a", ssl.create_default_context()))
    assert ei.value.code == "fetch_failed" and "redirects" in ei.value.message
    assert len(calls) == _scrape.MAX_REDIRECTS + 1


# ---- headers -------------------------------------------------------------


def test_request_headers_identify_the_connector():
    h = _scrape.request_headers()
    assert h["User-Agent"].startswith("Mozilla/5.0 (compatible; magoneai-web-search")
    assert "Chrome" not in h["User-Agent"]
    assert "text/html" in h["Accept"] and "application/pdf" in h["Accept"]
    assert "Accept-Language" in h


# ---- HTML tables ---------------------------------------------------------


def test_html_tables_retained_as_markdown():
    out = _scrape.extract_html((FIX / "ratings_page.html").read_text(), 8000)
    assert "| Teaching for effective learning | Very good | Good |" in out
    assert "| Assessment | Acceptable | Outstanding |" in out
    assert "AED 42,475" in out


def test_dom_walker_keeps_badges_and_tables_drops_boilerplate():
    out = _scrape.html_to_markdown((FIX / "ratings_page.html").read_text())
    assert "Good Overall Rating 2023-2024" in out
    assert "| Teaching for effective learning | Very good | Good |" in out
    assert "|---|---|---|" in out
    assert "Copyright footer" not in out and "var x" not in out and "Home" not in out
    assert "favorite_border" not in out


def test_walker_replaces_extractor_output_when_page_is_dominated_by_dropped_tables():
    # Simulates trafilatura discarding the tables (as it does on the KHDA page).
    extra = "<table><tr><th>X</th><th>Y</th></tr><tr><td>p1</td><td>p2</td></tr><tr><td>p3</td><td>p4</td></tr></table>"
    html = (FIX / "ratings_page.html").read_text().replace("</main>", extra + "</main>")
    with patch.object(
        _scrape.trafilatura, "extract", return_value="# Example School\n\nIntro text."
    ):
        out = _scrape.extract_html(html, 8000)
    assert "| Assessment | Acceptable | Outstanding |" in out
    assert "Overall Rating 2023-2024" in out


# ---- crawl4ai ------------------------------------------------------------


def test_crawl4ai_unset_makes_no_http_call():
    with patch("httpx.AsyncClient") as client:
        assert _run(search_tools._fetch_via_crawl4ai(URL, 100)) == ""
        rich = _run(search_tools._fetch_via_crawl4ai_rich(URL))
    client.assert_not_called()
    assert rich["success"] is False


def test_crawl4ai_fallback_used_when_configured(monkeypatch):
    monkeypatch.setattr(search_tools, "CRAWL4AI_URL", "http://crawl4ai:11235")
    shell = _fetched(b"<html><body><div id='app'></div></body></html>")
    fb = AsyncMock(return_value="rendered content " * 10)
    with (
        patch.object(_scrape, "_stream_get", AsyncMock(return_value=shell)),
        patch.object(search_tools, "_fetch_via_crawl4ai", fb),
    ):
        res = _run(search_tools.scrape_url({}, URL))
    assert res["content"].startswith("rendered content")
    fb.assert_awaited_once()


# ---- table recovery regression ------------------------------------------

ARTICLE = """<html><body><nav><a href="/">Home</a></nav><article><h1>Quarterly update</h1>
<p>The team shipped the new reporting module this quarter and customers reported faster dashboards.
It was a long effort across several groups, with careful testing at every stage of the rollout.</p>
<p>Next quarter the focus moves to reliability work and a clearer onboarding flow for new accounts.
We also plan to simplify the settings area and remove several rarely used options.</p></article>
%s<footer>Footer text</footer></body></html>"""

COOKIE_TABLE = (
    '<div id="cookie-banner"><table><tr><th>Cookie</th><th>Purpose</th></tr>'
    "<tr><td>_ga</td><td>analytics</td></tr><tr><td>sid</td><td>session</td></tr></table></div>"
)
LAYOUT_TABLE = (
    "<table><tr><td>Left column text here</td><td>Right column text here</td></tr></table>"
)


def _baseline(html):
    return _scrape._cut(
        _scrape.trafilatura.extract(
            html,
            include_links=True,
            include_tables=True,
            output_format="markdown",
        )
        or "",
        8000,
    )


@pytest.mark.parametrize("extra", ["", COOKIE_TABLE, LAYOUT_TABLE])
def test_ordinary_page_output_is_unchanged_trafilatura(extra):
    html = ARTICLE % extra
    with patch.object(_scrape, "html_to_markdown", side_effect=AssertionError("walker used")):
        assert _scrape.extract_html(html, 8000) == _baseline(html)


def test_single_dropped_data_table_is_appended_not_replacing():
    html = (
        ARTICLE
        % "<h2>Results</h2><table><tr><th>A</th><th>B</th></tr><tr><td>one</td><td>two</td></tr><tr><td>three</td><td>four</td></tr></table>"
    )
    base = _baseline(html)
    with patch.object(
        _scrape.trafilatura, "extract", return_value=base.split("Results")[0].strip()
    ):
        out = _scrape.extract_html(html, 8000)
    assert out.startswith(base.split("Results")[0].strip())
    assert "## Results" in out and "| three | four |" in out
    assert "Footer text" not in out


# ---- round 2: hop AIA, error wrapping, cache, resolver -------------------


def test_aia_is_built_for_the_failing_redirect_hop(monkeypatch):
    # scrape_url validates the URL before fetching; keep that lookup offline.
    from magone_mcp_core import net

    monkeypatch.setattr(
        net.socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(2, 1, 6, "", ("93.184.216.34", port or 443))],
    )
    err = _verify_error()
    err.hop_url = "https://incomplete.test:8443/x"
    ok = _fetched(b"<p>" + b"hello world " * 10 + b"</p>")
    build = AsyncMock(return_value=ssl.create_default_context())
    mock = AsyncMock(side_effect=[err, ok])
    with (
        patch.object(_scrape, "_stream_get", mock),
        patch.object(_scrape, "build_aia_context", build),
    ):
        _run(search_tools.scrape_url({}, "https://redirector.test/r"))
    build.assert_awaited_once_with("https://incomplete.test:8443/x")


@pytest.mark.parametrize(
    "exc,code",
    [
        (ConnectionResetError("reset"), "fetch_failed"),
        (ssl.SSLError("bad"), "tls_error"),
        (IndexError("empty"), "tls_error"),
        (ValueError("malformed"), "tls_error"),
        (TimeoutError(), "fetch_failed"),
    ],
)
def test_aia_internal_errors_become_scrape_errors(exc, code):
    with patch.object(_scrape, "_peer_leaf_der", AsyncMock(side_effect=exc)):
        with pytest.raises(ScrapeError) as ei:
            _run(_scrape.build_aia_context("https://leaf.test/"))
    assert ei.value.code == code


def test_empty_pkcs7_is_value_error():
    from cryptography.hazmat.primitives.serialization import pkcs7

    with patch.object(pkcs7, "load_der_pkcs7_certificates", return_value=[]):
        with pytest.raises(ValueError):
            _scrape._load_certs(b"\x30\x00")


def test_cached_aia_context_used_first_and_stale_one_evicted():
    ok = _fetched(b"<p>" + b"hello world " * 10 + b"</p>")
    cached = ssl.create_default_context()
    _scrape._host_ctx_cache["example.test:443"] = cached
    mock = AsyncMock(return_value=ok)
    with patch.object(_scrape, "_stream_get", mock):
        _run(_scrape._get_once(URL))
    assert mock.call_args_list[0].args[1] is cached and mock.call_count == 1

    tls = httpx.ConnectError("x")
    tls.__cause__ = ssl.SSLError("stale")
    build = AsyncMock(return_value=ssl.create_default_context())
    mock = AsyncMock(side_effect=[tls, _verify_error(), ok])
    with (
        patch.object(_scrape, "_stream_get", mock),
        patch.object(_scrape, "build_aia_context", build),
    ):
        _run(_scrape._get_once(URL))
    assert "example.test:443" not in _scrape._host_ctx_cache
    build.assert_awaited_once()


@pytest.mark.parametrize("ip", ["64:ff9b::7f00:1", "2002:7f00:1::1"])
def test_nat64_and_6to4_blocked(ip):
    with pytest.raises(ScrapeError) as ei:
        _scrape._resolve_public(ip, 80)
    assert ei.value.code == "blocked_url"


def test_resolver_returns_all_ips_and_connect_falls_back(monkeypatch):
    DNS["multi.test"] = "93.184.216.40"
    monkeypatch.setattr(
        _scrape.socket,
        "getaddrinfo",
        lambda h, p, *a, **k: [
            (0, 0, 0, "", ("93.184.216.40", p)),
            (0, 0, 0, "", ("93.184.216.41", p)),
        ],
    )
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "93.184.216.40":
            raise httpx.ConnectError("unreachable")
        return httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})

    _mock_client(monkeypatch, handler)
    res = _run(_scrape._stream_get("http://multi.test/", ssl.create_default_context()))
    assert seen == ["93.184.216.40", "93.184.216.41"] and res.status == 200


def test_pdf_truncation_note_stays_within_max_chars():
    out = _scrape.extract_pdf((FIX / "sample.pdf").read_bytes(), 60)
    assert len(out) <= 60 and "Truncated" in out


def test_file_listing_table_is_not_a_data_table():
    from lxml import html as lxml_html

    t = lxml_html.fromstring(
        "<table>"
        + "<tr><td></td><td><a href='/a'>a.py</a></td><td></td><td></td></tr>" * 6
        + "</table>"
    )
    assert not _scrape._is_data_table(t)


def test_ip_literal_host_validates_with_ip_san():
    root, inter, leaf = _make_chain()
    from cryptography.x509.verification import Store

    with patch.object(_scrape, "_certifi_store", lambda: Store([root])):
        with pytest.raises(ScrapeError):  # leaf has a DNS SAN only, so IP subject must not match
            _scrape._validate_chain(leaf, [inter], "93.184.216.34")


# ---- Review fixes: charset, odd responses, extraction pool ----------------


def test_header_charset_is_used_when_the_page_has_no_meta(monkeypatch):
    body = "café au lait, crème brûlée. ".encode("iso-8859-1") * 5

    def handler(request):
        return httpx.Response(
            200, content=body, headers={"content-type": "text/plain; charset=iso-8859-1"}
        )

    _mock_client(monkeypatch, handler)
    res = _run(search_tools.scrape_url({}, URL))
    assert "café au lait" in res["content"] and "�" not in res["content"]


def test_decode_html_prefers_the_header_charset_over_utf8():
    html = "<html><body><p>café</p></body></html>".encode("iso-8859-1")
    assert "café" in _scrape.decode_html(Fetched(URL, 200, "text/html", html, "iso-8859-1"))
    # No header charset: <meta> still decides.
    meta = b'<meta charset="iso-8859-1"><p>caf\xe9</p>'
    assert "café" in _scrape.decode_html(Fetched(URL, 200, "text/html", meta))


def test_redirect_to_an_out_of_range_port_is_an_error_code(monkeypatch):
    def handler(request):
        return httpx.Response(302, headers={"location": "http://example.com:99999/"})

    _mock_client(monkeypatch, handler)
    res = _run(search_tools.scrape_url({}, URL))
    assert res["error_code"] == "fetch_failed" and "port" in res["error"]


def test_body_that_does_not_match_its_content_encoding_is_an_error_code(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            content=b"this is not gzip data at all",
            headers={"content-type": "text/html", "content-encoding": "gzip"},
        )

    _mock_client(monkeypatch, handler)
    res = _run(search_tools.scrape_url({}, URL))
    assert res["error_code"] == "fetch_failed"


def test_extraction_runs_on_its_own_pool():
    import threading

    seen = []

    def spy(res, max_chars):
        seen.append(threading.current_thread().name)
        return "x", "html"

    with patch.object(_scrape, "extract", spy):
        _run(_scrape.extract_async(_fetched(b"<p>x</p>"), 100))
    assert seen and seen[0].startswith("web-search-extract")


def test_abandoned_extractions_do_not_hold_up_dns(monkeypatch):
    # Far more stuck parses than the loop's default executor has threads.
    import threading
    import time

    release = threading.Event()
    monkeypatch.setattr(_scrape, "EXTRACT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(_scrape, "extract", lambda r, m: release.wait(5) and ("x", "html"))

    async def scenario():
        stuck = [_scrape.extract_async(_fetched(b"<p>x</p>"), 100) for _ in range(40)]
        results = await asyncio.gather(*stuck, return_exceptions=True)
        assert all(isinstance(r, ScrapeError) and r.code == "timeout" for r in results)
        t0 = time.monotonic()
        ips = await _scrape._resolve("example.test", 443)
        return ips, time.monotonic() - t0

    try:
        ips, took = _run(scenario())
    finally:
        release.set()
    assert ips == ["93.184.216.34"] and took < 1.0
