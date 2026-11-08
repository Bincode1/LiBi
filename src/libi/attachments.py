from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
from collections.abc import Callable, Sequence
from itertools import islice
from pathlib import Path
from typing import Any

from PIL import Image

from libi.image.processor import encode_image
from libi.types import Message

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_TURN_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_TURN_ATTACHMENTS = 5
_IMAGE_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_TEXT_SUFFIXES = {
    ".txt",
    ".md",
    ".log",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".xml",
    ".html",
    ".css",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".py",
    ".rs",
    ".go",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".sql",
    ".sh",
    ".ps1",
}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_ID = re.compile(r"[0-9a-f]{64}")


class AttachmentError(ValueError):
    pass


class AttachmentStore:
    def __init__(self, db_path: Path):
        root = db_path.parent / "attachments"
        if os.name == "nt":
            path = str(root.absolute())
            if not path.startswith("\\\\?\\"):
                path = "\\\\?\\UNC\\" + path[2:] if path.startswith("\\\\") else "\\\\?\\" + path
            root = Path(path)
        self.root = root

    def directory(self, session_id: str) -> Path:
        return self.root / hashlib.sha256(session_id.encode()).hexdigest()

    def add(self, session_id: str, name: str, data: bytes) -> dict[str, Any]:
        name = name.strip()
        if not name or len(name) > 180 or any(c in "/\\" or not c.isprintable() for c in name):
            raise AttachmentError("附件文件名无效。")
        if not 0 < len(data) <= MAX_ATTACHMENT_BYTES:
            raise AttachmentError("附件必须小于或等于 10 MiB，且不能为空。")
        media_type = _media_type(name, data)
        attachment_id = hashlib.sha256(name.encode() + b"\0" + data).hexdigest()
        record = {"id": attachment_id, "name": name, "media_type": media_type, "size": len(data)}
        directory = self.directory(session_id)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        meta = directory / f"{attachment_id}.json"
        blob = directory / attachment_id
        if not meta.is_file() or not blob.is_file():
            _write(blob, data)
            _write(meta, json.dumps(record, ensure_ascii=False).encode())
        return record

    def get(self, session_id: str, attachment_id: str) -> tuple[dict[str, Any], Path]:
        if not isinstance(attachment_id, str) or not _ID.fullmatch(attachment_id):
            raise AttachmentError("附件 ID 无效。")
        directory = self.directory(session_id)
        path = directory / attachment_id
        try:
            record = json.loads((directory / f"{attachment_id}.json").read_bytes())
        except FileNotFoundError:
            raise AttachmentError("该附件不在此会话中，或已无法读取。") from None
        if not path.is_file():
            raise AttachmentError("该附件不在此会话中，或已无法读取。")
        return record, path

    def read_text(
        self,
        session_id: str,
        attachment_id: str,
        *,
        start: int = 1,
        limit: int = 80,
        start_column: int = 1,
    ) -> str:
        record, path = self.get(session_id, attachment_id)
        if record["media_type"] != "text/plain":
            raise AttachmentError("该附件不是可分段读取的文本。")
        if (
            type(start) is not int
            or start < 1
            or start > MAX_ATTACHMENT_BYTES
            or type(limit) is not int
            or not 1 <= limit <= 200
            or type(start_column) is not int
            or start_column < 1
        ):
            raise AttachmentError("起始行或列无效，每次可读取 1 至 200 行。")
        output = [f"{record['name']}（附件 ID {attachment_id}）"]
        remaining = 3600
        with path.open(encoding="utf-8-sig") as source:
            for number, line in enumerate(islice(source, start - 1, start + limit), start):
                if number >= start + limit or remaining <= 0:
                    output.append(f"还有更多内容。使用 read_attachment 从第 {number} 行继续读取。")
                    break
                column = start_column if number == start else 1
                line = line.rstrip("\r\n")[column - 1 :]
                output.append(f"{number}: {line[:remaining]}")
                if len(line) > remaining:
                    output.append(
                        f"当前行还有更多内容。使用 read_attachment 设置 start_line={number}、"
                        f"start_column={column + remaining} 继续读取。"
                    )
                    break
                remaining -= len(line) + 12
        if len(output) == 1:
            return f"{record['name']}：没有第 {start} 行。"
        return "\n".join(output)

    def prompt_parts(
        self,
        refs: Sequence[dict[str, Any]],
        *,
        selected: set[tuple[str, str]],
        supports_images: bool,
        clean_text: Callable[[str], str],
    ) -> list[dict[str, Any]]:
        parts = []
        for item in refs:
            session_id, attachment_id = item["session_id"], item["id"]
            label = f"用户附件 {item['name']}（ID {attachment_id}）。"
            if (session_id, attachment_id) not in selected:
                parts.append({"type": "text", "text": label})
                continue
            try:
                record, path = self.get(session_id, attachment_id)
                if record["media_type"] == "text/plain":
                    excerpt = clean_text(self.read_text(session_id, attachment_id))
                    parts.append({"type": "text", "text": f"以下为用户附件数据：\n{excerpt}"})
                elif supports_images:
                    data_url, width, height = encode_image(path)
                    parts.extend(
                        [
                            {"type": "text", "text": label},
                            {
                                "type": "image_url",
                                "image_url": {"url": data_url},
                                "metadata": {
                                    "source": item["name"],
                                    "width": width,
                                    "height": height,
                                },
                            },
                        ]
                    )
                else:
                    parts.append({"type": "text", "text": label + "当前模型无法读取图片。"})
            except (OSError, AttachmentError):
                parts.append({"type": "text", "text": label + "本机附件无法读取。"})
        return parts


def message_attachments(message: Message) -> list[dict[str, Any]]:
    if message.role != "user" or isinstance(message.content, str):
        return []
    return [part for part in message.content if part.get("type") == "attachment"]


def latest_attachments(messages: Sequence[Message]) -> set[tuple[str, str]]:
    for message in reversed(messages):
        if refs := message_attachments(message):
            return {(ref["session_id"], ref["id"]) for ref in refs}
    return set()


def _media_type(name: str, data: bytes) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        try:
            with Image.open(io.BytesIO(data), formats=list(_IMAGE_TYPES)) as image:
                if image.width * image.height > 32_000_000 or getattr(image, "n_frames", 1) != 1:
                    raise AttachmentError("图片像素过多，或包含多帧。")
                media_type = _IMAGE_TYPES[image.format]
                image.load()
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            if isinstance(exc, AttachmentError):
                raise
            raise AttachmentError("无法读取图片。请使用有效的 PNG、JPEG 或 WebP。") from None
        return media_type
    if suffix not in _TEXT_SUFFIXES:
        raise AttachmentError("此文件类型暂不支持。请选择图片、文本、日志或常见代码文件。")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise AttachmentError("文本附件必须使用 UTF-8 编码。") from None
    if not text or "\0" in text:
        raise AttachmentError("无法读取空文本或二进制文件。")
    return "text/plain"


def _write(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
