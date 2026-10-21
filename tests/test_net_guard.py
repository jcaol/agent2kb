"""Tests for the SSRF guard (agent2kb.net_guard) and its callers. No real network, no real DNS."""

import http.server
import socket
import threading
from unittest.mock import patch

import httpx
import pytest

from agent2kb import net_guard as ng
from agent2kb.ingest.handlers import url as url_handler

PUBLIC = "93.184.216.34"


def _resolver(mapping, calls=None):
    """Stand-in for ng.resolve_public: name -> list of addresses (or an exception)."""

    def fake(host, port):
        if calls is not None:
            calls.append(host)
        answer = mapping[host]
        if isinstance(answer, Exception):
            raise answer
        import ipaddress

        if not all(ng.is_public_ip(ipaddress.ip_address(a)) for a in answer):
            raise ng.BlockedURLError(f"Blocked URL: '{host}' resolves to non-public address")
        return list(answer)

    return fake


class _Recorder:
    """httpx transport that records requests and replies from a script."""

    def __init__(self, handler):
        self.requests = []
        self.transport = httpx.MockTransport(self._wrap(handler))

    def _wrap(self, handler):
        def inner(request):
            self.requests.append(request)
            return handler(request)

        return inner


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1",
        "0.0.0.0", "224.0.0.1", "255.255.255.255", "192.0.0.1",
        "::1", "::", "fc00::1", "fe80::1", "ff02::1",
        "::ffff:127.0.0.1", "::ffff:10.0.0.1", "2002:7f00:1::", "64:ff9b::7f00:1",
    ],
)
def test_non_public_addresses_rejected(addr):
    import ipaddress

    assert not ng.is_public_ip(ipaddress.ip_address(addr))


@pytest.mark.parametrize("addr", [PUBLIC, "8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_public_addresses_accepted(addr):
    import ipaddress

    assert ng.is_public_ip(ipaddress.ip_address(addr))


class TestResolvePublic:
    def _gai(self, *addrs):
        def fake(host, port, *a, **k):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (x, port)) for x in addrs]

        return fake

    def test_fails_closed_when_resolution_fails(self):
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("nope")):
            with pytest.raises(ValueError, match="cannot resolve"):
                ng.resolve_public("nx.example", 80)

    def test_fails_closed_on_empty_answer(self):
        with patch("socket.getaddrinfo", self._gai()):
            with pytest.raises(ValueError, match="no addresses"):
                ng.resolve_public("empty.example", 80)

    def test_one_private_address_poisons_the_answer(self):
        with patch("socket.getaddrinfo", self._gai(PUBLIC, "10.0.0.5")):
            with pytest.raises(ValueError, match="non-public"):
                ng.resolve_public("mixed.example", 80)

    def test_public_answer_returned_deduplicated(self):
        with patch("socket.getaddrinfo", self._gai(PUBLIC, PUBLIC)):
            assert ng.resolve_public("ok.example", 80) == [PUBLIC]


