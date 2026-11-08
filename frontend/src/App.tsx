import { lazy, Suspense, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import { matchPath, useLocation, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AnimatePresence, useReducedMotion } from 'motion/react'
import * as m from 'motion/react-m'
import {
  ArrowDown,
  ArrowRight,
  ChevronDown,
  CircleAlert,
  Code2,
  Folder,
  Inbox,
  LoaderCircle,
  Menu,
  Moon,
  PanelLeftClose,
  PanelRight,
  Pencil,
  Plug,
  Plus,
  Search,
  ShieldCheck,
  Sparkles,
  Sun,
  X,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { Disclosure } from '@/components/ui/disclosure'
import { AgentMark } from '@/features/conversation/agent-avatar'
import { Composer } from '@/features/conversation/composer'
import { ModePicker } from '@/features/conversation/mode-picker'
import { TurnUsageSummary } from '@/features/conversation/turn-usage'
import { McpSettings } from '@/features/mcp/settings'
import { Details, type DetailsTab } from '@/features/workspace/details'
import {
  SessionWorkspace,
  WorkspacePicker,
  useRecentWorkspaces,
} from '@/features/workspace/workspace-picker'
import { useEventFeed } from '@/hooks/use-event-feed'
import { useTabDrafts } from '@/hooks/use-tab-drafts'
import { ApiError, liveClient, request } from '@/lib/api'
import { projectTurns, resolveActiveRun, type PendingTurn, type ToolView } from '@/lib/events'
import { easeOut, useSurfaceMotion } from '@/lib/motion'
import { errorMessage } from '@/lib/utils'
import type { Mode, PermissionMode, Session, Subagent } from '@/lib/types'

const client = liveClient
const Turn = lazy(() => import('@/features/conversation/turn').then((m) => ({ default: m.Turn })))
const SubagentsPane = lazy(() =>
  import('@/features/workspace/subagents').then((m) => ({ default: m.SubagentsPane })),
)
const ConversationMarkdown = lazy(() =>
  import('@/features/conversation/markdown').then((m) => ({ default: m.ConversationMarkdown })),
)

const navigationDrawer = '(max-width: 760px)'
const detailsDrawer = '(max-width: 1080px)'
const maxTitleLength = 60
const sessionTitle = (session: Pick<Session, 'id' | 'title'>) =>
  session.title || `会话 ${session.id.replace('session_', '').slice(0, 8)}`
const statusLabel = (status: string) =>
  ({
    queued: '排队中',
    running: '运行中',
    canceling: '取消中',
    completed: '已完成',
    failed: '失败',
    canceled: '已取消',
  })[status] ?? status

function TaskResult({ text }: { text: string }) {
  return (
    <Disclosure className="task-result" label="查看结果">
      <Suspense fallback={<p role="status">正在加载结果…</p>}>
        <ConversationMarkdown text={text} />
      </Suspense>
    </Disclosure>
  )
}

export default function App() {
  const navigate = useNavigate(),
    queryClient = useQueryClient()
  const route = useLocation()
  const inMcpSettings = route.pathname === '/settings/mcp'
  const from: unknown = route.state?.returnTo
  const returnTo = typeof from === 'string' && matchPath('/sessions/:id', from) ? from : '/new'
  const id = matchPath('/sessions/:id', inMcpSettings ? returnTo : route.pathname)?.params.id
  const reducedMotion = useReducedMotion()
  const toastMotion = useSurfaceMotion({ y: 8 }, 0.18)
  const [theme, setTheme] = useState(() =>
    localStorage.getItem('paicli-theme') === 'dark' ? 'dark' : 'light',
  )
  const [settings, setSettings] = useState(false),
    [search, setSearch] = useState(false),
    [tasksOpen, setTasksOpen] = useState(false)
  const [searchTerm, setSearchTerm] = useState(''),
    [details, setDetails] = useState(false),
    [sidebar, setSidebar] = useState(false)
  const [renameDraft, setRenameDraft] = useState<string | null>(null)
  const [renaming, setRenaming] = useState(false)
  const [renameError, setRenameError] = useState('')
  const [mobile, setMobile] = useState(() => matchMedia(navigationDrawer).matches)
  const [detailsOverlay, setDetailsOverlay] = useState(() => matchMedia(detailsDrawer).matches)
  const [detailsTab, setDetailsTab] = useState<DetailsTab>('changes')
  const [agentSelection, setAgentSelection] = useState<{ key: string; path: Subagent[] }>()
  const [drafts, setDrafts] = useTabDrafts()
  const { recent: recentWorkspaces, remember: rememberWorkspace } = useRecentWorkspaces()
  const [mode, setMode] = useState<Mode>('react')
  const [submitting, setSubmitting] = useState(false),
    [retry, setRetry] = useState(0),
    [notice, setNotice] = useState('')
  const [newPermissionMode, setNewPermissionMode] = useState<PermissionMode>('default')
  const [changingPermission, setChangingPermission] = useState(false)
  const [cancelRequested, setCancelRequested] = useState<string>(),
    [tool, setTool] = useState<ToolView | null>(null),
    [taskPrompt, setTaskPrompt] = useState('')
  const [taskMode, setTaskMode] = useState<Mode>('react')
  const [pendingTurns, setPendingTurns] = useState<Record<string, PendingTurn>>({})
  const [nearBottom, setNearBottom] = useState(true)
  const scrollRef = useRef<HTMLDivElement>(null),
    sendLock = useRef(false)
  const searchInput = useRef<HTMLInputElement>(null)
  const searchResults = useRef<HTMLDivElement>(null)
  const navigationTrigger = useRef<HTMLButtonElement>(null)
  const focusComposerAfterNavigation = useRef(false)
  const detailsTrigger = useRef<HTMLButtonElement>(null)
  const approvalFocus = useRef<string | null>(null)
  const previousRuntimeError = useRef(false)
  const draftKey = id ?? 'new',
    draft = drafts[draftKey] ?? ''
  const setDraft = (value: string) => setDrafts((previous) => ({ ...previous, [draftKey]: value }))
  const sessions = useQuery({
    queryKey: ['sessions'],
    queryFn: ({ signal }) => client.sessions(signal),
    refetchInterval: 3500,
  })
  const health = useQuery({
    queryKey: ['health'],
    queryFn: ({ signal }) => client.health(signal),
    // A cached placeholder keeps a failed first check visible during refetches.
    initialData: null,
    initialDataUpdatedAt: 0,
    retry: false,
    refetchInterval: 4000,
    refetchOnWindowFocus: true,
    refetchOnReconnect: true,
  })
  const changes = useQuery({
    queryKey: ['changes', id],
    queryFn: () => client.changes(id!),
    enabled: Boolean(id) && client.capabilities.changes,
  })
  const tasks = useQuery({
    queryKey: ['tasks'],
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
  const feed = useEventFeed(client, id, retry)
  const { refetch: checkHealth, isError: runtimeOffline } = health
  const runtimeReady = health.isSuccess && health.data !== null
  const connectionError = runtimeOffline ? errorMessage(health.error) : feed.error
  const connectionSyncing = !runtimeOffline && (!runtimeReady || feed.syncing)
  const connectionLabel = runtimeOffline
    ? '连接异常'
    : feed.error
      ? '会话未同步'
      : connectionSyncing
        ? '连接中'
        : '本地连接'
  const reconnect = () => {
    void checkHealth()
    void sessions.refetch()
    if (settings) void bridge.refetch()
    if (id && !runtimeOffline && feed.error) setRetry((value) => value + 1)
  }
  const permissionMode = id ? (feed.permissionMode ?? 'default') : newPermissionMode
  const turns = useMemo(
    () => projectTurns(feed.events, feed.preview, pending),
    [feed.events, feed.preview, pending],
  )
  const currentTurn = turns.at(-1)
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
  const focusApproval = (approvalId: string) => {
    const card = document.getElementById(approvalId)
    card?.focus({ preventScroll: true })
    card?.scrollIntoView({ block: 'nearest' })
  }
  const showApproval = (approvalId: string) => {
    if (detailsOverlay) {
      approvalFocus.current = approvalId
      setDetails(false)
    } else focusApproval(approvalId)
  }
  const optimistic = turns.find((turn) => turn.optimistic)
  const activeRun = resolveActiveRun(feed, pending)
  const runId = activeRun?.run_id
  const pendingFinished = Boolean(pending?.run && runId !== pending.run.id)
  const unmanagedRun = feed.status === 'running' && !runId && (!pending || pendingFinished)
  const busy = Boolean(runId || feed.status === 'running' || (pending && !pendingFinished))
  const stopping = activeRun?.status === 'canceling' || Boolean(runId && cancelRequested === runId)
  const visibleSessions = useMemo(
    () =>
      (sessions.data ?? []).map((session) =>
        session.id === id && feed.connectedAt !== undefined && feed.cursor >= session.last_sequence
          ? { ...session, title: feed.title ?? session.title, status: feed.status }
          : session,
      ),
    [sessions.data, id, feed.title, feed.status, feed.cursor, feed.connectedAt],
  )
  const current = visibleSessions.find((session) => session.id === id)
  const runtimeCwd = health.data?.cwd ?? ''
  const newSessionCwd = recentWorkspaces[0] ?? runtimeCwd
  const sessionCwd = feed.cwd ?? current?.cwd
  const searchQuery = searchTerm.trim().toLowerCase()
  const searchMatches = search
    ? visibleSessions.filter((session) =>
        `${sessionTitle(session)} ${session.id} ${session.cwd ?? ''}`
          .toLowerCase()
          .includes(searchQuery),
      )
    : []
  const title = id ? sessionTitle(current ?? { id, title: feed.title }) : '新会话'
  const editedTitle = renameDraft?.trim().replace(/\s+/g, ' ') ?? ''
  const titleTooLong = [...editedTitle].length > maxTitleLength
  const visibleTool = tool
    ? (turns.flatMap((t) => t.tools).find((t) => t.id === tool.id) ?? tool)
    : null
  const notify = (message: string) => setNotice(message)
  useLayoutEffect(() => {
    document.documentElement.dataset.theme = theme
    const themeColor = getComputedStyle(document.documentElement).getPropertyValue('--background')
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', themeColor.trim())
    localStorage.setItem('paicli-theme', theme)
  }, [theme])
  useEffect(() => {
    const navigationQuery = matchMedia(navigationDrawer)
    const detailsQuery = matchMedia(detailsDrawer)
    const updateNavigation = (event: MediaQueryListEvent) => {
      setMobile(event.matches)
      if (!event.matches) setSidebar(false)
    }
    const updateDetails = (event: MediaQueryListEvent) => setDetailsOverlay(event.matches)
    navigationQuery.addEventListener('change', updateNavigation)
    detailsQuery.addEventListener('change', updateDetails)
    return () => {
      navigationQuery.removeEventListener('change', updateNavigation)
      detailsQuery.removeEventListener('change', updateDetails)
    }
  }, [])
  useEffect(() => {
    if (feed.error) void checkHealth()
  }, [checkHealth, feed.error])
  useEffect(() => {
    if (previousRuntimeError.current && runtimeReady && feed.error)
      setRetry((value) => value + 1)
    previousRuntimeError.current = runtimeOffline
  }, [runtimeOffline, runtimeReady, feed.error])
  useEffect(() => {
    if (location.pathname === '/') navigate('/new', { replace: true })
  }, [navigate])
  useEffect(() => {
    setTool(null)
    setAgentSelection(undefined)
    setRenameDraft(null)
    if (!id) setDetails(false)
    setNearBottom(true)
    scrollRef.current?.scrollTo({ top: 0 })
  }, [id])
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
  }, [draftKey, pending, pendingFinished, optimistic?.status, optimistic?.id, setDrafts])
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
      if (inMcpSettings) return
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setSearch((value) => !value)
      }
      if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === 'o') {
        event.preventDefault()
        navigate('/new')
        window.dispatchEvent(new Event('paicli:focus'))
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [navigate, inMcpSettings])
  const toggleTheme = () => {
    const updateTheme = () => setTheme((current) => (current === 'light' ? 'dark' : 'light'))
    if (!document.startViewTransition || matchMedia('(prefers-reduced-motion: reduce)').matches) {
      updateTheme()
      return
    }
    // Commit the theme and its controls before the browser takes the new snapshot.
    document.startViewTransition(() => flushSync(updateTheme))
  }
  const selectSession = (sessionId: string) => {
    navigate(`/sessions/${encodeURIComponent(sessionId)}`)
    setSidebar(false)
    setSearch(false)
    setSearchTerm('')
  }
  const renameSession = async () => {
    if (!id || !editedTitle || titleTooLong || renaming) return
    setRenaming(true)
    setRenameError('')
    try {
      await client.renameSession(id, editedTitle)
      await sessions.refetch()
      setRenameDraft(null)
    } catch (error) {
      setRenameError(errorMessage(error))
    } finally {
      setRenaming(false)
    }
  }
  const setPermissionMode = async (next: PermissionMode) => {
    if (changingPermission || submitting || next === permissionMode) return
    if (!id) {
      setNewPermissionMode(next)
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
    if (
      !draft.trim() ||
      busy ||
      connectionSyncing ||
      connectionError ||
      changingPermission ||
      sendLock.current
    )
      return
    sendLock.current = true
    setSubmitting(true)
    const message = draft.trim(),
      key = draftKey
    const queued = { message, afterSequence: feed.cursor }
    setPendingTurns((previous) => ({ ...previous, [key]: queued }))
    setNearBottom(true)
    let sessionId = id
    let submittedKey = key
    let submittedDraftKey = key
    try {
      if (!sessionId) {
        const session = await client.createSession(newSessionCwd || undefined)
        sessionId = session.id
        if (permissionMode === 'auto') await client.setPermissionMode(sessionId, 'auto')
        setNewPermissionMode('default')
        submittedKey = sessionId
        setPendingTurns((previous) => {
          const next = { ...previous, [submittedKey]: queued }
          delete next[key]
          return next
        })
        void queryClient.invalidateQueries({ queryKey: ['sessions'] })
        if (location.pathname === '/new') {
          submittedDraftKey = sessionId
          setDrafts((previous) => ({
            ...previous,
            [session.id]: previous[key] ?? draft,
            [key]: '',
          }))
          navigate(`/sessions/${encodeURIComponent(sessionId)}`)
        }
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
      const acceptedDraftKey = submittedDraftKey
      setDrafts((previous) =>
        previous[acceptedDraftKey] === draft ? { ...previous, [acceptedDraftKey]: '' } : previous,
      )
      void queryClient.invalidateQueries({ queryKey: ['sessions'] })
    } catch (error) {
      setPendingTurns((previous) => {
        const next = { ...previous }
        delete next[submittedKey]
        return next
      })
      void checkHealth()
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
      setCancelRequested(runId)
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
    if (mobile && sidebar) focusComposerAfterNavigation.current = true
    else window.dispatchEvent(new Event('paicli:focus'))
  }
  const addTask = async () => {
    if (!taskPrompt.trim() || submitting || !runtimeReady) return
    setSubmitting(true)
    try {
      await client.addTask(taskPrompt.trim(), taskMode)
      setTaskPrompt('')
      await tasks.refetch()
    } catch (error) {
      notify(errorMessage(error))
    } finally {
      setSubmitting(false)
    }
  }
  const grouped = visibleSessions.reduce<Record<string, Session[]>>((groups, session) => {
    const today = new Date().toDateString(),
      yesterday = new Date(Date.now() - 86400000).toDateString(),
      date = new Date(session.updated_at).toDateString()
    const label = date === today ? '今天' : date === yesterday ? '昨天' : '更早'
    ;(groups[label] ??= []).push(session)
    return groups
  }, {})
  const project = health.data?.cwd.split(/[\\/]/).filter(Boolean).at(-1) ?? 'PaiCLI-Python'
  const SessionHeading = id ? 'h1' : 'span'
  const detailPanel = details && (
    <Details
      changes={changes.data ?? []}
      fileTools={turns.flatMap((turn) => turn.tools)}
      tool={visibleTool}
      tab={detailsTab}
      onTab={setDetailsTab}
      subagentCount={feed.subagents?.length ?? 0}
      subagents={
        <Suspense
          fallback={
            <div className="loading-state" role="status">
              正在加载…
            </div>
          }
        >
          <SubagentsPane
            key={draftKey}
            agents={feed.subagents}
            agent={visibleAgent}
            rootId={id}
            client={client}
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
        </Suspense>
      }
      onClose={() => {
        setDetails(false)
        if (!detailsOverlay) detailsTrigger.current?.focus({ preventScroll: true })
      }}
      onClearTool={() => setTool(null)}
      onInspect={setTool}
    />
  )
  const navigation = (
    <aside className="sidebar" aria-label="主导航">
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
          <span>PaiCLI</span>
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
      <Button
        variant="accent"
        className="new-chat"
        onClick={openNewConversation}
        aria-keyshortcuts="Control+Shift+O"
      >
        <Plus size={17} />
        新建会话
      </Button>
      <nav className="primary-nav">
        <button onClick={() => setSearch(true)} aria-keyshortcuts="Control+K">
          <Search size={16} />
          搜索会话
        </button>
        <button onClick={() => setTasksOpen(true)}>
          <Inbox size={16} />
          后台任务
        </button>
        <button
          onClick={() => {
            setSidebar(false)
            navigate('/settings/mcp', { state: { returnTo: route.pathname } })
          }}
        >
          <Plug size={16} aria-hidden="true" />
          连接器
        </button>
      </nav>
      <div className="project-selector" title={health.data?.cwd ?? project}>
        <Folder size={15} aria-hidden="true" />
        <strong>{project}</strong>
      </div>
      <div className="session-navigation">
        {!runtimeOffline && sessions.isLoading && (
          <div className="sidebar-loading">
            <LoaderCircle size={15} className="spin" />
            加载会话…
          </div>
        )}
        {sessions.error && !runtimeOffline && (
          <div className="sidebar-error">
            <CircleAlert size={15} />
            <p>{errorMessage(sessions.error)}</p>
            <button onClick={reconnect}>重试</button>
          </div>
        )}
        {!runtimeOffline && !sessions.isLoading && !sessions.error && !sessions.data?.length && (
          <p className="sidebar-empty">暂无会话</p>
        )}
        {Object.entries(grouped).map(([label, items]) => (
          <section className="session-group" key={label}>
            <h3>{label}</h3>
            {items.map((session) => (
              <button
                className={`session-item ${session.id === id ? 'selected' : ''}`}
                aria-current={session.id === id ? 'page' : undefined}
                title={[sessionTitle(session), session.cwd].filter(Boolean).join('\n')}
                key={session.id}
                onClick={() => selectSession(session.id)}
              >
                <span>{sessionTitle(session)}</span>
                {session.status === 'running' && (
                  <span className="status-dot pulse" aria-label="运行中" />
                )}
              </button>
            ))}
          </section>
        ))}
      </div>
      <div className="sidebar-bottom">
        {currentTurn && <TurnUsageSummary usage={currentTurn.usage} current />}
        <Button
          variant="ghost"
          size="icon"
          aria-label={theme === 'light' ? '切换深色主题' : '切换浅色主题'}
          onClick={toggleTheme}
        >
          {theme === 'light' ? <Moon size={16} /> : <Sun size={16} />}
        </Button>
      </div>
    </aside>
  )
  const workspace = (
    <div className="workspace" style={inMcpSettings ? { display: 'none' } : undefined}>
      {mobile ? (
        <Dialog
          open={sidebar}
          onOpenChange={setSidebar}
          title="主导航"
          side="left"
          restoreFocus={() => {
            if (focusComposerAfterNavigation.current) {
              focusComposerAfterNavigation.current = false
              window.dispatchEvent(new Event('paicli:focus'))
            } else navigationTrigger.current?.focus()
          }}
        >
          {navigation}
        </Dialog>
      ) : (
        navigation
      )}
      <main className="main-panel">
        <header className="topbar">
          <div className="topbar-leading">
            <Button
              variant="ghost"
              size="icon"
              className="mobile-menu"
              ref={navigationTrigger}
              aria-label="打开导航"
              aria-haspopup="dialog"
              onClick={() => setSidebar(true)}
            >
              <Menu size={18} />
            </Button>
            <SessionHeading className="breadcrumb-title" title={title}>
              {title}
            </SessionHeading>
            {id && (
              <Button
                variant="ghost"
                size="icon"
                className="rename-session"
                aria-label="重命名会话"
                title="重命名会话"
                onClick={() => {
                  setRenameError('')
                  setRenameDraft(current?.title ?? feed.title ?? '')
                }}
              >
                <Pencil size={13} aria-hidden="true" />
              </Button>
            )}
          </div>
          <div className="topbar-actions">
            <button
              className="source-pill"
              data-state={connectionError ? 'offline' : connectionSyncing ? 'pending' : 'ready'}
              onClick={() => setSettings(true)}
            >
              <span aria-hidden="true" />
              {connectionLabel}
              <ChevronDown size={12} aria-hidden="true" />
            </button>
            <Button
              variant="ghost"
              size="icon"
              ref={detailsTrigger}
              aria-label={details ? '隐藏详情面板' : '显示详情面板'}
              aria-pressed={details}
              onClick={() => setDetails(!details)}
            >
              <PanelRight size={17} />
            </Button>
          </div>
        </header>
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
                <h1>今天想做什么？</h1>
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
                        setDrafts((previous) => {
                          const currentDraft = previous[draftKey]
                          return {
                            ...previous,
                            [draftKey]: currentDraft?.trim()
                              ? `${currentDraft.trimEnd()}\n\n${item.prompt}`
                              : item.prompt,
                          }
                        })
                        window.dispatchEvent(new Event('paicli:focus'))
                      }}
                    >
                      <item.icon size={16} aria-hidden="true" />
                      <span>{item.title}</span>
                    </button>
                  ))}
                </div>
              </section>
            ) : (
              <>
                {!runtimeOffline && feed.loading && !pending && (
                  <div className="loading-state">
                    <LoaderCircle className="spin" size={18} />
                    正在恢复会话…
                  </div>
                )}
                {!runtimeOffline && !feed.loading && !feed.error && !busy && turns.length === 0 && (
                  <div className="empty-conversation">
                    <AgentMark />
                    <h2>准备就绪</h2>
                  </div>
                )}
                <Suspense
                  fallback={
                    <div className="loading-state" role="status">
                      正在加载对话…
                    </div>
                  }
                >
                  {turns.map((turn) => (
                    <Turn
                      key={turn.id}
                      turn={turn}
                      unmanaged={unmanagedRun || !busy}
                      canApprove={client.capabilities.approvals && !runtimeOffline}
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
                </Suspense>
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
                behavior: reducedMotion ? 'instant' : 'smooth',
              })
            }}
          >
            <ArrowDown size={14} />
            回到最新
          </Button>
        )}
        <Composer
          workspace={
            id ? (
              sessionCwd && <SessionWorkspace path={sessionCwd} />
            ) : (
              <WorkspacePicker
                client={client}
                defaultPath={runtimeCwd}
                path={newSessionCwd}
                recent={recentWorkspaces}
                disabled={connectionSyncing || runtimeOffline || submitting}
                onSelect={rememberWorkspace}
              />
            )
          }
          draft={draft}
          setDraft={setDraft}
          mode={mode}
          setMode={setMode}
          permissionMode={permissionMode}
          setPermissionMode={(next) => void setPermissionMode(next)}
          changingPermission={changingPermission}
          busy={busy}
          unmanagedRun={unmanagedRun}
          submitting={submitting}
          syncing={connectionSyncing}
          connectionError={connectionError}
          onReconnect={reconnect}
          canStop={client.capabilities.cancelTurn && Boolean(id && runId)}
          stopping={stopping}
          onSend={() => void send()}
          onStop={() => void stop()}
          onNewConversation={openNewConversation}
        />
      </main>
      {detailsOverlay ? (
        <Dialog
          open={details}
          onOpenChange={setDetails}
          title="工作详情"
          side="right"
          restoreFocus={() => {
            const target = approvalFocus.current
            approvalFocus.current = null
            if (target) focusApproval(target)
            else detailsTrigger.current?.focus()
          }}
        >
          {detailPanel}
        </Dialog>
      ) : (
        <div className="details-slot" inert={!details}>
          <AnimatePresence>
            {details && (
              <m.div
                className="details-slide"
                initial={reducedMotion ? false : { width: 0 }}
                animate={{ width: 'var(--details-track)' }}
                exit={{
                  width: 0,
                  transition: { duration: reducedMotion ? 0 : 0.2, ease: easeOut },
                }}
                transition={{ duration: reducedMotion ? 0 : 0.26, ease: easeOut }}
              >
                {detailPanel}
              </m.div>
            )}
          </AnimatePresence>
        </div>
      )}
      <AnimatePresence>
        {notice && (
          <m.div className="toast" role="status" key="notice" {...toastMotion}>
            <CircleAlert size={16} />
            <span>{notice}</span>
            <button aria-label="关闭提示" onClick={() => setNotice('')}>
              <X size={15} />
            </button>
          </m.div>
        )}
      </AnimatePresence>
      <Dialog
        open={renameDraft !== null}
        onOpenChange={(open) => {
          if (!open && !renaming) setRenameDraft(null)
        }}
        title="重命名会话"
      >
        <form
          className="session-title-form"
          onSubmit={(event) => {
            event.preventDefault()
            void renameSession()
          }}
        >
          <input
            value={renameDraft ?? ''}
            onChange={(event) => {
              setRenameError('')
              setRenameDraft(event.target.value)
            }}
            disabled={renaming}
            aria-label="会话名称"
            aria-invalid={titleTooLong}
            aria-describedby={titleTooLong ? 'session-title-limit' : undefined}
            placeholder="输入会话名称"
          />
          <Button size="sm" disabled={renaming || !editedTitle || titleTooLong}>
            {renaming ? '保存中…' : '保存'}
          </Button>
        </form>
        {titleTooLong && (
          <p className="inline-failure" id="session-title-limit">
            会话名称最多 {maxTitleLength} 个字符。
          </p>
        )}
        {renameError && (
          <p className="inline-failure" role="alert">
            {renameError}
          </p>
        )}
      </Dialog>
      <Dialog open={settings} onOpenChange={setSettings} title="本地 Runtime 连接">
        <div className="settings-section">
          <div className="settings-status">
            <span
              className={`connection-dot ${connectionError ? 'offline' : ''}`}
              aria-hidden="true"
            />
            {connectionLabel}
          </div>
          {connectionError && <p>{connectionError}</p>}
          {runtimeOffline && (
            <ol className="connection-steps">
              <li>停止当前工作台。若前端和 Runtime 分开启动，请先停止两者。</li>
              <li>
                在项目根目录重新启动工作台
                <code className="command">uv run paicli workbench --cwd .</code>
              </li>
            </ol>
          )}
          {runtimeOffline && bridge.isError && <p>当前页面无法访问本地工作台代理。</p>}
          {!runtimeOffline && health.data && <p className="workspace-path">{health.data.cwd}</p>}
        </div>
        <div className="settings-footer">
          <Button size="sm" onClick={reconnect} disabled={health.isFetching}>
            {health.isFetching ? '检测中…' : '重新检测'}
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
        description="按标题或 ID 查找最近 100 条会话。"
      >
        <div className="search-input">
          <Search size={18} />
          <input
            ref={searchInput}
            placeholder="搜索你的会话…"
            aria-label="搜索会话"
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            onKeyDown={(e) => {
              if (e.nativeEvent.isComposing || !searchMatches.length) return
              if (e.key === 'Enter') {
                e.preventDefault()
                selectSession(searchMatches[0].id)
              } else if (e.key === 'ArrowDown') {
                e.preventDefault()
                searchResults.current?.querySelector('button')?.focus()
              }
            }}
          />
        </div>
        <div className="search-results" ref={searchResults}>
          {searchMatches.map((session) => (
            <button
              key={session.id}
              onClick={() => selectSession(session.id)}
              onKeyDown={(event) => {
                if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
                event.preventDefault()
                const row = event.currentTarget
                const next =
                  event.key === 'ArrowDown' ? row.nextElementSibling : row.previousElementSibling
                if (next instanceof HTMLButtonElement) next.focus()
                else if (event.key === 'ArrowUp') searchInput.current?.focus()
              }}
            >
              <span>{sessionTitle(session)}</span>
              <ArrowRight size={14} />
            </button>
          ))}
          {searchMatches.length === 0 && <p>没有找到匹配的会话。</p>}
        </div>
      </Dialog>
      <Dialog open={tasksOpen} onOpenChange={setTasksOpen} title="后台任务">
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
          <ModePicker
            label="后台任务执行模式"
            value={taskMode}
            onValueChange={setTaskMode}
            className="task-mode"
          />
          <Button
            type="submit"
            size="sm"
            disabled={!taskPrompt.trim() || submitting || !runtimeReady}
          >
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
                <strong>{task.prompt}</strong>
                <span className={`task-status ${task.status}`}>{statusLabel(task.status)}</span>
              </div>
              {task.result && <TaskResult text={task.result} />}
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
  return (
    <>
      {inMcpSettings && (
        <McpSettings onBack={() => navigate(returnTo)} onConnection={() => setSettings(true)} />
      )}
      {workspace}
    </>
  )
}
