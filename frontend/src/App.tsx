import { useEffect, useMemo, useRef, useState } from 'react'
import { useMatch, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowDown,
  ArrowRight,
  BookOpen,
  Check,
  ChevronDown,
  CircleAlert,
  CircleHelp,
  Clock3,
  Code2,
  Folder,
  GitBranch,
  Inbox,
  LoaderCircle,
  Menu,
  MessageSquare,
  Moon,
  PanelLeftClose,
  PanelRight,
  Plus,
  Search,
  Settings2,
  ShieldCheck,
  Sparkles,
  Sun,
  Terminal,
  X,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { Composer } from '@/features/conversation/composer'
import { AgentMark, Turn } from '@/features/conversation/turn'
import { Details, type DetailsTab } from '@/features/workspace/details'
import { SubagentsPane } from '@/features/workspace/subagents'
import { useEventFeed } from '@/hooks/use-event-feed'
import { ApiError, liveClient, request } from '@/lib/api'
import { demoClient } from '@/lib/demo'
import { projectTurns, resolveActiveRun, type PendingTurn, type ToolView } from '@/lib/events'
import { errorMessage } from '@/lib/utils'
import type { Mode, PermissionMode, Session, Source, Subagent } from '@/lib/types'

const navigationDrawer = '(max-width: 950px)'
const detailsDrawer = '(max-width: 1275px)'
const sessionTitle = (session: Session) =>
  session.title || `会话 ${session.id.replace('session_', '').slice(0, 8)}`
const statusLabel = (status: string) =>
  ({
    queued: '排队中',
    running: '运行中',
    completed: '已完成',
    failed: '失败',
    canceled: '已取消',
  })[status] ?? status
export default function App() {
  const navigate = useNavigate(),
    queryClient = useQueryClient()
  const id = useMatch('/sessions/:id')?.params.id
  const [source, setSource] = useState<Source>(() =>
    localStorage.getItem('paicli-source') === 'live' ? 'live' : 'demo',
  )
  const [theme, setTheme] = useState(() =>
    localStorage.getItem('paicli-theme') === 'dark' ? 'dark' : 'light',
  )
  const [settings, setSettings] = useState(false),
    [search, setSearch] = useState(false),
    [tasksOpen, setTasksOpen] = useState(false)
  const [searchTerm, setSearchTerm] = useState(''),
    [details, setDetails] = useState(() => !matchMedia(detailsDrawer).matches),
    [sidebar, setSidebar] = useState(false)
  const [mobile, setMobile] = useState(() => matchMedia(navigationDrawer).matches)
  const [detailsTab, setDetailsTab] = useState<DetailsTab>('changes')
  const [agentSelection, setAgentSelection] = useState<{ key: string; path: Subagent[] }>()
  const [drafts, setDrafts] = useState<Record<string, string>>({}),
    [mode, setMode] = useState<Mode>('react')
  const [submitting, setSubmitting] = useState(false),
    [retry, setRetry] = useState(0),
    [notice, setNotice] = useState('')
  const [newPermissionModes, setNewPermissionModes] = useState<Record<Source, PermissionMode>>({
    live: 'default',
    demo: 'default',
  })
  const [changingPermission, setChangingPermission] = useState(false)
  const [cancelRequested, setCancelRequested] = useState<string>(),
    [tool, setTool] = useState<ToolView | null>(null),
    [taskPrompt, setTaskPrompt] = useState('')
  const [pendingTurns, setPendingTurns] = useState<Record<string, PendingTurn>>({})
  const [nearBottom, setNearBottom] = useState(true)
  const scrollRef = useRef<HTMLDivElement>(null),
    sendLock = useRef(false)
  const sourceRef = useRef(source)
  sourceRef.current = source
  const client = source === 'demo' ? demoClient : liveClient
  const draftKey = `${source}:${id ?? 'new'}`,
    draft = drafts[draftKey] ?? ''
  const setDraft = (value: string) => setDrafts((previous) => ({ ...previous, [draftKey]: value }))
  const sessions = useQuery({
    queryKey: ['sessions', source],
    queryFn: ({ signal }) => client.sessions(signal),
    refetchInterval: 3500,
  })
  const health = useQuery({
    queryKey: ['health', source],
    queryFn: ({ signal }) => client.health(signal),
    retry: false,
  })
  const changes = useQuery({
    queryKey: ['changes', source, id],
    queryFn: () => client.changes(id!),
    enabled: Boolean(id) && client.capabilities.changes,
  })
  const tasks = useQuery({
    queryKey: ['tasks', source],
    queryFn: ({ signal }) => client.tasks(signal),
    enabled: tasksOpen,
    refetchInterval: tasksOpen ? 2500 : false,
  })
  const bridge = useQuery({
    queryKey: ['bridge'],
    queryFn: () => request<{ configured: boolean }>('/__paicli/status'),
    enabled: settings,
    retry: false,
  })
  const pending = pendingTurns[draftKey]
  const feed = useEventFeed(client, source, id, retry)
  const permissionMode = id ? (feed.permissionMode ?? 'default') : newPermissionModes[source]
  const turns = useMemo(
    () => projectTurns(feed.events, feed.preview, pending),
    [feed.events, feed.preview, pending],
  )
  const agentsByTurn = useMemo(() => {
    const result = new Map<string, Subagent[]>()
    for (const agent of feed.subagents ?? []) {
      if (!agent.parent_turn_id) continue
      const agents = result.get(agent.parent_turn_id) ?? []
      agents.push(agent)
      result.set(agent.parent_turn_id, agents)
    }
    return result
  }, [feed.subagents])
  const agentPath = agentSelection?.key === draftKey ? agentSelection.path : []
  const selectedAgent = agentPath.at(-1)
  const visibleAgent =
    feed.subagents?.find((agent) => agent.id === selectedAgent?.id) ?? selectedAgent
  const approvals = useMemo(() => turns.flatMap((turn) => turn.approvals), [turns])
  const openAgent = (agent: Subagent) => {
    setAgentSelection({ key: draftKey, path: [agent] })
    setTool(null)
    setDetailsTab('subagents')
    setDetails(true)
  }
  const showApproval = (approvalId: string) => {
    if (matchMedia(detailsDrawer).matches) setDetails(false)
    requestAnimationFrame(() => {
      const card = document.getElementById(approvalId)
      card?.focus({ preventScroll: true })
      card?.scrollIntoView({ block: 'nearest' })
    })
  }
  const optimistic = turns.find((turn) => turn.optimistic)
  const activeRun = resolveActiveRun(feed, pending)
  const runId = activeRun?.run_id
  const pendingFinished = Boolean(pending?.run && runId !== pending.run.id)
  const unmanagedRun =
    source === 'live' && feed.status === 'running' && !runId && (!pending || pendingFinished)
  const busy = Boolean(runId || feed.status === 'running' || (pending && !pendingFinished))
  const stopping = activeRun?.status === 'canceling' || Boolean(runId && cancelRequested === runId)
  const current = sessions.data?.find((s) => s.id === id)
  const title = current ? sessionTitle(current) : '新会话'
  const visibleTool = tool
    ? (turns.flatMap((t) => t.tools).find((t) => t.id === tool.id) ?? tool)
    : null
  const notify = (message: string) => setNotice(message)
  useEffect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem('paicli-theme', theme)
  }, [theme])
  useEffect(() => {
    const resize = () => {
      setMobile(matchMedia(navigationDrawer).matches)
      if (matchMedia(detailsDrawer).matches) setDetails(false)
    }
    window.addEventListener('resize', resize)
    return () => window.removeEventListener('resize', resize)
  }, [])
  useEffect(() => {
    localStorage.setItem('paicli-source', source)
  }, [source])
  useEffect(() => {
    if (location.pathname === '/')
      navigate(source === 'demo' ? '/sessions/demo-workspace' : '/new', { replace: true })
  }, [navigate, source])
  useEffect(() => {
    setTool(null)
    setAgentSelection(undefined)
    setNearBottom(true)
    scrollRef.current?.scrollTo({ top: 0 })
  }, [id, source])
  useEffect(() => {
    if (!pending || !pendingFinished) return
    if (optimistic?.status === 'failed' || optimistic?.id === 'pending') {
      setDrafts((previous) => ({ ...previous, [draftKey]: previous[draftKey] || pending.message }))
      setNotice('Agent 未能启动，请查看本地 Runtime 后重试。')
    }
    setPendingTurns((previous) => {
      if (previous[draftKey] !== pending) return previous
      const next = { ...previous }
      delete next[draftKey]
      return next
    })
  }, [draftKey, pending, pendingFinished, optimistic?.status, optimistic?.id])
  useEffect(() => {
    const viewport = scrollRef.current
    if (!nearBottom || !viewport) return
    if (!busy && viewport.scrollTop === 0 && viewport.scrollHeight - viewport.clientHeight <= 72)
      return
    viewport.scrollTo({ top: viewport.scrollHeight, behavior: 'instant' })
  }, [turns, nearBottom, busy])
  useEffect(() => {
    if (!notice) return
    const timer = setTimeout(() => setNotice(''), 7000)
    return () => clearTimeout(timer)
  }, [notice])
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setSearch((value) => !value)
      }
      if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === 'o') {
        event.preventDefault()
        navigate('/new')
        window.dispatchEvent(new Event('paicli:focus'))
      }
      if (event.key === 'Escape') setSidebar(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [navigate])
  const selectSession = (sessionId: string) => {
    navigate(`/sessions/${encodeURIComponent(sessionId)}`)
    setSidebar(false)
    setSearch(false)
  }
  const switchSource = (next: Source) => {
    if (submitting) return
    setSource(next)
    navigate('/new')
    setSettings(false)
    setNotice('')
  }
  const setPermissionMode = async (next: PermissionMode) => {
    if (changingPermission || submitting || next === permissionMode) return
    if (!id) {
      setNewPermissionModes((previous) => ({ ...previous, [source]: next }))
      return
    }
    setChangingPermission(true)
    try {
      const result = await client.setPermissionMode(id, next)
      feed.setPermissionMode(result.mode)
    } catch (error) {
      notify(errorMessage(error))
    } finally {
      setChangingPermission(false)
    }
  }
  const send = async () => {
    if (!draft.trim() || busy || feed.syncing || changingPermission || sendLock.current) return
    sendLock.current = true
    setSubmitting(true)
    const submittedSource = source,
      message = draft.trim(),
      key = draftKey
    const queued = { message, afterSequence: feed.cursor }
    setPendingTurns((previous) => ({ ...previous, [key]: queued }))
    setDrafts((previous) => ({ ...previous, [key]: '' }))
    setNearBottom(true)
    let sessionId = id
    let submittedKey = key
    try {
      if (!sessionId) {
        const session = await client.createSession()
        sessionId = session.id
        if (permissionMode === 'auto') await client.setPermissionMode(sessionId, 'auto')
        setNewPermissionModes((previous) => ({ ...previous, [submittedSource]: 'default' }))
        submittedKey = `${submittedSource}:${sessionId}`
        setPendingTurns((previous) => {
          const next = { ...previous, [submittedKey]: queued }
          delete next[key]
          return next
        })
        void queryClient.invalidateQueries({ queryKey: ['sessions', submittedSource] })
        if (sourceRef.current === submittedSource && location.pathname === '/new')
          navigate(`/sessions/${encodeURIComponent(sessionId)}`)
      }
      const result = await client.send(sessionId, message, mode)
      const accepted = {
        ...queued,
        afterSequence: result.after_sequence ?? queued.afterSequence,
        run: { id: result.run_id, turnId: result.turn_id, acceptedAt: performance.now() },
      }
      setPendingTurns((previous) =>
        previous[submittedKey] === queued ? { ...previous, [submittedKey]: accepted } : previous,
      )
      if (feed.error && sourceRef.current === submittedSource) setRetry((value) => value + 1)
      void queryClient.invalidateQueries({ queryKey: ['sessions', submittedSource] })
    } catch (error) {
      setPendingTurns((previous) => {
        const next = { ...previous }
        delete next[submittedKey]
        return next
      })
      setDrafts((previous) => ({
        ...previous,
        [submittedKey]: previous[submittedKey] || message,
      }))
      notify(errorMessage(error))
    } finally {
      sendLock.current = false
      setSubmitting(false)
    }
  }
  const stop = async () => {
    if (!id || !runId || stopping) return
    setSubmitting(true)
    try {
      await client.cancelTurn(id, runId)
      if (source === 'live') setCancelRequested(runId)
    } catch (error) {
      notify(errorMessage(error))
      if (error instanceof ApiError && (error.status === 404 || error.status === 409))
        setRetry((value) => value + 1)
    } finally {
      setSubmitting(false)
    }
  }
  const approval = async (approvalId: string, decision: 'approve' | 'deny') => {
    if (!id) return
    await client.decideApproval(id, approvalId, decision)
  }
  const openNewConversation = () => {
    navigate('/new')
    setSidebar(false)
    window.dispatchEvent(new Event('paicli:focus'))
  }
  const addTask = async () => {
    if (!taskPrompt.trim() || submitting) return
    setSubmitting(true)
    try {
      await client.addTask(taskPrompt.trim(), mode)
      setTaskPrompt('')
      await tasks.refetch()
    } catch (error) {
      notify(errorMessage(error))
    } finally {
      setSubmitting(false)
    }
  }
  const grouped = (sessions.data ?? []).reduce<Record<string, Session[]>>((groups, session) => {
    const today = new Date().toDateString(),
      yesterday = new Date(Date.now() - 86400000).toDateString(),
      date = new Date(session.updated_at).toDateString()
    const label = date === today ? '今天' : date === yesterday ? '昨天' : '更早'
    ;(groups[label] ??= []).push(session)
    return groups
  }, {})
  const project = health.data?.cwd.split(/[\\/]/).filter(Boolean).at(-1) ?? 'PaiCLI-Python'
  return (
    <div className={`workspace ${details ? 'with-details' : ''}`}>
      {sidebar && (
        <button className="sidebar-scrim" aria-label="关闭导航" onClick={() => setSidebar(false)} />
      )}
      <aside
        className={`sidebar ${sidebar ? 'is-open' : ''}`}
        aria-label="主导航"
        inert={mobile && !sidebar}
      >
        <div className="brand-row">
          <a
            className="brand"
            href="/new"
            onClick={(e) => {
              e.preventDefault()
              navigate('/new')
              setSidebar(false)
            }}
          >
            <AgentMark />
            <span>
              PaiCLI<span className="brand-period">.</span>
            </span>
          </a>
          <Button
            variant="ghost"
            size="icon"
            className="sidebar-close"
            aria-label="收起导航"
            onClick={() => setSidebar(false)}
          >
            <PanelLeftClose size={16} />
          </Button>
        </div>
        <Button variant="outline" className="new-chat" onClick={openNewConversation}>
          <Plus size={17} />
          新建会话<kbd>Ctrl ⇧ O</kbd>
        </Button>
        <nav className="primary-nav">
          <button onClick={() => setSearch(true)}>
            <Search size={16} />
            搜索会话<kbd>Ctrl K</kbd>
          </button>
          <button onClick={() => setTasksOpen(true)}>
            <Inbox size={16} />
            后台任务
          </button>
        </nav>
        <div className="project-heading">
          <span>项目空间</span>
        </div>
        <div className="project-selector">
          <div className="project-icon">
            <Code2 size={16} />
          </div>
          <div>
            <strong>{project}</strong>
            <span>本地项目</span>
          </div>
        </div>
        <div className="session-navigation">
          {sessions.isLoading && (
            <div className="sidebar-loading">
              <LoaderCircle size={15} className="spin" />
              加载会话…
            </div>
          )}
          {sessions.error && (
            <div className="sidebar-error">
              <CircleAlert size={15} />
              <p>{errorMessage(sessions.error)}</p>
              <button onClick={() => void sessions.refetch()}>重试连接</button>
            </div>
          )}
          {!sessions.isLoading && !sessions.error && !sessions.data?.length && (
            <p className="sidebar-empty">还没有会话，从一个想法开始。</p>
          )}
          {Object.entries(grouped).map(([label, items]) => (
            <section className="session-group" key={label}>
              <h3>{label}</h3>
              {items.map((session) => (
                <button
                  className={`session-item ${session.id === id ? 'selected' : ''}`}
                  aria-current={session.id === id ? 'page' : undefined}
                  key={session.id}
                  onClick={() => selectSession(session.id)}
                >
                  <MessageSquare size={14} />
                  <span>{sessionTitle(session)}</span>
                  {session.status === 'running' && <span className="status-dot pulse" />}
                </button>
              ))}
            </section>
          ))}
        </div>
        <div className="sidebar-bottom">
          <button className="settings-nav" onClick={() => setSettings(true)}>
            <Settings2 size={16} />
            设置与连接
            <span className={`connection-dot ${health.isError ? 'offline' : ''}`} />
          </button>
          <div className="profile-row">
            <span className="profile-avatar">Y</span>
            <div>
              <strong>个人工作空间</strong>
              <span>{source === 'demo' ? '交互演示' : '本地 Runtime'}</span>
            </div>
            <Button
              variant="ghost"
              size="icon"
              aria-label={theme === 'light' ? '切换深色主题' : '切换浅色主题'}
              onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')}
            >
              {theme === 'light' ? <Moon size={16} /> : <Sun size={16} />}
            </Button>
          </div>
        </div>
      </aside>
      <main className="main-panel">
        <header className="topbar">
          <div className="topbar-leading">
            <Button
              variant="ghost"
              size="icon"
              className="mobile-menu"
              aria-label="打开导航"
              onClick={() => setSidebar(true)}
            >
              <Menu size={18} />
            </Button>
            <span className="breadcrumb-project">{project}</span>
            <span className="breadcrumb-slash">/</span>
            <span className="breadcrumb-title">{id ? title : '新会话'}</span>
          </div>
          <div className="topbar-actions">
            <button
              className={`source-pill ${source}`}
              data-offline={source === 'live' && health.isError ? true : undefined}
              onClick={() => setSettings(true)}
            >
              <span />
              {source === 'demo'
                ? '演示模式'
                : health.isError
                  ? '连接异常'
                  : health.isPending
                    ? '连接中'
                    : '本地连接'}
              <ChevronDown size={11} />
            </button>
            <span className="topbar-separator" />
            <Button
              variant="ghost"
              size="icon"
              aria-label={details ? '隐藏详情面板' : '显示详情面板'}
              aria-pressed={details}
              onClick={() => setDetails(!details)}
            >
              <PanelRight size={17} />
            </Button>
          </div>
        </header>
        {source === 'live' && (
          <div className="live-info">
            <span className="status-dot" data-offline={health.isError ? true : undefined} />
            {health.isError ? 'Runtime 未连接，请检查设置' : '通过现有事件 API 同步执行状态'}
          </div>
        )}
        <div
          className="conversation-scroll"
          ref={scrollRef}
          onScroll={(e) => {
            const el = e.currentTarget
            setNearBottom(el.scrollHeight - el.scrollTop - el.clientHeight < 100)
          }}
        >
          <div className={`conversation-content ${!id && !pending ? 'welcome-content' : ''}`}>
            {!id && !pending ? (
              <section className="welcome">
                <div className="welcome-symbol">
                  <Terminal size={25} />
                </div>
                <h1>
                  让想法，在项目里发生<span>。</span>
                </h1>
                <p>从一个问题、一段代码，或一个还没成形的想法开始。</p>
                <div className="suggestions">
                  {[
                    {
                      icon: Code2,
                      title: '读懂这个项目',
                      prompt: '帮我梳理这个项目的目录结构和 Agent 的核心运行流程。',
                    },
                    {
                      icon: Sparkles,
                      title: '实现一个想法',
                      prompt: '我想改进项目的会话管理，先分析现有实现，再提出方案。',
                    },
                    {
                      icon: ShieldCheck,
                      title: '检查代码质量',
                      prompt: '检查工具执行器的错误处理和审批流程，找出需要改进的问题。',
                    },
                  ].map((item) => (
                    <button
                      key={item.title}
                      onClick={() => {
                        setDraft(item.prompt)
                        window.dispatchEvent(new Event('paicli:focus'))
                      }}
                    >
                      <item.icon size={19} />
                      <span>{item.title}</span>
                      <ArrowRight size={14} />
                    </button>
                  ))}
                </div>
                <div className="welcome-hint">
                  <BookOpen size={13} />
                  {source === 'demo'
                    ? '当前为演示环境，可在设置中连接真实 Agent'
                    : 'Agent 会使用当前 Runtime 配置的模型与工具'}
                </div>
              </section>
            ) : (
              <>
                <div className="conversation-heading">
                  <h1>{title}</h1>
                  <div>
                    <Folder size={12} />
                    {project}
                    <span>·</span>
                    <GitBranch size={12} />
                    {source === 'demo' ? '示例工作区' : '本地工作区'}
                  </div>
                </div>
                {feed.loading && !pending && (
                  <div className="loading-state">
                    <LoaderCircle className="spin" size={18} />
                    正在恢复会话…
                  </div>
                )}
                {feed.error && (
                  <div className="feed-error" role="alert">
                    <CircleAlert size={16} />
                    <span>{feed.error}</span>
                    <Button variant="outline" size="sm" onClick={() => setRetry((v) => v + 1)}>
                      重试
                    </Button>
                  </div>
                )}
                {!feed.loading && !feed.error && !busy && turns.length === 0 && (
                  <div className="empty-conversation">
                    <AgentMark />
                    <h2>准备就绪</h2>
                    <p>向 PaiCLI 描述这个项目的下一步。</p>
                  </div>
                )}
                {turns.map((turn) => (
                  <Turn
                    key={turn.id}
                    turn={turn}
                    unmanaged={source === 'live' && (unmanagedRun || !busy)}
                    canApprove={client.capabilities.approvals}
                    onApprove={approval}
                    agents={agentsByTurn.get(turn.id)}
                    onOpenAgent={openAgent}
                    agentLabel={(agentId) =>
                      feed.subagents?.find((agent) => agent.id === agentId)?.label
                    }
                    onInspect={(selected) => {
                      setTool(selected)
                      setDetails(true)
                    }}
                    notify={notify}
                  />
                ))}
              </>
            )}
          </div>
        </div>
        {!nearBottom && (
          <Button
            variant="outline"
            size="sm"
            className="jump-bottom"
            onClick={() => {
              setNearBottom(true)
              scrollRef.current?.scrollTo({
                top: scrollRef.current.scrollHeight,
                behavior: 'smooth',
              })
            }}
          >
            <ArrowDown size={14} />
            回到最新
          </Button>
        )}
        <Composer
          draft={draft}
          setDraft={setDraft}
          mode={mode}
          setMode={setMode}
          permissionMode={permissionMode}
          setPermissionMode={(next) => void setPermissionMode(next)}
          changingPermission={changingPermission}
          source={source}
          busy={busy}
          unmanagedRun={unmanagedRun}
          submitting={submitting}
          syncing={feed.syncing}
          canStop={client.capabilities.cancelTurn && Boolean(id && runId)}
          stopping={stopping}
          onSend={() => void send()}
          onStop={() => void stop()}
          onNewConversation={openNewConversation}
        />
      </main>
      {details && (
        <Details
          source={source}
          changes={changes.data ?? []}
          tool={visibleTool}
          tab={detailsTab}
          onTab={setDetailsTab}
          subagentCount={feed.subagents?.length ?? 0}
          subagents={
            <SubagentsPane
              key={draftKey}
              agents={feed.subagents}
              agent={visibleAgent}
              rootId={id}
              client={client}
              source={source}
              approvals={approvals}
              syncing={feed.syncing}
              supported={feed.subagents !== undefined}
              onApproval={showApproval}
              onOpen={(agent) => {
                if (visibleAgent) setAgentSelection({ key: draftKey, path: [...agentPath, agent] })
                else openAgent(agent)
              }}
              onBack={() => setAgentSelection({ key: draftKey, path: agentPath.slice(0, -1) })}
            />
          }
          onClose={() => setDetails(false)}
          onClearTool={() => setTool(null)}
        />
      )}
      {notice && (
        <div className="toast" role="status">
          <CircleAlert size={16} />
          <span>{notice}</span>
          <button aria-label="关闭提示" onClick={() => setNotice('')}>
            <X size={15} />
          </button>
        </div>
      )}
      <Dialog
        open={settings}
        onOpenChange={setSettings}
        title="让工作台连接你的 Agent"
        description="选择交互演示，或使用本地 Python Runtime。"
      >
        <div className="connection-options">
          <button
            className={source === 'demo' ? 'active' : ''}
            disabled={submitting}
            onClick={() => switchSource('demo')}
          >
            <Sparkles size={22} />
            <div>
              <strong>交互演示</strong>
              <p>体验会话、分段输出、审批和示例变更。</p>
            </div>
            {source === 'demo' && <Check size={17} />}
          </button>
          <button
            className={source === 'live' ? 'active' : ''}
            disabled={submitting}
            onClick={() => switchSource('live')}
          >
            <Terminal size={22} />
            <div>
              <strong>连接 Runtime</strong>
              <p>使用当前项目中的真实 Agent 与工具。</p>
            </div>
            {source === 'live' && <Check size={17} />}
          </button>
        </div>
        <div className="settings-section">
          <h3>本地连接</h3>
          <div className="settings-status">
            <span className={`connection-dot ${bridge.data?.configured ? '' : 'offline'}`} />
            {bridge.isError
              ? '无法读取桥接状态，请使用 pnpm dev 启动本地前端'
              : bridge.isPending
                ? '正在检查本地连接配置…'
                : bridge.data?.configured
                  ? '服务端访问凭据已配置'
                  : '尚未配置本地服务端访问凭据'}
          </div>
          <p>
            在 <code>frontend/.env.local</code> 中设置 <code>PAICLI_WEB_RUNTIME_KEY</code>
            ，然后重启前端。默认连接本机 8080 端口。
          </p>
          <p className="muted">密钥由本地代理注入，浏览器不接收或保存密钥。</p>
        </div>
        <div className="settings-section">
          <h3>接口接入状态</h3>
          <div className="capability-row">
            <span>会话 / 历史 / 异步运行 / 后台任务 / 文字增量 / 网页审批</span>
            <span className="cap-ready">已接入</span>
          </div>
          <div className="capability-row">
            <span>停止 / Diff</span>
            <span className="cap-pending">待后端补齐</span>
          </div>
        </div>
        <div className="settings-footer">
          <span>
            <CircleHelp size={13} />
            详细配置见 frontend/README.md
          </span>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')}
          >
            {theme === 'light' ? <Moon size={14} /> : <Sun size={14} />}切换主题
          </Button>
        </div>
      </Dialog>
      <Dialog
        open={search}
        onOpenChange={(open) => {
          setSearch(open)
          if (!open) setSearchTerm('')
        }}
        title="搜索会话"
        description="按标题或会话 ID 查找最近的工作。"
      >
        <div className="search-input">
          <Search size={18} />
          <input
            autoFocus
            placeholder="搜索你的会话…"
            aria-label="搜索会话"
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
          />
          <kbd>ESC</kbd>
        </div>
        <div className="search-results">
          {(sessions.data ?? [])
            .filter((s) =>
              `${sessionTitle(s)} ${s.id}`.toLowerCase().includes(searchTerm.toLowerCase()),
            )
            .map((session) => (
              <button key={session.id} onClick={() => selectSession(session.id)}>
                <MessageSquare size={16} />
                <span>{sessionTitle(session)}</span>
                <ArrowRight size={14} />
              </button>
            ))}
          {!(sessions.data ?? []).some((s) =>
            `${sessionTitle(s)} ${s.id}`.toLowerCase().includes(searchTerm.toLowerCase()),
          ) && <p>没有找到匹配的会话。</p>}
        </div>
      </Dialog>
      <Dialog
        open={tasksOpen}
        onOpenChange={setTasksOpen}
        title="后台任务"
        description={
          source === 'demo'
            ? '演示任务队列，不执行真实操作。'
            : '提交任务，由本地 Runtime Worker 执行。'
        }
      >
        <form
          className="task-form"
          onSubmit={(e) => {
            e.preventDefault()
            void addTask()
          }}
        >
          <input
            value={taskPrompt}
            onChange={(e) => setTaskPrompt(e.target.value)}
            aria-label="后台任务描述"
            placeholder="添加一个后台任务…"
          />
          <Button size="sm" disabled={!taskPrompt.trim() || submitting}>
            <Plus size={14} />
            添加
          </Button>
        </form>
        <div className="task-list">
          {tasks.isLoading && <p>正在加载任务…</p>}
          {tasks.error && (
            <div role="alert">
              {errorMessage(tasks.error)}
              <Button variant="ghost" size="sm" onClick={() => void tasks.refetch()}>
                重试
              </Button>
            </div>
          )}
          {!tasks.isLoading && !tasks.error && !tasks.data?.length && (
            <div className="panel-empty">
              <Inbox size={25} />
              <p>队列是空的，添加一个任务开始。</p>
            </div>
          )}
          {tasks.data?.map((task) => (
            <div className="task-item" key={task.id}>
              <div>
                <Clock3 size={15} />
                <strong>{task.prompt}</strong>
                <span className={`task-status ${task.status}`}>{statusLabel(task.status)}</span>
              </div>
              {task.result && <p>{task.result}</p>}
              {task.error && (
                <p className="inline-failure">任务执行失败，请检查本地 Runtime 日志。</p>
              )}
              {['queued', 'running'].includes(task.status) && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={async () => {
                    try {
                      await client.cancelTask(task.id)
                      await tasks.refetch()
                    } catch (error) {
                      notify(errorMessage(error))
                    }
                  }}
                >
                  取消任务
                </Button>
              )}
            </div>
          ))}
        </div>
      </Dialog>
    </div>
  )
}