class TestValidateUrl:
    @pytest.fixture(autouse=True)
    def _dns(self, monkeypatch):
        monkeypatch.setattr(ng, "resolve_public", _resolver({"ok.example": [PUBLIC]}))

    def test_public_url_passes(self):
        ng.validate_url("https://ok.example/a?b=1")

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://ok.example/x", "file:///etc/passwd", "javascript:alert(1)", "//ok.example/x",
            "http://", "http://localhost/x", "http://a.localhost/x", "http://127.0.0.1/",
            "http://[::1]/", "http://metadata.google.internal/", "http://169.254.169.254/x",
            "http://user:pw@ok.example/", "https://youtube.com.evil.example/", "http://ok.example:99999/",
        ],
    )
    def test_rejected(self, url):
        with pytest.raises(ValueError):
            ng.validate_url(url)

    @pytest.mark.parametrize(
        "url", ["http://0010.8.8.8/", "http://134744072/", "http://0x8.8.8.8/", "http://8.8/"]
    )
    def test_non_canonical_numeric_hosts_are_refused_before_resolving(self, url, monkeypatch):
        # Resolvers disagree on these forms (macOS reads 0010 as decimal, glibc as octal), so
        # they are refused outright, even when they would point at a public address.
        def no_dns(*a, **k):
            raise AssertionError("resolved a host that should be refused statically")

        monkeypatch.setattr(ng.socket, "getaddrinfo", no_dns)
        with pytest.raises(ValueError, match="non-canonical numeric host"):
            ng.validate_url(url)

    @pytest.mark.parametrize(
        "url", ["http://2130706433/", "http://0x7f.0.0.1/", "http://0177.0.0.1/", "http://127.1/"]
    )
    def test_numeric_host_tricks_resolve_to_loopback_and_are_blocked(self, url, monkeypatch):
        monkeypatch.undo()  # real resolver: numeric forms need no network
        with pytest.raises(ValueError):
            ng.validate_url(url)

    def test_dns_failure_is_refused(self, monkeypatch):
        monkeypatch.setattr(
            ng,
            "resolve_public",
            _resolver({"gone.example": ValueError("Blocked URL: cannot resolve")}),
        )
        with pytest.raises(ValueError, match="cannot resolve"):
            ng.validate_url("https://gone.example/")

    def test_utils_validate_url_is_the_same_guard_and_fails_closed(self, monkeypatch):
        from agent2kb.utils import validate_url

        monkeypatch.undo()
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("nope")):
            with pytest.raises(ValueError, match="cannot resolve"):
                validate_url("https://nx.example/")

    def test_domain_allow_list(self, monkeypatch):
        monkeypatch.setattr(
            ng,
            "resolve_public",
            _resolver({"docs.ok.example": [PUBLIC], "other.example": [PUBLIC]}),
        )
        with ng.restrict_hosts(["ok.example"]):
            ng.validate_url("https://docs.ok.example/")
            with pytest.raises(ValueError, match="not in the allowed domains"):
                ng.validate_url("https://other.example/")
            with pytest.raises(ValueError):
                ng.validate_url("https://evilok.example/")  # suffix trick
        ng.validate_url("https://other.example/")  # restriction ends with the block


