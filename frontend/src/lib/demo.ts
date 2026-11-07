import type {
  ChangeFile,
  EventPage,
  PermissionMode,
  RuntimeClient,
  RuntimeEvent,
  Session,
  Task,
} from './types'

const now = () => new Date().toISOString()
const uid = () => crypto.randomUUID()
export const demoChanges: ChangeFile[] = [
  {
    path: 'src/components/SessionList.tsx',
    added: 18,
    removed: 6,
    lines: [
      { kind: 'context', number: 12, text: 'export function SessionList({ sessions }: Props) {' },
      { kind: 'remove', number: 13, text: '  return sessions.map(session => (' },
      { kind: 'add', number: 13, text: '  const groups = groupByDate(sessions)' },
      { kind: 'add', number: 14, text: '' },
      { kind: 'add', number: 15, text: '  return groups.map(({ label, items }) => (' },
      { kind: 'add', number: 16, text: '    <section key={label} className="session-group">' },
      { kind: 'add', number: 17, text: '      <h3>{label}</h3>' },
      { kind: 'add', number: 18, text: '      {items.map(session => (' },
      {
        kind: 'context',
        number: 19,
        text: '        <SessionItem key={session.id} session={session} />',
      },
      { kind: 'add', number: 20, text: '      ))}' },
      { kind: 'add', number: 21, text: '    </section>' },
      { kind: 'context', number: 22, text: '  ))' },
      { kind: 'context', number: 23, text: '}' },
    ],
  },
  {
    path: 'src/lib/date.ts',
    added: 24,
    removed: 0,
    lines: [
      { kind: 'add', number: 1, text: 'export function groupByDate(sessions: Session[]) {' },
      { kind: 'add', number: 2, text: '  const today = new Date().toDateString()' },
      { kind: 'add', number: 3, text: '  return groupSessions(sessions, today)' },
      { kind: 'add', number: 4, text: '}' },
    ],
  },
  {
    path: 'src/components/SessionList.test.tsx',
    added: 32,
    removed: 0,
    lines: [
      { kind: 'add', number: 1, text: "it('groups sessions by local date', () => {" },
      { kind: 'add', number: 2, text: '  const groups = groupByDate(fixtures)' },
      { kind: 'add', number: 3, text: '  expect(groups).toHaveLength(3)' },
      { kind: 'add', number: 4, text: '})' },
    ],
  },
]
const sessions: Session[] = [
  ['demo-workspace', '让会话列表更有条理'],
  ['demo-architecture', '梳理 Agent 的运行流程'],
  ['demo-approval', '为工具执行增加确认步骤'],
  ['demo-notes', '整理项目的开发文档'],
].map(([id, title], index) => ({
  id,
  title,
  status: 'completed',
  last_sequence: 0,
  created_at: new Date(Date.now() - index * 86400000).toISOString(),
  updated_at: new Date(Date.now() - index * 86400000).toISOString(),
}))
const logs = new Map<string, RuntimeEvent[]>()
const listeners = new Map<string, Set<() => void>>()
const runs = new Map<
  string,
  { sessionId: string; turnId: string; timer?: ReturnType<typeof setTimeout> }
>()
const pending = new Map<string, () => void>()
const permissionModes = new Map<string, PermissionMode>()
const tasks: Task[] = [
  {
    id: 'demo-task-1',
    prompt: '检查工具执行器的错误处理',
    mode: 'react',
    status: 'completed',
    result: '演示结果：已检查错误传播和审批拒绝分支。',
    created_at: now(),
    updated_at: now(),
  },
]
function append(
  id: string,
  type: string,
  payload: Record<string, unknown>,
  turn = 'demo-turn-1',
  extra: Partial<RuntimeEvent> = {},
) {
  const events = logs.get(id) ?? []
  const event: RuntimeEvent = {
    id: uid(),
    sequence: (events.at(-1)?.sequence ?? 0) + 1,
    type,
    payload,
    created_at: now(),
    turn_id: turn,
    step_id: turn + '-step',
    ...extra,
  }
  events.push(event)
  logs.set(id, events)
  const session = sessions.find((s) => s.id === id)
  if (session) {
    session.last_sequence = event.sequence
    if (type !== 'session/title_updated') session.updated_at = now()
    if (type === 'turn/started') session.status = 'running'
    if (['turn/completed', 'turn/canceled', 'turn/failed'].includes(type))
      session.status = type.split('/')[1]
  }
  listeners.get(id)?.forEach((publish) => publish())
}

