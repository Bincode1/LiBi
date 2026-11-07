import { describe, expect, it } from 'vitest'
import { mergeEvents, projectTurns, resolveActiveRun } from './events'
import type { RuntimeEvent } from './types'
const event = (
  sequence: number,
  type: string,
  payload: Record<string, unknown> = {},
  extra: Partial<RuntimeEvent> = {},
): RuntimeEvent => ({
  id: `e${sequence}`,
  sequence,
  type,
  payload,
  turn_id: 'turn-1',
  step_id: 'step-1',
  created_at: '2026-11-03T00:00:00Z',
  ...extra,
})

describe('submitted run tracking', () => {
  const pending = { message: '检查项目', afterSequence: 5, run: { id: 'run-1', acceptedAt: 30 } }
  const oldFeed = {
    cursor: 5,
    events: [event(4, 'turn/started'), event(5, 'turn/completed')],
    activeRun: null,
    connectedAt: 10,
  }

  it('keeps Stop available for the submitted run before SSE acknowledges it', () => {
    const expected = { run_id: 'run-1', status: 'running' }
    expect(resolveActiveRun(oldFeed, pending)).toEqual(expected)
    expect(
      resolveActiveRun(
        { ...oldFeed, activeRun: { run_id: 'previous-run', status: 'running' } },
        pending,
      ),
    ).toEqual(expected)
    expect(
      resolveActiveRun(
        { ...oldFeed, activeRun: { run_id: 'run-1', status: 'canceling' } },
        pending,
      ),
    ).toEqual({ run_id: 'run-1', status: 'canceling' })
  })

  it('clears a missing run on a newer cursor, a fresh connection, or after it was observed', () => {
    expect(resolveActiveRun({ ...oldFeed, cursor: 6 }, pending)).toBeUndefined()
    expect(resolveActiveRun({ ...oldFeed, connectedAt: 40 }, pending)).toBeUndefined()
    expect(resolveActiveRun({ ...oldFeed, lastSeenRunId: 'run-1' }, pending)).toBeUndefined()
  })

  it('clears a run on a terminal event even if the feed does not expose active_run', () => {
    const events = [
      ...oldFeed.events,
      event(6, 'turn/started', {}, { turn_id: 'turn-2' }),
      event(7, 'turn/canceled', {}, { turn_id: 'turn-2' }),
    ]
    expect(resolveActiveRun({ cursor: 7, events }, pending)).toBeUndefined()
  })

  it('matches a receipt to its exact turn even when the cursor advances past another run', () => {
    const accepted = { ...pending, run: { ...pending.run, turnId: 'turn-3' } }
    const events = [
      ...oldFeed.events,
      event(6, 'turn/started', {}, { turn_id: 'turn-2' }),
      event(7, 'turn/canceled', {}, { turn_id: 'turn-2' }),
    ]
    expect(resolveActiveRun({ ...oldFeed, cursor: 7, events }, accepted)).toEqual({
      run_id: 'run-1',
      status: 'running',
    })
    expect(projectTurns(events, null, accepted).at(-1)?.id).toBe('pending')
    events.push(
      event(8, 'turn/started', {}, { turn_id: 'turn-3' }),
      event(9, 'turn/canceled', {}, { turn_id: 'turn-3' }),
    )
    expect(resolveActiveRun({ ...oldFeed, cursor: 9, events }, accepted)).toBeUndefined()
  })
})
describe('event projection', () => {
  it('deduplicates replayed events and orders non-contiguous sequences', () => {
    expect(
      mergeEvents(
        [event(8, 'user/message')],
        [event(8, 'user/message'), event(3, 'turn/started')],
      ).map((e) => e.sequence),
    ).toEqual([3, 8])
  })
  it('keeps tool commentary separate from the completed answer', () => {
    const history = [
      event(1, 'assistant/message', { content: '先检查文件。', tool_calls: [{ id: 'tool-1' }] }),
      event(2, 'tool/execution_started', { name: 'read_file' }, { tool_call_id: 'tool-1' }),
    ]
    const [turn] = projectTurns(history)
    expect(turn.status).toBe('running')
    expect(turn.tools[0].status).toBe('running')
    const [finished] = projectTurns([
      ...history,
      event(3, 'assistant/message', { content: '找到了空输入错误。' }, { step_id: 'step-2' }),
      event(4, 'turn/completed'),
    ])
    expect(finished.messages).toEqual([
      { id: 'step-1', text: '先检查文件。', commentary: true, sequence: 1 },
      { id: 'step-2', text: '找到了空输入错误。', sequence: 3 },
    ])
  })
  it('shows a submitted question while the next turn is still starting', () => {
    const history = [
      event(1, 'turn/started'),
      event(2, 'user/message', { content: '你好' }),
      event(3, 'assistant/message', { content: '你好！' }),
      event(4, 'turn/completed'),
    ]
    const pending = { message: '你好', afterSequence: 3 }
    expect(projectTurns([], null, pending)[0]).toMatchObject({ user: '你好', status: 'running' })
    expect(projectTurns(history, null, pending).map((turn) => [turn.user, turn.status])).toEqual([
      ['你好', 'completed'],
      ['你好', 'running'],
    ])

    const next = { turn_id: 'turn-2' }
    const started = [...history, event(5, 'turn/started', {}, next)]
    expect(projectTurns(started, null, pending).at(-1)).toMatchObject({
      id: 'turn-2',
      user: '你好',
      status: 'running',
    })
    const confirmed = [
      ...started,
      event(6, 'user/message', { content: '你好' }, next),
      event(7, 'assistant/message', { content: '有什么可以帮你？' }, next),
      event(8, 'turn/completed', {}, next),
    ]
    expect(projectTurns(confirmed, null, pending).map((turn) => [turn.id, turn.status])).toEqual([
      ['turn-1', 'completed'],
      ['turn-2', 'completed'],
    ])
  })
  it('reconciles deltas with final messages without duplicating text', () => {
    const [turn] = projectTurns([
      event(1, 'assistant/text_delta', { text: '你' }),
      event(2, 'assistant/text_delta', { text: '好' }),
      event(3, 'assistant/message', { content: '你好。' }),
      event(4, 'turn/completed'),
    ])
    expect(turn.messages).toEqual([{ id: 'step-1', text: '你好。', sequence: 1 }])
    expect(turn.status).toBe('completed')
  })
  it('renders the Runtime preview until the final message for its step arrives', () => {
    const events = [event(1, 'user/message', { content: 'hello' })]
    const preview = { turn_id: 'turn-1', step_id: 'step-1', text: 'First part' }

    expect(projectTurns(events, preview)[0].messages).toEqual([
      { id: 'step-1', text: 'First part', streaming: true },
    ])
    expect(
      projectTurns(
        [...events, event(2, 'assistant/message', { content: 'First part and final part.' })],
        preview,
      )[0].messages,
    ).toEqual([{ id: 'step-1', text: 'First part and final part.', sequence: 2 }])
  })
  it.each([
    event(3, 'agent/results_received', { delivery_ids: ['delivery-1'] }),
    event(
      3,
      'user/message',
      { content: 'internal result' },
      { source: 'subagent-delivery', step_id: null },
    ),
  ])('preserves the user and supersedes a draft only when a later answer arrives', (delivery) => {
    const history = [
      event(1, 'user/message', { content: '原始提问' }),
      event(2, 'assistant/message', { content: '汇总前的回答' }),
      delivery,
    ]
    expect(projectTurns(history)[0].messages[0].superseded).toBeUndefined()
    const [turn] = projectTurns([
      ...history,
      event(4, 'assistant/message', { content: '最终汇总' }, { step_id: 'step-2' }),
      event(5, 'turn/completed'),
    ])
    expect(turn.user).toBe('原始提问')
    expect(turn.messages.map((message) => [message.text, message.superseded])).toEqual([
      ['汇总前的回答', true],
      ['最终汇总', undefined],
    ])
  })
  it('waits for the next model step even when a previous answer is visible', () => {
    const history = [
      event(1, 'user/message', { content: '继续' }),
      event(2, 'assistant/message', { content: '上一段回复' }),
    ]
    expect(projectTurns(history)[0].replyPhase).toBe('ready')
    const nextStep = event(3, 'step/started', { mode: 'react' }, { step_id: 'step-2' })
    expect(projectTurns([...history, nextStep])[0].replyPhase).toBe('waiting')
    const preview = { turn_id: 'turn-1', step_id: 'step-2', text: '已' }
    expect(projectTurns([...history, nextStep], preview)[0].replyPhase).toBe('streaming')
  })
  it('matches parallel tools by call id instead of name', () => {
    const [turn] = projectTurns([
      event(1, 'tool/execution_started', { name: 'read_file' }, { tool_call_id: 'a' }),
      event(2, 'tool/execution_started', { name: 'read_file' }, { tool_call_id: 'b' }),
      event(3, 'tool/result', { name: 'read_file', content: 'B' }, { tool_call_id: 'b' }),
    ])
    expect(turn.tools.map((t) => [t.id, t.status])).toEqual([
      ['a', 'running'],
      ['b', 'completed'],
    ])
  })
  it('settles canceled tools and excludes child content from parent turns', () => {
    const turns = projectTurns([
      event(1, 'tool/execution_started', { name: 'bash' }),
      event(
        2,
        'assistant/message',
        { content: 'child output' },
        { source: 'child:1', turn_id: 'child-turn' },
      ),
      event(3, 'turn/canceled'),
    ])
    expect(turns).toHaveLength(1)
    expect(turns[0].status).toBe('canceled')
    expect(turns[0].tools[0].status).toBe('failed')
  })
  it('tracks approval decisions without declaring the turn completed', () => {
    const [turn] = projectTurns([
      event(1, 'approval/started', { command: 'pnpm test' }, { approval_id: 'a' }),
      event(2, 'approval/approved'),
    ])
    expect(turn.approvals?.[0].status).toBe('approved')
    expect(turn.status).toBe('running')
  })
  it('disables pending approvals when the turn is canceled', () => {
    const [turn] = projectTurns([
      event(1, 'approval/started', { command: 'pnpm test' }, { approval_id: 'a' }),
      event(2, 'turn/canceled'),
    ])
    expect(turn.approvals?.[0].status).toBe('canceled')
  })
  it('keeps concurrent approvals independently actionable and replays their outcomes', () => {
    const [turn] = projectTurns([
      event(
        1,
        'approval/started',
        { tool_name: 'bash', input: { command: 'Get-Date' } },
        {
          approval_id: 'a',
        },
      ),
      event(
        2,
        'approval/started',
        { tool_name: 'write_file', input: { path: 'report.md' } },
        {
          approval_id: 'b',
        },
      ),
      event(3, 'approval/rejected', {}, { approval_id: 'a' }),
    ])
    expect(turn.approvals).toEqual([
      expect.objectContaining({ id: 'a', status: 'rejected', input: { command: 'Get-Date' } }),
      expect.objectContaining({
        id: 'b',
        status: 'pending',
        toolName: 'write_file',
        input: { path: 'report.md' },
      }),
    ])
  })
})
