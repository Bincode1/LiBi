import { Bot, Check, ChevronRight, CircleAlert, Clock3, LoaderCircle } from 'lucide-react'
import { subagentIsActive, subagentStatus } from '@/lib/activity'
import type { Subagent } from '@/lib/types'
import { AgentAvatar } from './agent-avatar'

export function SubagentRow({
  agent,
  onOpen,
}: {
  agent: Subagent
  onOpen: (agent: Subagent) => void
}) {
  const active = subagentIsActive(agent)
  const done = agent.status === 'completed' || agent.status === 'idle'
  return (
    <button
      className="subagent-row"
      data-agent-id={agent.id}
      type="button"
      onClick={() => onOpen(agent)}
    >
      <AgentAvatar agent={agent} />
      <span className="subagent-row-label">
        <span title={agent.label}>{agent.label}</span>
        <small className="subagent-status" data-status={agent.status}>
          {agent.status === 'approval' ? (
            <Clock3 size={11} aria-hidden="true" />
          ) : active ? (
            <LoaderCircle size={11} className="spin" aria-hidden="true" />
          ) : done ? (
            <Check size={11} aria-hidden="true" />
          ) : (
            <CircleAlert size={11} aria-hidden="true" />
          )}
          {subagentStatus(agent)}
        </small>
      </span>
      <ChevronRight size={14} aria-hidden="true" />
    </button>
  )
}

export function Delegation({
  agents,
  onOpen,
}: {
  agents: Subagent[]
  onOpen: (agent: Subagent) => void
}) {
  const completed = agents.every((agent) => agent.status === 'completed')
  return (
    <div className="delegation">
      <div className="delegation-heading">
        <Bot size={14} aria-hidden="true" />
        {completed ? `子 agent · ${agents.length} 个已完成` : `已启动 ${agents.length} 个子 agent`}
      </div>
      {agents.map((agent) => (
        <SubagentRow key={agent.id} agent={agent} onOpen={onOpen} />
      ))}
    </div>
  )
}