function eventPage(id: string, cursor: number): EventPage {
  const events = logs.get(id) ?? []
  const session = sessions.find((item) => item.id === id)
  return {
    session_id: id,
    events: events.slice(cursor),
    next_cursor: Math.max(events.length, cursor),
    state: { status: session?.status ?? 'idle', title: session?.title },
    permission_mode: permissionModes.get(id) ?? 'default',
  }
}
append('demo-workspace', 'turn/started', {})
append('demo-workspace', 'user/message', {
  content: '帮我优化一下会话列表，按时间分组，让最近的工作更容易找到。',
})
append('demo-workspace', 'tool/execution_started', { name: 'read_file' }, undefined, {
  tool_call_id: 'read-1',
})
append(
  'demo-workspace',
  'tool/result',
  { name: 'read_file', content: '已读取 SessionList.tsx、会话数据类型与现有样式。' },
  undefined,
  { tool_call_id: 'read-1' },
)
append('demo-workspace', 'tool/execution_started', { name: 'write_file' }, undefined, {
  tool_call_id: 'write-1',
})
append(
  'demo-workspace',
  'tool/result',
  {
    name: 'write_file',
    content: '演示变更：SessionList.tsx、date.ts、SessionList.test.tsx。真实文件未修改。',
  },
  undefined,
  { tool_call_id: 'write-1' },
)
append('demo-workspace', 'tool/execution_started', { name: 'bash' }, undefined, {
  tool_call_id: 'test-1',
})
append(
  'demo-workspace',
  'tool/result',
  {
    name: 'bash',
    content: '$ pnpm test\n\n✓ SessionList.test.tsx (3 tests)\n\n演示输出，不代表实际运行结果。',
  },
  undefined,
  { tool_call_id: 'test-1' },
)
append('demo-workspace', 'assistant/message', {
  content:
    '会话列表已经整理好了。现在可以更自然地找到最近的工作。\n\n### 这次调整\n\n- 按 **今天、昨天、更早** 分组，组内按最近活跃时间排序。\n- 保留当前会话的选中状态，长标题自动截断。\n- 为日期分组补充了边界情况测试。\n\n右侧可以查看 **3 个文件** 的示例变更，点击文件即可展开差异。\n\n> 这是交互演示，所有文件变更和测试输出均为示例，未修改你的项目。',
})
append('demo-workspace', 'request/completed', { usage: { total_tokens: 2846 } })
append('demo-workspace', 'turn/completed', {})
for (const [id, user, answer] of [
  [
    'demo-architecture',
    '帮我梳理一下 Agent 的运行流程。',
    '### 从请求到结果\n\n用户输入通过 Runtime 进入 `QueryEngine`，由 Agent 组织上下文并请求模型。模型选择工具后，工具执行器完成权限检查、审批和执行，再把结果交回下一轮模型请求。\n\n会话事件写入 `SessionStore`，前端通过事件投影展示执行进度。\n\n这是基于项目结构编写的演示会话。',
  ],
  [
    'demo-notes',
    '整理一下项目文档应该包含哪些内容。',
    '建议从三个入口组织项目文档：\n\n1. **开始使用**：环境、安装与第一次运行。\n2. **理解架构**：会话、事件、工具和模型请求。\n3. **参与开发**：测试方式、代码规范与接口约定。\n\n这是演示建议，尚未创建文件。',
  ],
]) {
  append(id, 'user/message', { content: user })
  append(id, 'assistant/message', { content: answer })
  append(id, 'turn/completed', {})
}
append('demo-approval', 'user/message', { content: '运行测试前，先让我确认一下要执行的命令。' })
append('demo-approval', 'approval/started', { command: 'pnpm test' }, undefined, {
  approval_id: 'approval-seed',
})
sessions.find((s) => s.id === 'demo-approval')!.status = 'running'
pending.set('approval-seed', () => {
  append('demo-approval', 'assistant/message', {
    content: '演示审批已通过。',
  })
  append('demo-approval', 'turn/completed', {})
})
sessions.forEach((session, index) => {
  session.updated_at = new Date(Date.now() - index * 86400000).toISOString()
})

