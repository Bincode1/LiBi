from __future__ import annotations

import asyncio
import socket

import httpx
import pytest

from paicli.web.fetch import NetworkPolicyError, PublicNetworkTransport, fetch_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://169.254.169.254/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://224.0.0.1/",
        "http://192.0.2.1/",
        "file:///etc/hosts",
        "http://user:placeholder@example.test/",
    ],
)
def test_fetch_rejects_non_public_destinations_before_connecting(monkeypatch, url):
    async def unexpected_request(_transport, _request):
        pytest.fail("a forbidden destination reached the network transport")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", unexpected_request)
    with pytest.raises(NetworkPolicyError):
        asyncio.run(fetch_url(url))


@pytest.mark.parametrize("addresses", [("127.0.0.1",), ("93.184.215.14", "10.0.0.1"), ()])
def test_fetch_rejects_private_or_empty_dns_answers(monkeypatch, addresses):
    def resolve(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80)) for ip in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with pytest.raises(NetworkPolicyError):
        asyncio.run(fetch_url("http://example.test/"))


def test_fetch_pins_public_dns_and_preserves_host_and_tls_name(monkeypatch):
    lookups = []
    requests = []

    def resolve(host, *_args, **_kwargs):
        lookups.append(host)
        # A second resolution would allow a rebinding attack.
        ip = "93.184.215.14" if len(lookups) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]

    async def respond(_transport, request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>Public</p>")

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    assert asyncio.run(fetch_url("https://example.test:8443/path?q=1")) == "Public"
    assert lookups == ["example.test"]
    assert requests[0].url == httpx.URL("https://93.184.215.14:8443/path?q=1")
    assert requests[0].headers["host"] == "example.test:8443"
    assert requests[0].extensions["sni_hostname"] == "example.test"


def test_fetch_validates_redirects_before_connecting(monkeypatch):
    visited = []

    async def redirect(_transport, request):
        visited.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", redirect)
    with pytest.raises(NetworkPolicyError):
        asyncio.run(fetch_url("https://93.184.215.14/"))
    assert visited == ["https://93.184.215.14/"]


def test_fetch_follows_public_relative_redirects(monkeypatch):
    visited = []

    async def respond(_transport, request):
        visited.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/end"})
        return httpx.Response(200, text="finished")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    assert asyncio.run(fetch_url("https://93.184.215.14/start")) == "finished"
    assert visited == ["https://93.184.215.14/start", "https://93.184.215.14/end"]


@pytest.mark.parametrize("all_fail", [False, True])
def test_fetch_tries_only_validated_addresses_with_bounded_backoff(monkeypatch, all_fail):
    attempts = []
    delays = []
    addresses = ["2606:4700:4700::1111", "93.184.215.14", "93.184.215.15", "93.184.215.16"]

    def resolve(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in addresses]

    async def respond(_transport, request):
        attempts.append(request.url.host)
        if all_fail or len(attempts) == 1:
            raise httpx.ConnectError("connection failed", request=request)
        return httpx.Response(200, text="connected")

    async def backoff(delay):
        delays.append(delay)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    monkeypatch.setattr("paicli.web.fetch.asyncio.sleep", backoff)
    if all_fail:
        with pytest.raises(httpx.ConnectError):
            asyncio.run(fetch_url("https://example.test/"))
        assert attempts == addresses[1:]
        assert delays == [0.1, 0.2]
    else:
        assert asyncio.run(fetch_url("https://example.test/")) == "connected"
        assert attempts == addresses[1:3]
        assert delays == [0.1]


def test_public_transport_closes_its_underlying_transport(monkeypatch):
    closed = []

    async def close(_transport):
        closed.append(True)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "aclose", close)
    asyncio.run(PublicNetworkTransport().aclose())
    assert closed == [True]


@pytest.mark.parametrize("body", ["", "long content"])
def test_fetch_handles_empty_and_truncated_pages(monkeypatch, body):
    async def respond(_transport, _request):
        return httpx.Response(200, text=body)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    result = asyncio.run(fetch_url("https://93.184.215.14/", max_length=4))
    assert result == ("long\n... [truncated]" if body else "(empty page)")


def test_fetch_redirect_loop_is_bounded(monkeypatch):
    requests = []

    async def redirect(_transport, request):
        requests.append(request.url)
        return httpx.Response(302, headers={"location": "/loop"})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", redirect)
    with pytest.raises(httpx.TooManyRedirects):
        asyncio.run(fetch_url("https://93.184.215.14/loop"))
    assert len(requests) == 11


def test_search_parses_html_decodes_destinations_and_limits_results(monkeypatch):
    from paicli.web.search import search_web

    requests = []

    async def respond(_transport, request):
        requests.append(request)
        return httpx.Response(
            200,
            text="""
<a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.test%2Fpage"
>A &amp; <b>B</b></a>
<a class="result__snippet">A useful <b>snippet</b></a>
<a class="result__a" href="https://second.test/">Second</a>
<a class="result__snippet">Other result</a>
""",
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    results = asyncio.run(search_web("hello world", max_results=1))
    assert len(results) == 1
    assert (results[0].title, results[0].url, results[0].snippet) == (
        "A & B",
        "https://example.test/page",
        "A useful snippet",
    )
    assert requests[0].url.params["q"] == "hello world"


def test_search_http_error_propagates(monkeypatch):
    from paicli.web.search import search_web

    async def reject(_transport, _request):
        return httpx.Response(503)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(search_web("query"))