class TestSafeGet:
    @pytest.fixture
    def dns(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            ng,
            "resolve_public",
            _resolver(
                {
                    "site.example": [PUBLIC],
                    "cdn.example": ["93.184.216.35"],
                    "evil.example": ["10.0.0.7"],
                    "rebind.example": [PUBLIC],
                },
                calls,
            ),
        )
        return calls

    def test_connection_is_pinned_to_the_validated_ip(self, dns):
        rec = _Recorder(lambda r: httpx.Response(200, text="hello"))
        resp = ng.safe_get("https://site.example/page?q=1", transport=rec.transport)
        assert resp.text == "hello"
        (req,) = rec.requests
        assert req.url.host == PUBLIC  # not the name: no second DNS lookup can happen
        assert req.url.path == "/page" and req.url.query == b"q=1"
        assert req.headers["host"] == "site.example"
        assert req.extensions["sni_hostname"] == "site.example"  # TLS checks the real name

    def test_http_url_has_no_sni_and_keeps_port_in_host_header(self, dns):
        rec = _Recorder(lambda r: httpx.Response(200, text="x"))
        ng.safe_get("http://site.example:8080/", transport=rec.transport)
        (req,) = rec.requests
        assert req.url.port == 8080 and req.headers["host"] == "site.example:8080"
        assert "sni_hostname" not in req.extensions

    def test_rebinding_second_answer_never_used(self, monkeypatch):
        answers = iter([[PUBLIC], ["127.0.0.1"]])  # what an attacker's DNS would do
        monkeypatch.setattr(ng, "resolve_public", lambda host, port: next(answers))
        rec = _Recorder(lambda r: httpx.Response(200, text="x"))
        ng.safe_get("https://rebind.example/", transport=rec.transport)
        assert [r.url.host for r in rec.requests] == [PUBLIC]
        assert next(answers) == ["127.0.0.1"]  # only one lookup was made for the hop

    def test_public_ip_literal_is_not_rewritten(self, monkeypatch):
        monkeypatch.setattr(ng, "resolve_public", _resolver({PUBLIC: [PUBLIC]}))
        rec = _Recorder(lambda r: httpx.Response(200, text="x"))
        ng.safe_get(f"http://{PUBLIC}/", transport=rec.transport)
        assert rec.requests[0].url.host == PUBLIC

    def test_redirect_to_internal_ip_literal_is_blocked(self, dns):
        rec = _Recorder(
            lambda r: httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})
        )
        with pytest.raises(ValueError, match="Blocked"):
            ng.safe_get("https://site.example/", transport=rec.transport)
        assert len(rec.requests) == 1  # the metadata endpoint was never contacted

    def test_redirect_to_name_resolving_privately_is_blocked(self, dns):
        rec = _Recorder(lambda r: httpx.Response(301, headers={"location": "https://evil.example/"}))
        with pytest.raises(ValueError, match="non-public|Blocked"):
            ng.safe_get("https://site.example/", transport=rec.transport)
        assert len(rec.requests) == 1

    def test_redirect_to_non_http_scheme_is_blocked(self, dns):
        rec = _Recorder(lambda r: httpx.Response(302, headers={"location": "file:///etc/passwd"}))
        with pytest.raises(ValueError, match="scheme"):
            ng.safe_get("https://site.example/", transport=rec.transport)

    def test_each_hop_is_revalidated_and_relative_locations_work(self, dns):
        def handler(request):
            if request.headers["host"] == "site.example" and request.url.path == "/a":
                return httpx.Response(302, headers={"location": "/b"})
            if request.url.path == "/b" and request.headers["host"] == "site.example":
                return httpx.Response(302, headers={"location": "https://cdn.example/c"})
            return httpx.Response(200, text="done")

        rec = _Recorder(handler)
        resp = ng.safe_get("https://site.example/a", transport=rec.transport)
        assert resp.text == "done"
        assert resp.extensions["final_url"] == "https://cdn.example/c"
        assert [r.url.host for r in rec.requests] == [PUBLIC, PUBLIC, "93.184.216.35"]
        assert dns == ["site.example", "site.example", "cdn.example"]  # one lookup per hop

    def test_redirect_limit(self, dns):
        rec = _Recorder(lambda r: httpx.Response(302, headers={"location": "/loop"}))
        with pytest.raises(ValueError, match="Too many redirects"):
            ng.safe_get("https://site.example/loop", max_redirects=3, transport=rec.transport)
        assert len(rec.requests) == 4

    def test_redirect_without_location(self, dns):
        rec = _Recorder(lambda r: httpx.Response(302))
        with pytest.raises(ValueError, match="Location"):
            ng.safe_get("https://site.example/", transport=rec.transport)

    def test_http_errors_raise_unless_disabled(self, dns):
        rec = _Recorder(lambda r: httpx.Response(404, text="nope"))
        with pytest.raises(httpx.HTTPStatusError):
            ng.safe_get("https://site.example/", transport=rec.transport)
        assert ng.safe_get("https://site.example/", raise_for_status=False,
                           transport=rec.transport).status_code == 404

    def test_body_size_cap(self, dns, monkeypatch):
        monkeypatch.setattr(ng, "MAX_FETCH_BYTES", 1000)
        rec = _Recorder(lambda r: httpx.Response(200, content=b"x" * 5000))
        with pytest.raises(ValueError, match="too large"):
            ng.safe_get("https://site.example/", transport=rec.transport)

    def test_gzip_body_is_decoded_once(self, dns):
        import gzip

        rec = _Recorder(
            lambda r: httpx.Response(
                200, content=gzip.compress(b"plain text"), headers={"content-encoding": "gzip"}
            )
        )
        assert ng.safe_get("https://site.example/", transport=rec.transport).text == "plain text"

    def test_head_method_and_final_url(self, dns):
        rec = _Recorder(lambda r: httpx.Response(200))
        resp = ng.safe_get("https://site.example/s", method="HEAD", transport=rec.transport)
        assert rec.requests[0].method == "HEAD"
        assert resp.extensions["final_url"] == "https://site.example/s"

    def test_allow_list_applies_to_every_hop(self, dns):
        rec = _Recorder(lambda r: httpx.Response(302, headers={"location": "https://cdn.example/x"}))
        with ng.restrict_hosts(["site.example"]):
            with pytest.raises(ValueError, match="allowed domains"):
                ng.safe_get("https://site.example/", transport=rec.transport)
        assert len(rec.requests) == 1

    def test_allow_list_cannot_be_waived(self, dns):
        import inspect

        assert "enforce_allowlist" not in inspect.signature(ng.safe_get).parameters

    def test_proxy_detection(self):
        parsed = ng.urlsplit("https://site.example/")
        with patch("urllib.request.getproxies", return_value={"https": "http://proxy:3128"}), \
             patch("urllib.request.proxy_bypass", return_value=False):
            assert ng._proxied(parsed)
        with patch("urllib.request.getproxies", return_value={"https": "http://proxy:3128"}), \
             patch("urllib.request.proxy_bypass", return_value=True):
            assert not ng._proxied(parsed)
        with patch("urllib.request.getproxies", return_value={}):
            assert not ng._proxied(parsed)


