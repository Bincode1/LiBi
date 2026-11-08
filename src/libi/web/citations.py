from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urldefrag, urlsplit

from libi.context.tokens import estimate_text_tokens

SUBMIT_RESEARCH_REPORT = "submit_research_report"
_URL = re.compile(r"(?i)(?:https?://|www\.)[^\s<>()\[\]]+")
_MARKDOWN_LINK = re.compile(r"!?\[([^\]\n]*)\]\([^\n]*?\)")


@dataclass(frozen=True, slots=True)
class ResearchSource:
    title: str
    url: str
    excerpt: str
    response_id: str | None = None

    @classmethod
    def from_record(cls, record: object) -> ResearchSource | None:
        if not isinstance(record, dict):
            return None
        url = record.get("url")
        if not isinstance(url, str) or len(url) > 2048 or re.search(r"[\x00-\x20<>\[\]\\]", url):
            return None
        try:
            url = urldefrag(url).url
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
            ):
                return None
        except ValueError:
            return None
        excerpt = record.get("excerpt")
        if not isinstance(excerpt, str) or not excerpt.strip():
            return None
        title = record.get("title")
        response_id = record.get("response_id")
        return cls(
            " ".join(title.split())[:160]
            if isinstance(title, str) and title.strip()
            else parsed.hostname,
            url,
            " ".join(excerpt.split())[:320],
            response_id if isinstance(response_id, str) else None,
        )


def merge_sources(sources: Iterable[ResearchSource]) -> tuple[ResearchSource, ...]:
    by_url: dict[str, ResearchSource] = {}
    for source in sources:
        previous = by_url.get(source.url)
        if previous is None or (source.response_id and not previous.response_id):
            by_url[source.url] = source
    return tuple(by_url.values())


def _safe_text(text: str) -> str:
    text = _MARKDOWN_LINK.sub(r"\1", text)
    text = _URL.sub("（未核验链接）", text)
    return re.sub(r"([\\\[\]<>])", r"\\\1", text)


def _source_link(source: ResearchSource) -> str:
    title = _safe_text(source.title)
    qualifier = "（仅搜索摘要）" if source.response_id is None else ""
    return f"[{title}](<{source.url}>){qualifier}"


def source_list(sources: tuple[ResearchSource, ...]) -> str:
    return "\n".join(f"- {_source_link(source)}" for source in sources)


def report_output_definition() -> dict:
    return {
        "type": "function",
        "function": {
            "name": SUBMIT_RESEARCH_REPORT,
            "description": (
                "Submit the final answer as the only call in this response. "
                "Use the source IDs from this turn's delegated research for each finding. "
                "Use an empty source_ids list for a finding without evidence."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "findings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string", "description": "Plain text finding"},
                                "source_ids": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["text", "source_ids"],
                        },
                    },
                },
                "required": ["findings"],
            },
        },
    }


class CitationBook:
    """References issued to the model during one turn."""

    def __init__(self) -> None:
        self.entries: dict[str, ResearchSource] = {}
        self._ids: dict[str, str] = {}
        self.available: tuple[ResearchSource, ...] = ()

    def id_for(self, source: ResearchSource) -> str | None:
        return self._ids.get(source.url)

    def offer(self, sources: tuple[ResearchSource, ...], tokens: int) -> str:
        self.available = merge_sources((*self.available, *sources))
        lines = ["Research sources. Cite these IDs in submit_research_report:"]
        remaining = tokens - estimate_text_tokens(lines[0])
        for source in sources:
            number = self._ids.get(source.url) or f"S{len(self.entries) + 1}"
            label = "page read" if source.response_id else "search snippet only"
            line = f"{number} | {source.title} | {source.url} ({label}) | {source.excerpt}"
            cost = estimate_text_tokens(line) + 1
            if cost > remaining:
                continue
            remaining -= cost
            lines.append(line)
            self._ids[source.url] = number
            previous = self.entries.get(number)
            if previous is None or (source.response_id and not previous.response_id):
                self.entries[number] = source
        return "\n".join(lines) if len(lines) > 1 else ""

    def render(self, submission: dict, fallback: str) -> str:
        findings = submission.get("findings")
        rendered = []
        needs_sources = False
        if isinstance(findings, list):
            for finding in findings:
                if not isinstance(finding, dict):
                    continue
                text = finding.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                raw_ids = finding.get("source_ids")
                ids = raw_ids if isinstance(raw_ids, list) else []
                valid = dict.fromkeys(
                    value for value in ids if isinstance(value, str) and value in self.entries
                )
                links = " ".join(_source_link(self.entries[value]) for value in valid)
                warning = (
                    " **（待核验）**"
                    if not valid
                    or any(not isinstance(value, str) or value not in self.entries for value in ids)
                    else ""
                )
                needs_sources |= bool(warning)
                statement = _safe_text(" ".join(text.split()))
                rendered.append(f"- {statement}{warning} {links}".rstrip())
        sources = tuple(self.entries.values()) or self.available
        appendix = "已查阅的来源（不代表逐条引用）\n\n" + source_list(sources[:12])
        if rendered:
            title = submission.get("title")
            if isinstance(title, str) and title.strip():
                rendered.insert(0, f"## {_safe_text(' '.join(title.split()))}\n")
            body = "\n".join(rendered)
            return f"{body}\n\n{appendix}" if needs_sources else body
        body = _safe_text(fallback.strip())
        warning = "本次结论未逐条关联来源，待核验。"
        return "\n\n".join(item for item in (body, warning, appendix) if item)
