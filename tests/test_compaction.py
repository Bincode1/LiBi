from __future__ import annotations

import asyncio

import pytest

from paicli.context import (
    CompactionCoordinator,
    CompactionError,
    CompactionPlanner,
    CompactionPolicy,
    CompactionResponse,
    CompactionSerializer,
    CompactionValidator,
    MessageSource,
)
from paicli.llm.base import ProviderRequestError
from paicli.types import Message, Usage


@pytest.fixture
def summary():
    headings = [
        "Primary Request and Intent",
        "Constraints and Preferences",
        "Key Technical Concepts",
        "Files and Changes",
        "Decisions and Rationale",
        "Progress",
        "Pending Work and Next Steps",
        "Critical Context",
    ]
    return "\n".join(f"## {heading}\n(none)" for heading in headings) + (
        "\n### Done\n(none)\n### In Progress\n(none)\n### Blocked\n(none)"
    )


@pytest.fixture
def history():
    return [
        Message(role="user", content="old question " + "a" * 500),
        Message(role="assistant", content="old answer " + "b" * 500),
        Message(role="user", content="recent request"),
    ]


@pytest.mark.parametrize(
    "overrides",
    [
        {"context_window": 0},
        {"normal_max_output_tokens": -1},
        {"threshold_ratio": 0},
        {"threshold_ratio": 1.1},
        {"compression_target": 0},
        {"compression_target": 1.1},
        {"reserve_tokens": -1},
    ],
)
def test_compaction_policy_rejects_invalid_budgets(overrides):
    values = {"context_window": 10_000, "normal_max_output_tokens": 100, **overrides}
    with pytest.raises(ValueError):
        CompactionPolicy(**values)


def test_compaction_noops_and_provenance_boundaries(history):
    planner = CompactionPlanner(CompactionPolicy(10_000, 100, min_recent_messages=1))
    assert planner.plan([]).no_op_reason == "no_history"
    assert planner.plan(history).no_op_reason == "below_threshold"
    assert planner.plan(history[-1:], force=True).no_op_reason == "no_safe_range"
    with pytest.raises(ValueError, match="same length"):
        planner.plan(history, sources=[])
    source = MessageSource(checkpoint_ids=("old-checkpoint",))
    plan = planner.plan(history, force=True, sources=[source, source, source])
    assert plan.no_op_reason == "no_safe_range"


@pytest.mark.parametrize(
    ("response", "code"),
    [
        ("", "empty_summary"),
        ("missing headings", "invalid_summary"),
        ("x" * 20_000, "summary_too_large"),
        (CompactionResponse("partial", finish_reason="tool_use"), "invalid_summary"),
        (CompactionResponse("partial", finish_reason="max_tokens"), "invalid_summary"),
    ],
)
def test_invalid_summary_is_rejected_with_stable_code(response, code):
    with pytest.raises(CompactionError) as captured:
        CompactionValidator(max_output_tokens=1000).validate(response)
    assert captured.value.code == code


