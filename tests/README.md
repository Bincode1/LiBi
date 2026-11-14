# Test Suite

Run commands from the repository root. Python 3.11+ is supported; use the locked
development environment rather than a system-wide pytest installation.

```powershell
uv sync --locked --extra dev
uv run --extra dev ruff check .
uv run --extra dev ruff format --check .
```

For local edits, start with the affected files or the fast component suite:

```powershell
uv run --extra dev python -m pytest -m "not integration and not slow"
```

Before merging or releasing, run the complete regression suite with coverage:

```powershell
uv run --extra dev python -m pytest --cov --cov-report=term-missing --cov-report=html
```

Plain `python -m pytest` runs every test too. Marker selection is always explicit;
the default configuration and CI never skip integration or slow tests.

## Isolation And Failures

- `conftest.py` assigns each test a temporary HOME and USERPROFILE and clears
  provider configuration inherited from the host. User databases, audit logs,
  snapshots, skills, and credentials are never test fixtures.
- Temporary homes live beside temporary workspaces so snapshots cannot copy
  their own storage recursively.
- External DNS lookups and socket connections fail immediately, and teardown
  also fails if application code swallowed such an error. HTTP provider and web tests
  replace the transport; local HTTP tests bind only to an ephemeral loopback port.
  Child processes are local Python MCP servers or short command scripts.
- Each test has a 30-second timeout. Async waits and thread joins also have local
  deadlines; thread futures propagate worker failures to the test.
- `provider_stream` scripts SSE responses while keeping the real provider parser,
  tool execution, agent loop, and persistence active in entry-point tests.
- Keep fake credentials synthetic. Do not load project `.env` files or use real
  provider keys. Test failures and snapshots must not contain real secrets.

## Coverage Map

| Chain | Tests | Important boundaries |
| --- | --- | --- |
| Configuration | `test_config.py`, `test_environment.py` | Precedence, invalid/unreadable files, provider settings, subagent limits, redaction |
| CLI / SDK / REPL | `test_cli.py`, `test_sdk.py`, `test_repl.py` | Real runtime, ReAct/Plan, history/resume, missing key, provider errors, exit/cleanup |
| Runtime HTTP | `test_runtime.py`, `test_runtime_http.py` | Authentication, JSON validation, routes, turns, SSE resume, cursors, child isolation |
| Durable tasks | `test_tasks.py`, `test_runtime_worker.py` | Atomic claim, leases, stale owners, terminal states, cancellation, migration, project scopes |
| Agent / Plan | `test_query.py`, `test_plan.py`, `test_reliability.py` | Tool replay, skills, DAG batches, cycles, failed dependencies, cleanup |
| Child runtime | `test_subagents.py`, `test_subagent_observation.py` | Capacity, deadlines, nesting, recovery, exactly-once delivery, bounded observation, approvals |
| Tools | `test_tools.py`, `test_tool_executor.py`, `test_tool_parsing.py`, `test_builtin_wiring.py` | Approval matrix, malformed input, timeouts, cancellation, bounded reads, serial writes, real handler wiring |
| Files / commands | `test_file_ops.py`, `test_policy.py`, `test_runtime_http.py` | Paths, byte/line limits, dry runs, missing matches, permissions, exit codes, process termination |
| Sessions / compaction | `test_session.py`, `test_compaction.py`, `test_context.py` | Projection, replay, migration, summary validation, provenance, fencing, failed compaction and retry |
| LLM / images | `test_llm_usage.py`, `test_llm_protocol.py`, `test_image.py`, `test_model_selector.py` | SSE fragmentation, finish reasons, usage/prices, HTTP failures, actual image MIME, resize |
| MCP | `test_mcp.py`, `test_mcp_config.py` | Real stdio server, tool calls, broken server isolation, scope merge, environment expansion |
| Memory / skills / prompts | `test_memory.py`, `test_skill.py`, `test_prompt.py` | Deduplication, expiry, quotas, relevance, project isolation, one-shot context |
| Snapshots / index / diagnostics | `test_snapshot.py`, `test_code_index.py`, `test_lsp.py` | Restore/delete safety, ignored paths, stale index refresh, root isolation, syntax errors |
| Rendering | `test_render.py`, `test_permission_mode.py` | Terminal streaming boundaries, permissions, token display, subagent status |

Run focused suites with explicit file paths, for example:

```powershell
uv run --extra dev python -m pytest tests/test_cli.py tests/test_sdk.py tests/test_runtime_http.py
uv run --extra dev python -m pytest tests/test_compaction.py tests/test_session.py
uv run --extra dev python -m pytest -m integration
```

The `integration` marker covers CLI/SDK/REPL entry points, shared bootstrap,
runtime workers, HTTP, real MCP processes, and command execution. Pure command
classification and pre-execution policy checks stay in the fast suite. The
`slow` marker keeps the 22-turn agent regression out of the fast command while
preserving its original boundary. Fast tests still exercise real SQLite, files,
and interacting components; they are not a substitute for entry-point checks.

## Keeping Tests Useful

- Add a case for a distinct contract, failure branch, boundary, or known bug.
  Test count is not a target. Use representative decision tables instead of
  multiplying inputs that the code never consults.
- Keep approval policy decisions in executor tests; builtin tool tests verify
  that each write tool is protected, fails closed without a presenter, and
  performs the intended file change after approval.
- Assert returned events, persisted state, actual side effects, and resource
  cleanup. Prefer these over private attribute values and incidental result order.
- Protocol tests own provider error text and redaction; entry-point tests verify
  propagation, exit status, and persisted failure. These test different contracts.
- When pruning cases, compare covered lines and branches as well as the overall
  percentage, and inspect any lost path. Keep regressions for previously fixed
  configuration, compaction, image MIME, path, MCP EOF, and observer race bugs.

## Scope Of Automated Validation

The CI workflow targets Python 3.11 and 3.12 on Windows and Linux with Python
tests, lint, and formatting checks. Coverage includes branches and enforces an 80% overall
floor; subprocess execution is checked by assertions but is not included in the
parent's coverage counters. Coverage artifacts are ignored by Git.

Automated tests do not verify a live paid provider, an installed Chrome DevTools
server, or a human-driven terminal session. REPL tests script input at the prompt
boundary. Do not describe this suite as exhaustive interactive terminal or
real-provider validation.
