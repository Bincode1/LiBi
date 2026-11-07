import type { ActiveRun, AssistantPreview, RuntimeEvent } from './types'
export interface ToolView {
  id: string
  name: string
  content: string
  status: 'running' | 'completed' | 'failed'
  sequence?: number
  stepId?: string | null
}
export interface ApprovalView {
  id: string
  toolName?: string
  command?: string
  input?: Record<string, unknown>
  expiresAt?: string
  agentId?: string
  agentSessionId?: string
  agentLabel?: string
  status: 'pending' | 'approved' | 'rejected' | 'canceled'
}
interface MessageView {
  id: string
  text: string
  streaming?: boolean
  commentary?: boolean
  sequence?: number
  superseded?: boolean
}
export interface TurnView {
  id: string
  user: string
  messages: MessageView[]
  steering?: { id: string; text: string; sequence: number }[]
  tools: ToolView[]
  status: string
  replyPhase: 'starting' | 'waiting' | 'streaming' | 'ready'
  failureMessage?: string
  approvals: ApprovalView[]
  tokens: number
  optimistic?: boolean
}
export interface PendingTurn {
  message: string
  afterSequence: number
  run?: { id: string; turnId?: string; acceptedAt: number }
}
const terminalEvents = new Set(['turn/completed', 'turn/failed', 'turn/canceled'])

function findPendingStart(events: RuntimeEvent[], pending: PendingTurn) {
  return events.find(
    (event) =>
      event.type === 'turn/started' &&
      event.turn_id &&
      (pending.run?.turnId
        ? event.turn_id === pending.run.turnId
        : event.sequence > pending.afterSequence) &&
      !event.source?.startsWith('child:'),
  )
}

export function resolveActiveRun(
  feed: {
    activeRun?: ActiveRun | null
    cursor: number
    events: RuntimeEvent[]
    connectedAt?: number
    lastSeenRunId?: string
  },
  pending?: PendingTurn,
): ActiveRun | undefined {
  const active = feed.activeRun ?? undefined
  if (!pending?.run || active?.run_id === pending.run.id) return active
  const { afterSequence, run } = pending
  // A buffered frame may describe the previous run. Only older runtimes need cursor inference.
  if (
    feed.activeRun !== undefined &&
    ((!run.turnId && feed.cursor > afterSequence) ||
      feed.lastSeenRunId === run.id ||
      (feed.connectedAt ?? 0) > run.acceptedAt)
  ) {
    return active
  }
  const start = findPendingStart(feed.events, pending)
  if (
    start &&
    feed.events.some(
      (event) =>
        event.sequence > start.sequence &&
        event.turn_id === start.turn_id &&
        terminalEvents.has(event.type),
    )
  ) {
    return active
  }
  return { run_id: run.id, status: 'running' }
}

