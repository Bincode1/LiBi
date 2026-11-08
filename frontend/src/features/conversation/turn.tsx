import { useId, useState, type ReactNode } from 'react'
import { Check, ChevronRight, CircleAlert, Clock3, Copy, LoaderCircle } from 'lucide-react'
import * as m from 'motion/react-m'
import { Disclosure, DisclosureChevron, Reveal } from '@/components/ui/disclosure'
import { subagentLaunchFailure, toolLabel } from '@/lib/activity'
import type { ToolView, TurnView } from '@/lib/events'
import type { Subagent } from '@/lib/types'
import { useSurfaceMotion } from '@/lib/motion'
import { errorMessage } from '@/lib/utils'
import { AgentMark } from './agent-avatar'
import { ApprovalCard } from './approval'
import { ConversationMarkdown } from './markdown'
import { Delegation } from './subagent-activity'
import { TurnUsageSummary } from './turn-usage'

const delegationTools = new Set(['subagent', 'spawn_subagent'])
const coordinationTools = new Set(['list_agents', 'send_message', 'interrupt_agent'])
const replyProgress = {
  starting: '正在启动 Agent',
  waiting: '等待模型响应',
  streaming: '正在生成回复',
  ready: '',
}

function ToolRow({
  tool,
  unmanaged,
  onInspect,
}: {
  tool: ToolView
  unmanaged: boolean
  onInspect: (tool: ToolView) => void
}) {
  return (
    <button className="tool-row" onClick={() => onInspect(tool)}>
      <span className={`tool-state ${tool.status}`}>
        {tool.status === 'running' ? (
          unmanaged ? (
            <Clock3 size={14} />
          ) : (
            <LoaderCircle className="spin" size={14} />
          )
        ) : tool.status === 'failed' ? (
          <CircleAlert size={14} />
        ) : (
          <Check size={14} />
        )}
      </span>
      <span>{toolLabel(tool.name)}</span>
      <ChevronRight size={13} />
    </button>
  )
}
export function Turn({
  turn,
  unmanaged = false,
  canApprove,
  onApprove,
  onInspect,
  notify,
  agents = [],
  onOpenAgent,
  agentLabel,
}: {
  turn: TurnView
  unmanaged?: boolean
  canApprove: boolean
  onApprove: (id: string, decision: 'approve' | 'deny') => Promise<void>
  onInspect: (tool: ToolView) => void
  notify: (message: string) => void
  agents?: Subagent[]
  onOpenAgent?: (agent: Subagent) => void
  agentLabel?: (id: string) => string | undefined
}) {
  const [expanded, setExpanded] = useState(false)
  const processId = useId()
  const [copied, setCopied] = useState(false)
  const approvalMotion = useSurfaceMotion({ y: 5 }, 0.17)
  const statusMotion = useSurfaceMotion({}, 0.12)
  const pendingApprovals = turn.approvals.filter((approval) => approval.status === 'pending')
  const pending = pendingApprovals.length > 0
  const handledApprovals = turn.approvals.filter((approval) => approval.status !== 'pending')
  const deniedCount = handledApprovals.filter((approval) => approval.status === 'rejected').length
  const failedDelegations = turn.tools.filter(
    (tool) => delegationTools.has(tool.name) && tool.status === 'failed',
  )
  const tools = turn.tools.filter(
    (tool) =>
      !delegationTools.has(tool.name) ||
      (tool.status !== 'failed' &&
        !agents.some(
          (agent) =>
            agent.tool_call_id === tool.id ||
            (!agent.tool_call_id && agent.parent_step_id && agent.parent_step_id === tool.stepId),
        )),
  )
  const workTools = tools.filter((tool) => !coordinationTools.has(tool.name))
  const activeTool = workTools.find((tool) => tool.status === 'running')
  const waitingForDelegation = turn.tools.some(
    (tool) =>
      tool.status === 'running' && (delegationTools.has(tool.name) || tool.name === 'send_message'),
  )
  const drafts = turn.messages.filter((message) => message.superseded)
  const answers = turn.messages.filter((message) => !message.commentary && !message.superseded)
  const canCopyAnswer = turn.status === 'completed' && answers.length > 0
  const working = turn.status === 'running' && !unmanaged && !pending
  const finishedUsage = turn.status !== 'running' ? turn.usage : undefined
  const progress =
    working && !activeTool && !waitingForDelegation ? replyProgress[turn.replyPhase] : ''
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(answers.map((m) => m.text).join('\n\n'))
      setCopied(true)
    } catch (error) {
      notify(errorMessage(error))
    }
  }
  const timeline: { key: string; sequence: number; content: ReactNode }[] = turn.messages
    .filter((message) => !message.superseded)
    .map((message) => ({
      key: message.id,
      sequence: message.sequence ?? Infinity,
      content: (
        <ConversationMarkdown
          text={message.text}
          commentary={message.commentary}
          streaming={turn.status === 'running' && !unmanaged && message.streaming === true}
        />
      ),
    }))
  if (onOpenAgent) {
    const batches = new Map<string, Subagent[]>()
    for (const agent of agents) {
      const key = agent.parent_step_id ?? agent.id
      const batch = batches.get(key) ?? []
      batch.push(agent)
      batches.set(key, batch)
    }
    for (const [key, batch] of batches) {
      timeline.push({
        key: `delegation-${key}`,
        sequence: batch[0].sequence,
        content: <Delegation agents={batch} onOpen={onOpenAgent} />,
      })
    }
  }
  for (const failure of failedDelegations) {
    timeline.push({
      key: `failed-${failure.id}`,
      sequence: failure.sequence ?? Infinity,
      content: (
        <button className="delegation-failure" onClick={() => onInspect(failure)}>
          <CircleAlert size={14} aria-hidden="true" />子 agent 启动失败 ·{' '}
          {subagentLaunchFailure(failure.content)}
          <ChevronRight size={14} aria-hidden="true" />
        </button>
      ),
    })
  }
  timeline.sort((a, b) => a.sequence - b.sequence)
  return (
    <article className="turn">
      {turn.user && (
        <div className="user-message">
          <div>
            <div className="sr-only">你</div>
            <p>{turn.user}</p>
          </div>
        </div>
      )}
      <div className="agent-message">
        <AgentMark
          variant={working ? 'working' : turn.status === 'completed' ? 'done' : 'avatar'}
        />
        <div className="agent-body">
          <div className="message-label">LiBi</div>
          {(tools.length > 0 || drafts.length > 0) && (
            <div className="process">
              <button
                type="button"
                className="process-toggle"
                aria-expanded={expanded}
                aria-controls={processId}
                onClick={() => setExpanded(!expanded)}
              >
                {pending || (unmanaged && activeTool) ? (
                  <Clock3 size={14} />
                ) : activeTool ? (
                  <LoaderCircle size={14} className="spin" />
                ) : (
                  <Check size={14} />
                )}
                <span>
                  {unmanaged && activeTool
                    ? `已记录 ${workTools.length} 个步骤`
                    : pending
                      ? `已发起 ${workTools.length} 个步骤`
                      : activeTool
                        ? `正在${toolLabel(activeTool.name, '执行工具')} · ${workTools.length} 个步骤`
                        : deniedCount > 0
                          ? `已记录 ${workTools.length} 个步骤`
                          : workTools.length
                            ? `已执行 ${workTools.length} 个步骤`
                            : '查看执行过程'}
                </span>
                <DisclosureChevron expanded={expanded} />
              </button>
              <div id={processId}>
                <Reveal open={expanded}>
                  {expanded && (
                    <div className="tool-list" role="region" aria-label="执行步骤" tabIndex={0}>
                      {tools.map((tool) => (
                        <ToolRow
                          key={tool.id}
                          tool={tool}
                          unmanaged={unmanaged}
                          onInspect={onInspect}
                        />
                      ))}
                      {drafts.map((message) => (
                        <Disclosure
                          className="superseded-reply"
                          label="汇总前的回答"
                          key={message.id}
                        >
                          <ConversationMarkdown text={message.text} />
                        </Disclosure>
                      ))}
                    </div>
                  )}
                </Reveal>
              </div>
            </div>
          )}
          {timeline.map((item) => (
            <div key={item.key}>{item.content}</div>
          ))}
          {pendingApprovals.map((approval) => (
            <m.div key={approval.id} id={approval.id} tabIndex={-1} {...approvalMotion}>
              <ApprovalCard
                approval={approval}
                agentLabel={
                  approval.agentId
                    ? approval.agentLabel || agentLabel?.(approval.agentId) || '子 agent'
                    : undefined
                }
                canApprove={canApprove && !unmanaged}
                onApprove={onApprove}
                notify={notify}
              />
            </m.div>
          ))}
          {handledApprovals.length > 0 && (
            <Disclosure
              className="approval-history"
              label={
                <>
                  {deniedCount > 0 ? (
                    <CircleAlert size={13} aria-hidden="true" />
                  ) : (
                    <Check size={13} aria-hidden="true" />
                  )}
                  <span>
                    {`已处理 ${handledApprovals.length} 项审批`}
                    {deniedCount > 0 && ` · 拒绝 ${deniedCount} 项`}
                  </span>
                </>
              }
            >
              <div className="approval-history-content">
                {handledApprovals.map((approval) => (
                  <ApprovalCard
                    key={approval.id}
                    approval={approval}
                    canApprove={canApprove}
                    onApprove={onApprove}
                    notify={notify}
                  />
                ))}
              </div>
            </Disclosure>
          )}
          {progress && (
            <div className="thinking" role="status">
              <span />
              <span />
              <span />
              <m.span className="thinking-label" key={progress} {...statusMotion}>
                {progress}
              </m.span>
            </div>
          )}
          {turn.status === 'failed' && (
            <div className="inline-failure" role="alert">
              <CircleAlert size={15} aria-hidden="true" />
              {turn.failureMessage ??
                (tools.length || drafts.length
                  ? '本轮执行失败，可展开执行步骤查看详情。'
                  : '本轮执行失败。')}
            </div>
          )}
          {turn.status === 'canceled' && <p className="muted text-small">本轮执行已停止</p>}
          {(finishedUsage || canCopyAnswer) && (
            <div className="message-footer">
              {canCopyAnswer && (
                <button aria-label="复制回答" title="复制回答" onClick={() => void copy()}>
                  {copied ? <Check size={14} /> : <Copy size={14} />}
                </button>
              )}
              {finishedUsage && <TurnUsageSummary usage={finishedUsage} />}
            </div>
          )}
        </div>
      </div>
    </article>
  )
}