def test_serializer_redacts_and_truncates_without_mutating_messages():
    messages = [
        Message(role="user", content="password=synthetic-inline-value"),
        Message(role="tool", tool_call_id="call", content="head" + "x" * 2000 + "tail"),
        Message(
            role="user",
            content=[
                {"type": "text", "text": "image request"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,PRIVATE_BINARY"}},
            ],
        ),
    ]
    serializer = CompactionSerializer(max_tool_result_chars=256)
    result = serializer.serialize(messages, focus="preserve the request")
    assert "synthetic-inline-value" not in result.text
    assert "PRIVATE_BINARY" not in result.text
    assert "head" in result.text and "tail" in result.text
    assert "tool result truncated" in result.text
    assert (
        result.input_hash == serializer.serialize(messages, focus="preserve the request").input_hash
    )
    assert "synthetic-inline-value" in messages[0].content


def test_compaction_commits_summary_usage_and_provenance(history, summary):
    requests, events, checkpoints = [], [], []

    class Client:
        async def complete(self, request):
            requests.append(request)
            return CompactionResponse(summary, usage=Usage(input_tokens=50, output_tokens=20))

    coordinator = CompactionCoordinator(
        policy=CompactionPolicy(10_000, 100, min_recent_messages=1),
        client=Client(),
        provider="test",
        model="summary-model",
        event_writer=lambda kind, payload: events.append((kind, payload)),
    )
    result = asyncio.run(
        coordinator.compact(
            history,
            sources=[MessageSource((i,)) for i in range(1, 4)],
            through_sequence=3,
            checkpoint_writer=checkpoints.append,
        )
    )
    assert result.summary == summary
    assert result.usage.total_tokens == 70
    assert result.checkpoint_messages[-1] == history[-1]
    assert result.checkpoint_sources[0].event_sequences == (1, 2)
    assert [kind for kind, _ in events] == ["compaction/started", "compaction/checkpoint"]
    assert checkpoints[0]["source_sequences"] == [1, 2, 3]
    assert requests[0].tools == () and requests[0].purpose == "compaction"


@pytest.mark.parametrize("failure", ["provider", "invalid_summary", "checkpoint"])
def test_compaction_failure_records_reason_and_can_retry(history, summary, failure):
    events = []
    failing = True

    class Client:
        async def complete(self, _request):
            if failing and failure == "provider":
                raise RuntimeError("summarizer unavailable")
            return CompactionResponse(
                "invalid" if failing and failure == "invalid_summary" else summary
            )

    def checkpoint(_payload):
        if failing and failure == "checkpoint":
            raise OSError("checkpoint write failed")

    coordinator = CompactionCoordinator(
        policy=CompactionPolicy(10_000, 100, min_recent_messages=1),
        client=Client(),
        provider="test",
        model="summary-model",
        event_writer=lambda kind, payload: events.append((kind, payload)),
    )
    with pytest.raises(CompactionError) as captured:
        asyncio.run(coordinator.compact(history, checkpoint_writer=checkpoint))
    assert captured.value.code == (
        "invalid_summary" if failure == "invalid_summary" else "provider_error"
    )
    assert [kind for kind, _ in events] == ["compaction/started", "compaction/failed"]
    assert not coordinator._busy
    failing = False
    assert asyncio.run(coordinator.compact(history, checkpoint_writer=checkpoint)).compaction_id


def test_compaction_records_attempts_and_final_provider_error(history):
    events = []

    class Client:
        async def complete(self, request):
            for attempt, code in enumerate((503, 500), start=1):
                await request.on_provider_attempt_failed(
                    {
                        "attempt": attempt,
                        "stage": "response_headers",
                        "elapsed_ms": 35,
                        "error_type": "HTTPStatusError",
                        "retrying": attempt == 1,
                        "provider_error": {"kind": "http", "status_code": code},
                    }
                )
            raise ProviderRequestError("failed", kind="http", status_code=500)

    coordinator = CompactionCoordinator(
        policy=CompactionPolicy(10_000, 100, min_recent_messages=1),
        client=Client(),
        provider="test",
        model="summary-model",
        event_writer=lambda kind, payload: events.append((kind, payload)),
    )
    with pytest.raises(CompactionError) as captured:
        asyncio.run(coordinator.compact(history, attempt_id="compact_attempt_example"))

    assert [kind for kind, _ in events] == [
        "compaction/started",
        "request/attempt_failed",
        "request/attempt_failed",
        "compaction/failed",
    ]
    first, second = events[1][1], events[2][1]
    assert first["request_id"] == second["request_id"] == "compact_attempt_example"
    assert first["purpose"] == second["purpose"] == "compaction"
    assert first["provider"] == "test" and first["model"] == "summary-model"
    assert [first["retrying"], second["retrying"]] == [True, False]
    assert [first["provider_error"]["status_code"], second["provider_error"]["status_code"]] == [
        503,
        500,
    ]
    assert captured.value.code == "provider_error"
    assert captured.value.provider_error == {"kind": "http", "status_code": 500}
    assert events[-1][1]["provider_error"] == {"kind": "http", "status_code": 500}


def test_compaction_lease_fences_writes_and_is_released_after_failure(session_store):
    sid = session_store.create_session()
    first = session_store.append(sid, "user/message", {"content": "old"})
    lease = session_store.acquire_compaction_lease(
        sid, attempt_id="attempt", base_sequence=first.sequence, surface_hash="surface"
    )
    with pytest.raises(RuntimeError, match="busy"):
        session_store.acquire_compaction_lease(
            sid, attempt_id="other", base_sequence=first.sequence, surface_hash="surface"
        )
    with pytest.raises(RuntimeError):
        session_store.append(sid, "user/message", {"content": "must wait"})
    renewed = session_store.renew_compaction_lease(lease.owner_token)
    assert renewed.owner_token == lease.owner_token
    failed = session_store.fail_compaction(lease.owner_token, {"failure_code": "provider_error"})
    assert failed.event_type == "compaction/failed"
    with pytest.raises(RuntimeError, match="expired or invalid"):
        session_store.commit_compaction_checkpoint(lease.owner_token, {})
    assert session_store.append(sid, "user/message", {"content": "retry"}).sequence == 3


def test_compaction_lease_rejects_stale_history(session_store):
    sid = session_store.create_session()
    session_store.append(sid, "user/message", {"content": "new"})
    with pytest.raises(RuntimeError, match="history changed"):
        session_store.acquire_compaction_lease(
            sid, attempt_id="old", base_sequence=0, surface_hash="old-surface"
        )
