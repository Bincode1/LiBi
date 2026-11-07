import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, Bot, ChevronDown, ShieldCheck, Terminal } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { AgentAvatar } from '@/features/conversation/agent-avatar'
import { ConversationMarkdown } from '@/features/conversation/markdown'
import { SubagentRow } from '@/features/conversation/subagent-activity'
import { useEventFeed } from '@/hooks/use-event-feed'
import { subagentIsActive, subagentStatus, toolLabel } from '@/lib/activity'
import { projectTurns, type ApprovalView, type TurnView } from '@/lib/events'
import type { RuntimeClient, Subagent } from '@/lib/types'

const empty: Subagent[] = []

function elapsed(agent: Subagent, now: number) {
  const end = subagentIsActive(agent) ? now : Date.parse(agent.updated_at)
  const seconds = Math.max(0, Math.floor((end - Date.parse(agent.started_at)) / 1000))
  if (!Number.isFinite(seconds)) return ''
  return seconds >= 60 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : `${seconds} 秒`
}

function ChildAssignment({ label, text }: { label: string; text: string }) {
  if (text.length > 180) {
    return (
      <details className="child-assignment">
        <summary>
          <strong>{label}</strong>
          <ChevronDown size={13} aria-hidden="true" />
        </summary>
        <ConversationMarkdown text={text} />
      </details>
    )
  }
  return (
    <div className="child-assignment">
      <strong>{label}</strong>
      <ConversationMarkdown text={text} />
    </div>
  )
}

function ChildTurn({ turn, index, active }: { turn: TurnView; index: number; active: boolean }) {
  const entries = [
    ...turn.messages.map((message) => ({
      key: message.id,
      sequence: message.sequence ?? Infinity,
      content: (
        <ConversationMarkdown
          text={message.text}
          commentary={message.commentary}
          streaming={active && turn.status === 'running' && message.streaming}
        />
      ),
    })),
    ...turn.tools.map((tool) => ({
      key: tool.id,
      sequence: tool.sequence ?? Infinity,
      content: (
        <details className="child-tool">
          <summary>
            <Terminal size={13} aria-hidden="true" />
            <span>{toolLabel(tool.name)}</span>
            <small>
              {tool.status === 'running'
                ? active
                  ? '进行中'
                  : '无后续记录'
                : tool.status === 'failed'
                  ? '失败'
                  : '已完成'}
            </small>
            <ChevronDown size={12} aria-hidden="true" />
          </summary>
          <pre>{tool.content || '暂无工具输出。'}</pre>
        </details>
      ),
    })),
    ...(turn.steering ?? []).map((message) => ({
      key: message.id,
      sequence: message.sequence,
      content: <ChildAssignment label="父 agent 追加指令" text={message.text} />,
    })),
  ].sort((left, right) => left.sequence - right.sequence)

  return (
    <article className="child-turn">
      {turn.user && (
        <ChildAssignment
          label={index === 0 ? '父 agent 委派' : '父 agent 追加指令'}
          text={turn.user}
        />
      )}
      {entries.map((entry) => (
        <div key={entry.key}>{entry.content}</div>
      ))}
      {turn.status === 'failed' && (
        <p className="inline-failure">{turn.failureMessage ?? '本轮执行失败。'}</p>
      )}
      {turn.status === 'canceled' && <p className="muted">本轮已停止。</p>}
    </article>
  )
}