export const demoClient: RuntimeClient = {
  capabilities: { cancelTurn: true, approvals: true, changes: true, modelSelection: false },
  health: async () => ({ status: 'ok', cwd: 'PaiCLI-Python' }),
  sessions: async () =>
    sessions.map((s) => ({ ...s })).sort((a, b) => b.updated_at.localeCompare(a.updated_at)),
  createSession: async () => {
    const session: Session = {
      id: 'demo-' + uid(),
      status: 'idle',
      created_at: now(),
      updated_at: now(),
      last_sequence: 0,
    }
    sessions.unshift(session)
    return { ...session }
  },
  renameSession: async (id, title) => {
    const session = sessions.find((item) => item.id === id)
    if (!session) throw new Error('演示会话不存在。')
    session.title = title
    append(id, 'session/title_updated', { title }, undefined, { turn_id: null, step_id: null })
    return { title }
  },
  events: async (id, cursor) => eventPage(id, cursor),
  subscribe: (id, cursor, signal, receive) =>
    new Promise<void>((resolve) => {
      if (signal.aborted) {
        resolve()
        return
      }
      const subscribers = listeners.get(id) ?? new Set<() => void>()
      const publish = () => {
        const page = eventPage(id, cursor)
        cursor = page.next_cursor
        receive(page)
      }
      subscribers.add(publish)
      listeners.set(id, subscribers)
      signal.addEventListener(
        'abort',
        () => {
          subscribers.delete(publish)
          if (subscribers.size === 0) listeners.delete(id)
          resolve()
        },
        { once: true },
      )
      publish()
    }),
  send: async (id, message, mode) => {
    const session = sessions.find((s) => s.id === id)
    if (!session) throw new Error('演示会话不存在，请新建会话。')
    if (session.status === 'running') throw new Error('当前会话仍在运行。')
    const permissionMode = permissionModes.get(id) ?? 'default'
    const runId = uid(),
      turnId = uid()
    session.title ??= message.slice(0, 22)
    append(id, 'turn/started', { mode }, turnId)
    append(id, 'user/message', { content: message }, turnId)
    const run = {
      sessionId: id,
      turnId,
      timer: undefined as ReturnType<typeof setTimeout> | undefined,
    }
    runs.set(runId, run)
    const start = () => {
      const answer = `收到你的请求：**${message.replace(/[\r\n]/g, ' ').slice(0, 150)}**。\n\n${mode === 'plan' ? '### 执行计划\n\n1. 检查相关模块与现有约束。\n2. 完成改动并运行相关验证。\n3. 汇总结果与待处理问题。\n\n' : ''}当前是 **交互演示模式**，你可以体验分段输出、停止执行和会话切换。\n\n切换到「连接 Runtime」后，请求会发送给本地 Python Agent；当前不会调用模型或修改项目文件。`
      let offset = 0
      const tick = () => {
        append(id, 'assistant/text_delta', { text: answer.slice(offset, offset + 8) }, turnId)
        offset += 8
        if (offset < answer.length) run.timer = setTimeout(tick, 65)
        else {
          append(id, 'assistant/message', { content: answer }, turnId)
          append(id, 'turn/completed', {}, turnId)
          runs.delete(runId)
        }
      }
      run.timer = setTimeout(tick, 450)
    }
    if (/审批|确认命令/.test(message) && permissionMode !== 'auto') {
      const approvalId = uid()
      pending.set(approvalId, start)
      append(id, 'approval/started', { command: 'pnpm test' }, turnId, { approval_id: approvalId })
    } else start()
    return { run_id: runId }
  },
  setPermissionMode: async (id, mode) => {
    if (!sessions.some((session) => session.id === id)) throw new Error('演示会话不存在。')
    permissionModes.set(id, mode)
    listeners.get(id)?.forEach((publish) => publish())
    return { mode }
  },
  cancelTurn: async (id, runId) => {
    const run = runs.get(runId)
    if (!run || run.sessionId !== id) throw new Error('此运行无法停止，请刷新状态。')
    clearTimeout(run.timer)
    runs.delete(runId)
    for (const event of logs.get(id) ?? [])
      if (event.turn_id === run.turnId && event.approval_id) pending.delete(event.approval_id)
    append(id, 'turn/canceled', {}, run.turnId)
  },
  decideApproval: async (id, approvalId, decision) => {
    const resume = pending.get(approvalId)
    const event = logs.get(id)?.find((e) => e.approval_id === approvalId)
    if (!resume || !event) throw new Error('审批已过期或已处理。')
    pending.delete(approvalId)
    append(
      id,
      decision === 'approve' ? 'approval/approved' : 'approval/rejected',
      {},
      event.turn_id ?? undefined,
      { approval_id: approvalId },
    )
    if (decision === 'approve') resume()
    else {
      append(id, 'turn/canceled', {}, event.turn_id ?? undefined)
      for (const [key, run] of runs)
        if (run.sessionId === id) {
          clearTimeout(run.timer)
          runs.delete(key)
        }
    }
  },
  changes: async (id) => (id === 'demo-workspace' ? demoChanges : []),
  tasks: async () => tasks.map((t) => ({ ...t })),
  addTask: async (prompt, mode) => {
    tasks.unshift({
      id: uid(),
      prompt,
      mode,
      status: 'queued',
      created_at: now(),
      updated_at: now(),
    })
  },
  cancelTask: async (id) => {
    const task = tasks.find((t) => t.id === id)
    if (!task) throw new Error('任务不存在。')
    task.status = 'canceled'
    task.updated_at = now()
  },
}
