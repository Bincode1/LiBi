import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it, vi } from 'vitest'
import { Composer } from './composer'

function renderRunningComposer(unmanagedRun: boolean) {
  return renderToStaticMarkup(
    <Composer
      draft="下一步"
      setDraft={vi.fn()}
      mode="react"
      setMode={vi.fn()}
      permissionMode="default"
      setPermissionMode={vi.fn()}
      changingPermission
      busy
      unmanagedRun={unmanagedRun}
      submitting={false}
      syncing
      canStop={!unmanagedRun}
      stopping={false}
      onSend={vi.fn()}
      onStop={vi.fn()}
      onNewConversation={vi.fn()}
    />,
  )
}

it('keeps Stop enabled for a known run while the event feed is syncing', () => {
  const html = renderRunningComposer(false)
  const stopButton = html.match(/<button[^>]*aria-label="停止执行"[^>]*>/)?.[0]
  expect(stopButton).toBeDefined()
  expect(stopButton).not.toContain('disabled')
})

it('offers a new conversation for an unowned running history and keeps the draft', () => {
  const html = renderRunningComposer(true)
  expect(html).toContain('当前 Runtime 无法管理本轮运行')
  expect(html).toContain('新建会话')
  expect(html).toContain('下一步')
  expect(html).not.toContain('Agent 正在工作')
  expect(html).not.toContain('停止执行')
  expect(html).not.toContain('aria-label="发送消息"')
})
