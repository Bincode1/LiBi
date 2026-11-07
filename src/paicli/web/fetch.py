from __future__ import annotations

import asyncio
import html
import ipaddress
import re
import socket
from urllib.parse import urlparse

import httpx


class NetworkPolicyError(ValueError):
    pass


class PublicNetworkTransport(httpx.AsyncBaseTransport):
    """Validate every hop and connect to the validated IP to prevent DNS rebinding."""

    def __init__(self) -> None:
        # Different hostnames may share an IP but require different TLS identities.
        self._transport = httpx.AsyncHTTPTransport(
            trust_env=False, limits=httpx.Limits(max_keepalive_connections=0)
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        addresses = await asyncio.to_thread(_validate_public_url, str(request.url))
        candidates = addresses[:3] if request.method in {"GET", "HEAD"} else addresses[:1]
        for attempt, address in enumerate(candidates):
            pinned_request = httpx.Request(
                request.method,
                request.url.copy_with(host=address),
                headers=request.headers,
                stream=request.stream,
                extensions={
                    **request.extensions,
                    "sni_hostname": request.url.raw_host.decode("ascii"),
                },
            )
            try:
                return await self._transport.handle_async_request(pinned_request)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if attempt == len(candidates) - 1:
                    raise
                await asyncio.sleep(0.1 * 2**attempt)
        raise RuntimeError("public URL validation returned no connection candidates")

    async def aclose(self) -> None:
        await self._transport.aclose()


async def fetch_url(url: str, max_length: int = 10_000, timeout: float = 15.0) -> str:
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        max_redirects=10,
        transport=PublicNetworkTransport(),
        trust_env=False,
    ) as client:
        response = await client.get(url, headers={"user-agent": "PaiCLI-Python/0.1.0"})
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        text = response.text
        if "html" in content_type:
            text = extract_text_from_html(text)
        if len(text) > max_length:
            text = text[:max_length] + "\n... [truncated]"
        return text or "(empty page)"


def extract_text_from_html(raw_html: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", raw_html, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _validate_public_url(url: str) -> tuple[str, ...]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise NetworkPolicyError("only http/https URLs are allowed")
    if not parsed.hostname:
        raise NetworkPolicyError("URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise NetworkPolicyError("URL credentials are not allowed")
    host = parsed.hostname
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        _reject_private_ip(ip)
        return (str(ip),)
    try:
        infos = socket.getaddrinfo(host, parsed.port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise NetworkPolicyError("cannot resolve URL hostname") from exc
    if not infos:
        raise NetworkPolicyError("URL hostname has no addresses")
    for info in infos:
        address = info[4][0]
        _reject_private_ip(ipaddress.ip_address(address))
    addresses = dict.fromkeys(str(info[4][0]) for info in infos)
    return tuple(sorted(addresses, key=lambda address: ipaddress.ip_address(address).version))


def _reject_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        _reject_private_ip(ip.ipv4_mapped)
    if not ip.is_global or ip.is_multicast:
        raise NetworkPolicyError("URL resolves to a non-public address")