function ChildTranscript({
  agent,
  client,
  rootId,
  approvals,
  onOpen,
  onApproval,
  onBack,
}: {
  agent: Subagent
  client: RuntimeClient
  rootId: string
  approvals: ApprovalView[]
  onOpen: (agent: Subagent) => void
  onApproval: (id: string) => void
  onBack: () => void
}) {
  const [retry, setRetry] = useState(0)
  const feed = useEventFeed(client, agent.session_id, retry, rootId)
  const turns = useMemo(() => projectTurns(feed.events, feed.preview), [feed.events, feed.preview])
  const viewport = useRef<HTMLDivElement>(null)
  const nearBottom = useRef(subagentIsActive(agent))
  const back = useRef<HTMLButtonElement>(null)
  const current = feed.subagent ?? agent
  const active = subagentIsActive(current)
  const [now, setNow] = useState(Date.now)
  const pending = approvals.filter(
    (approval) =>
      approval.status === 'pending' &&
      (approval.agentSessionId === agent.session_id || approval.agentId === agent.id),
  )
  useLayoutEffect(() => {
    if (nearBottom.current && viewport.current) {
      viewport.current.scrollTop = viewport.current.scrollHeight
    }
  }, [turns, feed.error, pending.length])
  useLayoutEffect(() => {
    back.current?.focus({ preventScroll: true })
  }, [])
  useEffect(() => {
    if (!active) return
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [active])

  return (
    <div className="child-pane">
      <div className="child-header">
        <button className="child-back" ref={back} aria-label="返回子 agent 列表" onClick={onBack}>
          <ArrowLeft size={17} aria-hidden="true" />
        </button>
        <AgentAvatar agent={current} />
        <div>
          <h3 title={current.label}>{current.label}</h3>
          <p>
            {[subagentStatus(current), elapsed(current, now), current.closed && '执行实例已关闭']
              .filter(Boolean)
              .join(' · ')}
          </p>
        </div>
      </div>
      <div
        className="child-transcript"
        ref={viewport}
        onScroll={(event) => {
          const { scrollHeight, scrollTop, clientHeight } = event.currentTarget
          nearBottom.current = scrollHeight - scrollTop - clientHeight < 80
        }}
      >
        {feed.error && (
          <div className="subagent-notice" role="status">
            {feed.error}
            <Button variant="outline" size="sm" onClick={() => setRetry((value) => value + 1)}>
              重试
            </Button>
          </div>
        )}
        {feed.loading && (
          <p className="subagent-notice" role="status">
            正在加载执行记录…
          </p>
        )}
        {!feed.loading && !feed.error && !turns.length && (
          <p className="subagent-notice">
            {active ? '正在等待首条执行记录…' : '该子 agent 没有保存对话记录。'}
          </p>
        )}
        {turns.map((turn, index) => (
          <ChildTurn key={turn.id} turn={turn} index={index} active={active} />
        ))}
        {pending.map((approval) => (
          <button
            className="child-approval-link"
            key={approval.id}
            onClick={() => onApproval(approval.id)}
          >
            <ShieldCheck size={14} aria-hidden="true" />
            {approval.toolName ? `${toolLabel(approval.toolName)} · ` : ''}前往主对话审批
          </button>
        ))}
        {Boolean(feed.subagents?.length) && (
          <section className="subagent-list">
            <h3>此 agent 委派的任务</h3>
            {feed.subagents?.map((child) => (
              <SubagentRow key={child.id} agent={child} onOpen={onOpen} />
            ))}
          </section>
        )}
      </div>
    </div>
  )
}

export function SubagentsPane({
  agents = empty,
  agent,
  client,
  rootId,
  approvals,
  onOpen,
  onBack,
  onApproval,
  syncing,
  supported,
}: {
  agents?: Subagent[]
  agent?: Subagent
  client: RuntimeClient
  rootId?: string
  approvals: ApprovalView[]
  onOpen: (agent: Subagent) => void
  onBack: () => void
  onApproval: (id: string) => void
  syncing: boolean
  supported: boolean
}) {
  const list = useRef<HTMLDivElement>(null)
  const listScroll = useRef(0)
  const lastViewed = useRef<string>(undefined)
  const agentId = agent?.id
  useLayoutEffect(() => {
    if (agentId) {
      lastViewed.current = agentId
    } else if (list.current) {
      list.current.scrollTop = listScroll.current
      if (lastViewed.current) {
        list.current
          .querySelector<HTMLButtonElement>(`[data-agent-id="${CSS.escape(lastViewed.current)}"]`)
          ?.focus({ preventScroll: true })
      }
    }
  }, [agentId])

  if (agent && rootId) {
    return (
      <ChildTranscript
        key={agent.id}
        agent={agent}
        client={client}
        rootId={rootId}
        approvals={approvals}
        onOpen={onOpen}
        onApproval={onApproval}
        onBack={onBack}
      />
    )
  }
  const active = agents.filter(subagentIsActive)
  const ended = agents
    .filter((item) => !subagentIsActive(item))
    .sort((left, right) => right.updated_at.localeCompare(left.updated_at))
  return (
    <div
      className="subagent-lists"
      ref={list}
      onScroll={(event) => {
        listScroll.current = event.currentTarget.scrollTop
      }}
    >
      {agents.length ? (
        <>
          {[
            { label: '进行中', items: active },
            { label: '已结束', items: ended },
          ].map(({ label, items }) => (
            <section className="subagent-list" key={label}>
              <h3>
                {label} · {items.length}
              </h3>
              {items.map((item) => (
                <SubagentRow key={item.id} agent={item} onOpen={onOpen} />
              ))}
              {!items.length && <p className="muted">暂无任务</p>}
            </section>
          ))}
        </>
      ) : (
        <div className="panel-empty">
          <Bot size={28} aria-hidden="true" />
          <h3>{syncing ? '正在同步子 agent' : '本会话暂无子 agent'}</h3>
          <p>
            {rootId && !syncing && !supported
              ? '当前连接未提供子 agent 状态。'
              : 'PaiCLI 委派任务时，进度和交付会显示在这里。'}
          </p>
        </div>
      )}
    </div>
  )
}
