from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from libi.image import parse_image_references
from libi.llm.openai_compatible import OpenAICompatibleClient


def test_parse_local_image_reference(tmp_path):
    image_path = tmp_path / "image.png"
    Image.new("RGBA", (10, 10), (255, 0, 0, 128)).save(image_path)

    content = parse_image_references(f"look @image:{image_path.name}", str(tmp_path))

    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    data_url = content[1]["image_url"]["url"]
    header, encoded = data_url.split(",", 1)
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as encoded_image:
        assert header == f"data:{Image.MIME[encoded_image.format]};base64"
        assert encoded_image.size == (10, 10)


def test_non_vision_model_omits_image_payload(tmp_path):
    image_path = tmp_path / "image.png"
    Image.new("RGB", (10, 10), "red").save(image_path)
    content = parse_image_references(f"look @image:{image_path.name}", str(tmp_path))
    client = OpenAICompatibleClient(
        provider_name="deepseek",
        model="deepseek-flash",
        api_key="key",
        base_url="https://example.com/v1",
    )

    formatted = client._format_content(content)

    assert isinstance(formatted, str)
    assert "Image omitted" in formatted


def test_image_reference_preserves_text_and_remote_urls(tmp_path):
    message = "plain text"
    assert parse_image_references(message, str(tmp_path)) == message
    content = parse_image_references(
        "before @image:https://example.test/a.png after", str(tmp_path)
    )
    assert content == [
        {"type": "text", "text": "before "},
        {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
        {"type": "text", "text": " after"},
    ]


def test_image_missing_or_outside_workspace_is_rejected(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse_image_references("@image:missing.png", str(tmp_path))
    with pytest.raises(ValueError):
        parse_image_references("@image:../outside.png", str(tmp_path))


@pytest.mark.parametrize("mode", ["RGB", "RGBA", "L"])
def test_image_resizes_and_vision_payload_drops_internal_metadata(tmp_path, mode):
    target = tmp_path / "large.png"
    Image.new(mode, (3200, 1600)).save(target)
    content = parse_image_references("@image:large.png", str(tmp_path))
    assert content[0]["metadata"]["width"] == 1568
    assert content[0]["metadata"]["height"] == 784
    client = OpenAICompatibleClient("openai", "vision-model", "synthetic", "https://example.test")
    formatted = client._format_content(content)
    assert "metadata" not in formatted[0]
    assert "metadata" in content[0]