export function mergeEvents(previous: RuntimeEvent[], incoming: RuntimeEvent[]) {
  const map = new Map(previous.map((e) => [e.id, e]))
  for (const event of incoming) map.set(event.id, event)
  return [...map.values()].sort((a, b) => a.sequence - b.sequence)
}
export function projectTurns(
  events: RuntimeEvent[],
  preview?: AssistantPreview | null,
  pending?: PendingTurn,
): TurnView[] {
  const turns = new Map<string, TurnView>()
  const awaitingReplacement = new Map<string, MessageView>()
  for (const event of events) {
    if (!event.turn_id || event.source?.startsWith('child:')) continue
    let turn = turns.get(event.turn_id)
    if (!turn) {
      turn = {
        id: event.turn_id,
        user: '',
        messages: [],
        tools: [],
        approvals: [],
        status: 'running',
        replyPhase: 'waiting',
        tokens: 0,
      }
      turns.set(turn.id, turn)
    }
    const p = event.payload
    if (
      event.type === 'agent/results_received' ||
      (event.type === 'user/message' && event.source === 'subagent-delivery')
    ) {
      const previous = event.step_id
        ? turn.messages.find((message) => message.id === event.step_id && !message.commentary)
        : [...turn.messages].reverse().find((message) => !message.commentary && !message.streaming)
      if (previous) awaitingReplacement.set(turn.id, previous)
    } else if (event.type === 'user/message') {
      if (event.source === 'subagent-steer') {
        const steering = (turn.steering ??= [])
        steering.push({
          id: event.id,
          text: String(p.content ?? ''),
          sequence: event.sequence,
        })
      } else if (!turn.user) turn.user = String(p.content ?? '')
    }
    if (event.type === 'step/started') turn.replyPhase = 'waiting'
    if (event.type === 'assistant/message') {
      const step = event.step_id ?? event.id
      const commentary = Array.isArray(p.tool_calls) && p.tool_calls.length > 0
      const previous = awaitingReplacement.get(turn.id)
      if (previous && previous.id !== step && !commentary && p.content) {
        previous.superseded = true
        awaitingReplacement.delete(turn.id)
      }
      turn.replyPhase = commentary ? 'waiting' : 'ready'
      const message = turn.messages.find((m) => m.id === step)
      if (message) {
        message.text = String(p.content ?? '')
        delete message.streaming
        if (commentary) message.commentary = true
      } else if (p.content) {
        turn.messages.push({
          id: step,
          text: String(p.content),
          sequence: event.sequence,
          ...(commentary && { commentary }),
        })
      }
    }
    if (event.type === 'assistant/text_delta') {
      turn.replyPhase = 'streaming'
      const step = event.step_id ?? turn.id
      let message = turn.messages.find((m) => m.id === step)
      if (!message) {
        message = { id: step, text: '', sequence: event.sequence }
        turn.messages.push(message)
      }
      message.text += String(p.text ?? '')
      message.streaming = true
    }
    if (event.type === 'tool/execution_started' || event.type === 'tool/result') {
      const id = event.tool_call_id ?? event.id
      let tool = turn.tools.find((t) => t.id === id)
      if (!tool) {
        tool = {
          id,
          name: String(p.name ?? 'tool'),
          content: '',
          status: 'running',
          sequence: event.sequence,
          stepId: event.step_id,
        }
        turn.tools.push(tool)
      }
      if (event.type === 'tool/result') {
        tool.content = String(p.content ?? '')
        tool.status = p.is_error ? 'failed' : 'completed'
        turn.replyPhase = 'waiting'
      }
    }
    if (event.type === 'approval/started') {
      const command = p.command ?? p.description
      turn.approvals.push({
        id: event.approval_id ?? event.id,
        toolName: typeof p.tool_name === 'string' ? p.tool_name : undefined,
        command: typeof command === 'string' ? command : undefined,
        input:
          p.input && typeof p.input === 'object' && !Array.isArray(p.input)
            ? (p.input as Record<string, unknown>)
            : undefined,
        expiresAt: typeof p.expires_at === 'string' ? p.expires_at : undefined,
        agentId: typeof p.agent_id === 'string' && p.agent_id !== 'root' ? p.agent_id : undefined,
        agentSessionId: typeof p.agent_session_id === 'string' ? p.agent_session_id : undefined,
        agentLabel: typeof p.agent_label === 'string' ? p.agent_label : undefined,
        status: 'pending',
      })
    }
    const outcome = approvalOutcomes.get(event.type)
    if (outcome) {
      const approval = event.approval_id
        ? turn.approvals.find((item) => item.id === event.approval_id)
        : turn.approvals.at(-1)
      if (approval?.status === 'pending') approval.status = outcome
    }
    if (event.type === 'request/completed' && p.usage && typeof p.usage === 'object')
      turn.tokens += Number((p.usage as Record<string, unknown>).total_tokens ?? 0)
    if (terminalEvents.has(event.type)) {
      turn.status = event.type.split('/')[1]
      if (turn.status === 'failed') turn.failureMessage = providerFailureMessage(p)
      for (const approval of turn.approvals)
        if (approval.status === 'pending') approval.status = 'canceled'
      for (const tool of turn.tools)
        if (tool.status === 'running') {
          tool.status = 'failed'
          tool.content ||= '执行已结束，未收到工具结果。'
        }
    }
  }
  if (preview) {
    const turn = turns.get(preview.turn_id)
    if (turn?.status === 'running' && !turn.messages.some((m) => m.id === preview.step_id)) {
      turn.messages.push({ id: preview.step_id, text: preview.text, streaming: true })
      turn.replyPhase = 'streaming'
    }
  }
  if (pending) {
    const start = findPendingStart(events, pending)
    const turn = start?.turn_id ? turns.get(start.turn_id) : undefined
    if (turn && !turn.user) {
      turn.user = pending.message
      turn.optimistic = true
    } else if (!turn) {
      turns.set('pending', {
        id: 'pending',
        user: pending.message,
        status: 'running',
        replyPhase: 'starting',
        messages: [],
        tools: [],
        approvals: [],
        tokens: 0,
        optimistic: true,
      })
    }
  }
  return [...turns.values()].filter(
    (t) =>
      t.user || t.messages.length || t.tools.length || t.approvals.length || t.status === 'failed',
  )
}

function providerFailureMessage(payload: RuntimeEvent['payload']) {
  if (payload.reason === 'max_output_tokens') return '模型输出达到长度上限，本步工具未执行。'
  const error = payload.provider_error
  if (error && typeof error === 'object') {
    const { kind, code, status_code: status } = error as Record<string, unknown>
    if (code === 'max_output_tokens') return '模型输出达到长度上限，计划未执行。'
    if (kind === 'timeout') return '等待模型响应超时，请重试。'
    if (kind === 'connection') return '无法连接模型服务，请检查网络或模型服务地址。'
    if (kind === 'protocol') return '模型回复不完整或格式异常，请重试。'
    if (
      kind === 'http' &&
      typeof status === 'number' &&
      Number.isInteger(status) &&
      status >= 300 &&
      status <= 599
    ) {
      return `模型服务请求失败（HTTP ${status}）。`
    }
    return '模型服务请求失败。'
  }
  if (payload.reason === 'provider_error') return '模型服务请求失败。'
}

const approvalOutcomes = new Map<string, ApprovalView['status']>([
  ['approval/approved', 'approved'],
  ['approval/rejected', 'rejected'],
  ['approval/canceled', 'canceled'],
])
