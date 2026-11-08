from __future__ import annotations

import asyncio
import json

import pytest

from paicli.agent import QueryEngine
from paicli.agent.subagents import ChildReport, _report_result
from paicli.config import PaiCliConfig
from paicli.context.tokens import estimate_text_tokens
from paicli.runtime.conversation import SubagentCatalog
from paicli.session import SessionStore
from paicli.tools import ToolRegistry, get_builtin_tools
from paicli.web.citations import CitationBook, ResearchSource
from paicli.web.search import SearchResult

READ_URL = "https://example.test/release"
SEARCH_URL = "https://example.test/survey"
MADE_UP_URL = "https://example.test/not-read"


def call(name, arguments):
    return {
        "type": "tool_call_delta",
        "tool_call": {
            "index": 0,
            "id": name,
            "function": {"name": name, "arguments": json.dumps(arguments)},
        },
    }


@pytest.mark.parametrize("structured", [True, False])
def test_delegated_research_links_are_validated_in_the_existing_summary_request(
    tmp_path, monkeypatch, structured
):
    async def fetch(url, **_kwargs):
        assert url == READ_URL
        return "# Official release\n\nThe release is available."

    async def search(query, **_kwargs):
        assert query == "survey"
        return [SearchResult("Survey results", SEARCH_URL, "A search excerpt.")]

    monkeypatch.setattr("paicli.tools.builtins.fetch_url", fetch)
    monkeypatch.setattr("paicli.tools.builtins.search_web", search)

    class Client:
        model_name = "fake-model"
        provider_name = "fake-provider"
        max_context_window = 128_000
        parent_calls = 0

        async def chat(self, messages, tools, *, system_prompt):
            names = {tool["function"]["name"] for tool in tools}
            question = next(msg.content for msg in messages if msg.role == "user")
            question = question.partition("\n\n")[0]
            if question in {"Read release", "Find survey"}:
                if messages[-1].role == "tool":
                    yield {
                        "type": "text_delta",
                        "text": f"Child findings {READ_URL} {SEARCH_URL} {MADE_UP_URL}",
                    }
                    yield {"type": "message_end", "stop_reason": "end_turn"}
                    return
                if question == "Read release":
                    yield call("web_fetch", {"url": READ_URL})
                else:
                    yield call("web_search", {"query": "survey"})
                yield {"type": "message_end", "stop_reason": "tool_use"}
                return

            self.parent_calls += 1
            if self.parent_calls == 1:
                assert "submit_research_report" not in names
                yield call(
                    "subagent",
                    {
                        "tasks": [
                            {"description": "Release", "prompt": "Read release"},
                            {"description": "Survey", "prompt": "Find survey"},
                        ]
                    },
                )
                yield {"type": "message_end", "stop_reason": "tool_use"}
                return

            assert self.parent_calls == 2
            assert "submit_research_report" in names
            assert "S1 | Official release" in messages[-1].content
            assert "S2 | Survey results" in messages[-1].content
            if structured:
                yield call(
                    "submit_research_report",
                    {
                        "title": "Findings",
                        "findings": [
                            {"text": "Release available.", "source_ids": ["S1", "S1"]},
                            {"text": "Survey snippet.", "source_ids": ["S2"]},
                            {"text": f"See [fake]({MADE_UP_URL}).", "source_ids": ["S999"]},
                            {"text": "Missing evidence.", "source_ids": []},
                        ],
                    },
                )
                yield {"type": "message_end", "stop_reason": "tool_use"}
            else:
                yield {"type": "text_delta", "text": f"Plain answer {MADE_UP_URL}"}
                yield {"type": "message_end", "stop_reason": "end_turn"}

    config = PaiCliConfig()
    config.policy.hitl_mode = "never"
    config.features.memory = False
    config.features.skill = False
    registry = ToolRegistry()
    registry.register_all(
        [tool for tool in get_builtin_tools() if tool.name in {"web_fetch", "web_search"}]
    )
    store = SessionStore(tmp_path / "runtime.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    client = Client()

    async def run():
        async with QueryEngine(
            llm_client=client,
            config=config,
            tool_registry=registry,
            cwd=str(workspace),
            session_store=store,
        ) as engine:
            result = await engine.ask_complete_async("Research the release and survey")
            return engine.session_id, result.text

    session_id, answer = asyncio.run(run())
    assert client.parent_calls == 2
    assert MADE_UP_URL not in answer
    assert f"[Official release](<{READ_URL}>)" in answer
    assert f"[Survey results](<{SEARCH_URL}>)（仅搜索摘要）" in answer
    if structured:
        assert "- Release available. [Official release]" in answer
        assert "- See fake. **（待核验）**" in answer
        assert "- Missing evidence. **（待核验）**" in answer
    else:
        assert "本次结论未逐条关联来源，待核验" in answer

    reopened = SessionStore(tmp_path / "runtime.db")
    events = reopened.events(session_id)
    assert [e.payload["content"] for e in events if e.event_type == "assistant/message"][
        -1
    ] == answer
    (batch,) = [e for e in events if e.event_type == "tool/result"]
    assert {record["url"] for record in batch.payload["sources"]} == {READ_URL, SEARCH_URL}
    assert [record["source_id"] for record in batch.payload["sources"]] == ["S1", "S2"]
    assert reopened.get_web_document(session_id, batch.payload["sources"][0]["response_id"]) is None
    catalog = SubagentCatalog()
    state = reopened.state(session_id)
    catalog.advance(state, events)
    child_id = next(
        e.payload["child_session_id"] for e in events if e.event_type == "agent/child_started"
    )
    (child,) = catalog.snapshot(
        reopened,
        session_id,
        active_parent_turn=None,
        waiting_agents=set(),
        only_session_id=child_id,
    )
    assert "## 查阅来源" in child["report"]
    assert f"[Official release](<{READ_URL}>)" in child["report"]


def test_citation_table_keeps_whole_sources_when_report_is_cropped():
    source = ResearchSource("Official release", READ_URL, "The release is available.", "web_1")
    citations = CitationBook()
    body = "调研证据" * 20_000
    report = ChildReport("run", "child", "completed", body, sources=(source,))
    result = _report_result([report], 250, citations)
    assert "S1 | Official release" in result.content
    assert "Full report saved" in result.content
    assert estimate_text_tokens(result.content) <= 250
    assert result.sources == (source,)
    assert report.result == body
