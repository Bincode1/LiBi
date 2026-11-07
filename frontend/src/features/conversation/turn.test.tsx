import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it, vi } from 'vitest'
import { projectTurns } from '@/lib/events'
import type { AssistantPreview, RuntimeEvent } from '@/lib/types'
import { Turn } from './turn'

it('animates the live draft and keeps raw HTML out of the reply', () => {
  const render = (status: string, text: string, streaming?: boolean) =>
    renderToStaticMarkup(
      <Turn
        turn={{
          id: 'turn-1',
          user: '你好',
          status,
          replyPhase: streaming ? 'streaming' : 'ready',
          messages: [{ id: 'step-1', text, streaming }],
          tools: [],
          approvals: [],
          tokens: 0,
        }}
        canApprove={false}
        onApprove={vi.fn()}
        onInspect={vi.fn()}
        notify={vi.fn()}
      />,
    )

  const draft = render('running', '**你好', true)
  expect(draft).toContain('data-streamdown="strong"')
  expect(draft).toContain('data-sd-animate')
  expect(draft).toContain('正在生成回复')

  const final = render('completed', '**你好**\n\n<img src="https://example.invalid/pixel">')
  expect(final).toContain('data-streamdown="strong">你好</span>')
  expect(final).not.toContain('data-sd-animate')
  expect(final).not.toContain('<img')
})

it('groups handled approvals behind one closed disclosure and leaves pending requests visible', () => {
  const html = renderToStaticMarkup(
    <Turn
      turn={{
        id: 'turn-approvals',
        user: '检查项目',
        status: 'running',
        replyPhase: 'waiting',
        messages: [],
        tools: [],
        tokens: 0,
        approvals: [
          { id: 'a', status: 'approved', command: 'first-command' },
          { id: 'b', status: 'rejected', command: 'second-command' },
          { id: 'c', status: 'pending', command: 'pending-command' },
        ],
      }}
      canApprove
      onApprove={vi.fn()}
      onInspect={vi.fn()}
      notify={vi.fn()}
    />,
  )
  expect(html).toContain('已处理 2 项审批')
  const history = html.slice(html.indexOf('<details'))
  expect(history).not.toMatch(/^<details[^>]*\bopen(?:[\s=>])/)
  expect(history).not.toContain('first-command')
  expect(history).not.toContain('pending-command')
  expect(html.indexOf('pending-command')).toBeLessThan(html.indexOf('<details'))
  expect(html).not.toContain('等待模型响应')
})

function renderEvents(type: string, payload: RuntimeEvent['payload'], preview?: AssistantPreview) {
  const turn = projectTurns(
    [{ id: 'event-1', sequence: 1, type, payload, created_at: '', turn_id: 'turn-1' }],
    preview,
  )[0]
  return turn
    ? renderToStaticMarkup(
        <Turn
          turn={turn}
          canApprove={false}
          onApprove={vi.fn()}
          onInspect={vi.fn()}
          notify={vi.fn()}
        />,
      )
    : ''
}

it.each([
  [{ provider_error: { kind: 'http', status_code: 503 } }, 'HTTP 503'],
  [{ provider_error: { kind: 'timeout' } }, '等待模型响应超时'],
  [{ provider_error: { kind: 'connection' } }, '无法连接模型服务'],
  [{ provider_error: { kind: 'protocol' } }, '模型回复不完整或格式异常'],
  [{ reason: 'provider_error' }, '模型服务请求失败'],
  [{ reason: 'unexpected_error' }, '本轮执行失败'],
])('reports the reason for a failed turn with no tools (%j)', (payload, expected) => {
  const html = renderEvents('turn/failed', payload)
  expect(html).toContain(expected)
  expect(html).not.toContain('展开执行')
})

it('shows model progress after a tool and while the reply pauses after its first character', () => {
  const payload = { name: 'read_file', content: 'Read test.md' }
  expect(renderEvents('tool/execution_started', payload)).toContain('正在读取项目文件')
  const waiting = renderEvents('tool/result', payload)
  expect(waiting).toContain('已执行 1 个步骤')
  expect(waiting).toContain('等待模型响应')
  expect(waiting).not.toContain('class="markdown"')

  const draft = renderEvents('tool/result', payload, {
    turn_id: 'turn-1',
    step_id: 'step-2',
    text: '已',
  })
  expect(draft).toContain('data-sd-animate')
  expect(draft).toMatch(/>已<\/span>/)
  expect(draft).toContain('正在生成回复')
  expect(draft).not.toContain('等待模型响应')

  const final = renderEvents('assistant/message', { content: '已读完。' })
  expect(final).toContain('已读完。')
  expect(final).not.toContain('thinking-label')
})
