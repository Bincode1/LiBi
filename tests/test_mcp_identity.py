import asyncio
import base64
from io import BytesIO

import httpx
from mcp.types import Icon, Implementation
from PIL import Image

from libi.mcp.config import McpServerSpec
from libi.mcp.identity import connection_identity, server_icons
from libi.mcp.management import McpManagement


def test_saved_connector_gets_its_brand_without_connecting(tmp_path):
    management = McpManagement(str(tmp_path))
    revision = management.list(auto_start=True)["revision"]
    management.mutate(
        "add",
        {
            "name": "my-map",
            "revision": revision,
            "config": {"url": "https://mcp.amap.com/mcp?key=synthetic-map-key"},
        },
    )
    (server,) = management.list(auto_start=True)["servers"]
    assert server["identity"] == {"brand": "amap", "source": "mcp.amap.com"}
    assert server["check"] is None
    assert not server["config"]["enabled"]


def test_brand_matching_uses_exact_endpoints_and_package_names():
    for url in (
        "https://mcp.amap.com.example.org/mcp",
        "https://mcp.amap.com/other",
        "https://mcp.amap.com:8443/mcp",
    ):
        assert connection_identity(McpServerSpec("amap", "http", url=url))["brand"] is None
    spec = McpServerSpec("map", command="npx", args=["-y", "@amap/amap-maps-mcp-server@latest"])
    assert connection_identity(spec)["brand"] == "amap"


def test_server_icon_fetches_only_its_https_origin_and_returns_a_safe_thumbnail(monkeypatch):
    image = BytesIO()
    Image.new("RGB", (96, 96)).save(image, format="PNG")
    requests = []

    async def handle(_transport, request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "image/png"}, content=image.getvalue())

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    info = Implementation(
        name="server",
        version="1",
        icons=[
            Icon(src="https://unrelated.example/logo.png"),
            Icon(src="https://mcp.example/logo.png", theme="dark"),
        ],
    )
    icons = asyncio.run(server_icons(info, "https://mcp.example/mcp?key=synthetic"))
    (request,) = requests
    assert str(request.url) == "https://mcp.example/logo.png"
    assert "authorization" not in request.headers and "cookie" not in request.headers
    assert icons.keys() == {"dark"}
    prefix, encoded = icons["dark"].split(",", 1)
    assert prefix == "data:image/png;base64"
    with Image.open(BytesIO(base64.b64decode(encoded))) as thumbnail:
        assert thumbnail.size == (64, 64)
