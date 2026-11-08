import { QueryClient, QueryClientProvider, QueryObserver } from '@tanstack/react-query'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import App from './App'
import { liveClient } from './lib/api'

afterEach(() => vi.unstubAllGlobals())

it.each([
  [401, 'Runtime 鉴权失败'],
  [502, '无法连接 Runtime'],
])('keeps the offline UI visible while rechecking after HTTP %i', async (status, message) => {
  vi.stubGlobal('localStorage', { getItem: () => null })
  vi.stubGlobal('matchMedia', () => ({ matches: false }))
  const fetch = vi.fn<typeof globalThis.fetch>()
  vi.stubGlobal('fetch', fetch)
  const client = new QueryClient()
  const render = () =>
    renderToStaticMarkup(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/sessions/test-session']}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    )

  expect(render()).toContain('连接中')
  const health = new QueryObserver(client, {
    queryKey: ['health'],
    queryFn: ({ signal }) => liveClient.health(signal),
    enabled: false,
    retry: false,
  })
  const expectOffline = () => {
    const html = render()
    expect(html).toContain('连接异常')
    expect(html).toContain(message)
    expect(html).not.toContain('正在恢复会话')
    expect(html).not.toContain('加载会话')
  }

  try {
    fetch.mockResolvedValueOnce(new Response(null, { status }))
    await health.refetch()

    for (const nextStatus of [status, 200]) {
      let respond!: (response: Response) => void
      fetch.mockImplementationOnce(() => new Promise((resolve) => (respond = resolve)))
      const recheck = health.refetch()
      expectOffline()
      respond(Response.json({ status: 'ok', cwd: 'D:/project' }, { status: nextStatus }))
      await recheck
    }

    const recovered = render()
    expect(recovered).not.toContain(message)
    expect(recovered).toContain('正在恢复会话')
  } finally {
    client.clear()
  }
})
