import { afterEach, expect, it, vi } from 'vitest'
import { demoClient } from './demo'
import { projectTurns } from './events'
afterEach(() => vi.useRealTimers())
it('delivers each demo text chunk as it is generated', async () => {
  vi.useFakeTimers()
  const session = await demoClient.createSession()
  const controller = new AbortController()
  const receive = vi.fn()
  const subscription = demoClient.subscribe(session.id, 0, controller.signal, receive)
  await demoClient.send(session.id, '检查项目', 'react')
  await vi.advanceTimersByTimeAsync(450)
  expect(receive.mock.lastCall?.[0].events[0].type).toBe('assistant/text_delta')
  const firstCursor = receive.mock.lastCall?.[0].next_cursor
  await vi.advanceTimersByTimeAsync(65)
  expect(receive.mock.lastCall?.[0].next_cursor).toBe(firstCursor + 1)
  controller.abort()
  await subscription
  const delivered = receive.mock.calls.length
  await vi.runAllTimersAsync()
  expect(receive).toHaveBeenCalledTimes(delivered)
})

it('stops a simulated stream without letting later timers complete the turn', async () => {
  vi.useFakeTimers()
  const session = await demoClient.createSession()
  const run = await demoClient.send(session.id, '检查项目', 'react')
  await vi.advanceTimersByTimeAsync(600)
  await demoClient.cancelTurn(session.id, run.run_id)
  await vi.advanceTimersByTimeAsync(10_000)
  const page = await demoClient.events(session.id, 0)
  expect(page.state.status).toBe('canceled')
  expect(page.events.some((e) => e.type === 'turn/completed')).toBe(false)
})
it('waits for approval and rejects a repeated decision', async () => {
  vi.useFakeTimers()
  const session = await demoClient.createSession()
  await demoClient.send(session.id, '需要审批', 'plan')
  const page = await demoClient.events(session.id, 0)
  const approval = page.events.find((e) => e.type === 'approval/started')!
  expect(page.events.some((e) => e.type === 'assistant/text_delta')).toBe(false)
  await demoClient.decideApproval(session.id, approval.approval_id!, 'approve')
  await vi.runAllTimersAsync()
  const completed = await demoClient.events(session.id, 0)
  expect(projectTurns(completed.events)[0].status).toBe('completed')
  await expect(
    demoClient.decideApproval(session.id, approval.approval_id!, 'approve'),
  ).rejects.toThrow('审批已过期')
})

it('applies permission changes to the next turn without resolving a pending approval', async () => {
  vi.useFakeTimers()
  const session = await demoClient.createSession()
  const other = await demoClient.createSession()
  await demoClient.send(session.id, '需要审批', 'react')
  await demoClient.setPermissionMode(session.id, 'auto')
  await vi.runAllTimersAsync()
  const waiting = await demoClient.events(session.id, 0)
  expect(waiting.permission_mode).toBe('auto')
  expect(waiting.state.status).toBe('running')
  const [approval] = projectTurns(waiting.events)[0].approvals
  expect(approval.status).toBe('pending')
  expect((await demoClient.events(other.id, 0)).permission_mode).toBe('default')
  await demoClient.decideApproval(session.id, approval.id, 'deny')

  await demoClient.send(session.id, '需要审批', 'react')
  await vi.runAllTimersAsync()
  const next = await demoClient.events(session.id, waiting.next_cursor)
  expect(next.events.some((event) => event.type === 'approval/started')).toBe(false)
  expect(next.state.status).toBe('completed')

  await demoClient.setPermissionMode(session.id, 'default')
  const run = await demoClient.send(session.id, '需要审批', 'react')
  const restored = await demoClient.events(session.id, next.next_cursor)
  expect(restored.events.at(-1)?.type).toBe('approval/started')
  await demoClient.cancelTurn(session.id, run.run_id)
})

it('captures permission before publishing the start of a turn', async () => {
  const session = await demoClient.createSession()
  const controller = new AbortController()
  const subscription = demoClient.subscribe(session.id, 0, controller.signal, (page) => {
    if (page.events.some((event) => event.type === 'turn/started')) {
      void demoClient.setPermissionMode(session.id, 'auto')
    }
  })
  try {
    const run = await demoClient.send(session.id, '需要审批', 'react')
    const page = await demoClient.events(session.id, 0)
    await demoClient.cancelTurn(session.id, run.run_id)
    expect(page.permission_mode).toBe('auto')
    expect(projectTurns(page.events)[0].approvals[0]?.status).toBe('pending')
  } finally {
    controller.abort()
    await subscription
  }
})
