import { afterEach, expect, it, vi } from 'vitest'
import { CapabilityError, liveClient, request } from './api'
afterEach(() => vi.unstubAllGlobals())
it('does not display arbitrary error response bodies', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(new Response('private-provider-diagnostic', { status: 500 })),
  )
  await expect(request('/v1/health')).rejects.toThrow('Runtime 请求失败（500）')
})
it('never sends unsupported operations to a guessed endpoint', async () => {
  const fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
  await expect(liveClient.changes('s')).rejects.toBeInstanceOf(CapabilityError)
  expect(fetchMock).not.toHaveBeenCalled()
})
it('shows safe model discovery failures without displaying provider diagnostics', async () => {
  vi.stubGlobal(
    'fetch',
    vi
      .fn()
      .mockResolvedValue(
        Response.json(
          { model_error: '模型服务拒绝访问（HTTP 401）。', error: 'private-provider-diagnostic' },
          { status: 422 },
        ),
      ),
  )
  await expect(request('/v1/settings/model/test')).rejects.toThrow('模型服务拒绝访问（HTTP 401）。')
})
it('requests cancellation of the active run in its own session', async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValue(new Response('{"cancel_requested":true}', { status: 202 }))
  vi.stubGlobal('fetch', fetchMock)
  await liveClient.cancelTurn('s/one', 'r/two')
  const [path, init] = fetchMock.mock.calls[0]
  expect(path).toBe('/v1/sessions/s%2Fone/runs/r%2Ftwo/cancel')
  expect(init.method).toBe('POST')
})
it('encodes session identifiers and posts the runtime mode', async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ run_id: 'r' })))
  vi.stubGlobal('fetch', fetchMock)
  await liveClient.send('s/one', 'hello', 'plan')
  expect(fetchMock.mock.calls[0][0]).toBe('/v1/sessions/s%2Fone/turns/async')
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
    message: 'hello',
    mode: 'plan',
    interactive_approval: true,
  })
})
it('submits an approval decision to its session and approval id', async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response('{}'))
  vi.stubGlobal('fetch', fetchMock)
  await liveClient.decideApproval('s/one', 'approval/two', 'deny')
  const [path, init] = fetchMock.mock.calls[0]
  expect(path).toBe('/v1/sessions/s%2Fone/approvals/approval%2Ftwo/decision')
  expect(init.method).toBe('POST')
  expect(JSON.parse(init.body)).toEqual({ decision: 'deny' })
})

it('changes the permission mode of only the selected session', async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response('{"mode":"auto"}'))
  vi.stubGlobal('fetch', fetchMock)
  await liveClient.setPermissionMode('s/one', 'auto')
  const [path, init] = fetchMock.mock.calls[0]
  expect(path).toBe('/v1/sessions/s%2Fone/permissions')
  expect(init.method).toBe('POST')
  expect(JSON.parse(init.body)).toEqual({ mode: 'auto' })
})

it('receives pages over SSE and opens a new connection at the delivered cursor', async () => {
  const streams: FakeEventSource[] = []
  class FakeEventSource extends EventTarget {
    onerror: (() => void) | null = null
    close = vi.fn()
    constructor(public url: string) {
      super()
      streams.push(this)
    }
  }
  vi.stubGlobal('EventSource', FakeEventSource)
  const controller = new AbortController()
  const receive = vi.fn()
  const connection = liveClient.subscribe('s/one', 3, controller.signal, receive)
  expect(streams[0].url).toBe('/v1/sessions/s%2Fone/events/stream?cursor=3&view=conversation')
  const page = {
    session_id: 's/one',
    next_cursor: 5,
    events: [],
    state: { status: 'running' },
    assistant_preview: { turn_id: 'turn', step_id: 'step', text: '第一段' },
  }
  streams[0].dispatchEvent(new MessageEvent('page', { data: JSON.stringify(page) }))
  expect(receive).toHaveBeenCalledWith(page)
  const disconnected = expect(connection).rejects.toThrow('实时连接已断开')
  streams[0].onerror?.()
  await disconnected
  expect(streams[0].close).toHaveBeenCalledOnce()

  const reconnected = liveClient.subscribe('s/one', page.next_cursor, controller.signal, receive)
  expect(streams[1].url).toBe('/v1/sessions/s%2Fone/events/stream?cursor=5&view=conversation')
  controller.abort()
  await reconnected
  expect(streams[1].close).toHaveBeenCalledOnce()
})