class _Echo(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self):  # noqa: N802
        _Echo.seen.append(self.headers.get("Host"))
        body = b"pinned ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_real_socket_connects_to_pinned_ip_with_original_host_header(monkeypatch):
    """No transport stub: a real httpx connection goes to the pinned IP, Host stays the name."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _Echo)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # 'pinned.test' does not resolve anywhere; the stub says it is 127.0.0.1.
        monkeypatch.setattr(ng, "resolve_public", lambda host, p: ["127.0.0.1"])
        resp = ng.safe_get(f"http://pinned.test:{port}/x")
        assert resp.text == "pinned ok"
        assert _Echo.seen == [f"pinned.test:{port}"]
    finally:
        server.shutdown()


class TestCallers:
    def test_url_handler_fetch_uses_the_guard(self):
        with patch("agent2kb.net_guard.safe_get", return_value="RESP") as sg:
            assert url_handler._safe_get("https://x.example/", max_redirects=2) == "RESP"
        sg.assert_called_once_with("https://x.example/", max_redirects=2)

    def test_jina_reader_goes_through_guard(self):
        body = "Title: T\nMarkdown Content:\n" + "word " * 30
        resp = httpx.Response(200, text=body)
        with patch("agent2kb.net_guard.safe_get", return_value=resp) as sg:
            title, md = url_handler._try_jina_reader("https://x.example/a")
        assert title == "T" and md.startswith("word")
        assert sg.call_args.args[0] == "https://r.jina.ai/https://x.example/a"

    def test_jina_reader_is_never_called_while_an_allow_list_is_active(self):
        with patch("agent2kb.net_guard.safe_get") as sg, ng.restrict_hosts(["x.example"]):
            assert url_handler._try_jina_reader("https://x.example/a") == ("", "")
        sg.assert_not_called()

    def test_jina_reader_failure_is_silent(self):
        with patch("agent2kb.net_guard.safe_get", side_effect=ValueError("Blocked")):
            assert url_handler._try_jina_reader("https://x.example/a") == ("", "")

    def test_shortener_expansion_uses_guard_and_final_url(self):
        from agent2kb.utils import _expand_shortener

        resp = httpx.Response(200)
        resp.extensions["final_url"] = "https://real.example/page"
        with patch("agent2kb.net_guard.safe_get", return_value=resp) as sg:
            assert _expand_shortener("https://bit.ly/x") == "https://real.example/page"
        assert sg.call_args.kwargs["method"] == "HEAD"
        with patch("agent2kb.net_guard.safe_get", side_effect=ValueError("Blocked")):
            assert _expand_shortener("https://bit.ly/x") is None


class TestBlockedErrorClassification:
    """Guard refusals are BlockedURLError; ordinary failures are plain errors."""

    @pytest.fixture(autouse=True)
    def _dns(self, monkeypatch):
        monkeypatch.setattr(
            ng,
            "resolve_public",
            _resolver({"site.example": [PUBLIC], "10.0.0.1": ["10.0.0.1"]}),
        )

    @pytest.mark.parametrize(
        "url", ["ftp://site.example/", "http://localhost/", "http://127.0.0.1/",
                "http://u:p@site.example/", "https://youtube.com.evil.example/"],
    )
    def test_static_refusals(self, url):
        with pytest.raises(ng.BlockedURLError):
            ng.validate_url(url)

    def test_private_resolution_is_blocked(self, monkeypatch):
        monkeypatch.undo()
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.1", 80))]):
            with pytest.raises(ng.BlockedURLError):
                ng.validate_url("http://internal.example/")

    def test_allow_list_refusal_is_blocked(self):
        with ng.restrict_hosts(["other.example"]):
            with pytest.raises(ng.BlockedURLError):
                ng.validate_url("https://site.example/")

    def test_redirect_hop_refusal_is_blocked(self):
        rec = _Recorder(lambda r: httpx.Response(302, headers={"location": "http://10.0.0.1/"}))
        with pytest.raises(ng.BlockedURLError):
            ng.safe_get("https://site.example/", transport=rec.transport)

    def test_ordinary_failures_are_not_blocked_errors(self, monkeypatch):
        rec = _Recorder(lambda r: httpx.Response(302, headers={"location": "/again"}))
        with pytest.raises(ValueError) as e:
            ng.safe_get("https://site.example/", max_redirects=1, transport=rec.transport)
        assert not isinstance(e.value, ng.BlockedURLError)
        monkeypatch.undo()
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("nope")):
            with pytest.raises(ValueError) as e:
                ng.resolve_public("nx.example", 80)
        assert not isinstance(e.value, ng.BlockedURLError)


class _FakeS3:
    def __init__(self):
        self.uploads = {}

    def upload(self, key, body, content_type="text/markdown"):
        self.uploads[key] = body


class TestUrlHandlerJinaPolicy:
    """Jina Reader is a third-party fetcher: only for ordinary failures, never under policy."""

    BODY = "Real article text. " * 20

    @pytest.fixture
    def handler(self, monkeypatch):
        from agent2kb.models import ProfileConfig

        profile = ProfileConfig(
            name="p", bucket="b", prefix="projects/p/", region="us-east-1",
            allow_jina_reader=True, privacy_mode=False,
        )
        monkeypatch.setattr(url_handler, "_validate_url", lambda u: None)
        self.s3 = _FakeS3()
        self.extract_calls = []

        def fake_extract(html, url, allow_cloud=True):
            self.extract_calls.append(allow_cloud)
            return "Title", self.BODY

        monkeypatch.setattr(url_handler, "_extract_content", fake_extract)
        return url_handler.URLHandler(profile=profile, s3=self.s3, tags=[])

    def _fetch_returns(self, monkeypatch):
        resp = httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"})
        monkeypatch.setattr(url_handler, "_safe_get", lambda u: resp)

    def test_default_uses_jina(self, handler, monkeypatch):
        self._fetch_returns(monkeypatch)
        assert handler.process("https://site.example/a").success
        assert self.extract_calls == [True]

    def test_active_allow_list_disables_jina_even_when_profile_allows_it(
        self, handler, monkeypatch
    ):
        self._fetch_returns(monkeypatch)
        with ng.restrict_hosts(["site.example"]):
            assert handler.process("https://site.example/a").success
        assert self.extract_calls == [False]

    def test_guard_refusal_never_falls_back_to_jina(self, handler, monkeypatch):
        def blocked(url):
            raise ng.BlockedURLError("Blocked URL: redirect to 10.0.0.1")

        monkeypatch.setattr(url_handler, "_safe_get", blocked)
        result = handler.process("https://site.example/a")
        assert not result.success and "Blocked URL" in result.error
        assert self.extract_calls == []  # extraction (and so Jina) never ran
        assert self.s3.uploads == {}

    def test_ordinary_fetch_failure_still_falls_back_to_jina(self, handler, monkeypatch):
        def flaky(url):
            raise httpx.ConnectError("boom")

        monkeypatch.setattr(url_handler, "_safe_get", flaky)
        assert handler.process("https://site.example/a").success
        assert self.extract_calls == [True]

    def test_too_many_redirects_is_ordinary(self, handler, monkeypatch):
        def loop(url):
            raise ValueError("Too many redirects (>5) for URL: x")

        monkeypatch.setattr(url_handler, "_safe_get", loop)
        assert handler.process("https://site.example/a").success
        assert self.extract_calls == [True]

    def test_blocked_start_url_is_refused_before_anything_else(self, handler, monkeypatch):
        def refuse(url):
            raise ng.BlockedURLError("Blocked URL: 'localhost'")

        monkeypatch.setattr(url_handler, "_validate_url", refuse)
        result = handler.process("http://localhost/")
        assert not result.success and self.extract_calls == []


def test_full_width_digits_cannot_dodge_the_numeric_host_check():
    # NFKC turns these into 0177.0.0.1 / 127.0.0.1, which IDNA would then look up.
    for url in ("http://０１７７.0.0.1/", "http://１２７.0.0.1/"):
        with pytest.raises(ValueError, match="non-canonical numeric host"):
            ng._check_static(url)
