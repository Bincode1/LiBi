import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  Activity, Bot, CircleAlert, Clock3, FileSearch, LoaderCircle, MessageSquarePlus,
  PanelRight, Send, Settings2, Sparkles, Terminal, X,
} from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Separator } from "@/components/ui/separator"
import { Textarea } from "@/components/ui/textarea"
import "./App.css"

type Session = { id: string; created_at: string; updated_at: string; status: string; last_sequence: number }
type RuntimeEvent = {
  id: string; sequence: number; type: string; payload: Record<string, unknown>
  created_at: string; source?: string; agent_id?: string; agent_session_id?: string; turn_id?: string; step_id?: string; tool_call_id?: string
}
type AgentNode = { id: string; parentId: string; description?: string; state: string; phase?: string; updated: string; error?: string }
type TurnGroup = { id: string; user?: RuntimeEvent; assistant?: RuntimeEvent; process: RuntimeEvent[]; startedAt?: string; completed: boolean }
const KEY_STORAGE = "paicli-runtime-key"

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { "x-api-key": sessionStorage.getItem(KEY_STORAGE) ?? "" }
  if (init?.body) headers["content-type"] = "application/json"
  Object.assign(headers, init?.headers ?? {})
  const response = await fetch(path, { ...init, headers })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}))
    throw new Error(payload.error ?? `Request failed (${response.status})`)
  }
  return response.json() as Promise<T>
}

const timeLabel = (value: string) => new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
function statusTone(status: string) {
  const value = status.toLowerCase()
  if (value.includes("fail") || value.includes("error")) return "danger"
  if (value.includes("complete")) return "done"
  if (value.includes("wait") || value.includes("idle")) return "wait"
  if (value.includes("run")) return "live"
  return "neutral"
}
function eventTitle(event: RuntimeEvent) {
  const names: Record<string, string> = {
    "step/started": "Thinking", "step/completed": "Thinking complete",
    "tool/execution_started": "Tool call", "tool/result": "Tool result",
    "agent/subagent_catalog": "Subagent ready", "agent/child_started": "Subagent started",
    "agent/child_completed": "Subagent completed", "agent/child_failed": "Subagent failed",
    "user/message": "Your request", "assistant/message": "PaiCLI",
  }
  return names[event.type] ?? event.type.replaceAll("/", " · ")
}
function eventSummary(event: RuntimeEvent) {
  const p = event.payload
  if (event.type === "user/message" || event.type === "assistant/message") return String(p.content ?? "")
  if (event.type.includes("tool")) return `${String(p.name ?? "tool")}${p.is_error ? " · returned an error" : ""}`
  if (event.type.includes("child")) return String(p.description ?? p.result ?? p.error_code ?? p.child_id ?? "")
  return String(p.reason ?? p.mode ?? p.status ?? "")
}
function reasoningSummary(event: RuntimeEvent) {
  const reason = String(event.payload.reason ?? "").trim()
  if (reason) return reason
  const mode = String(event.payload.mode ?? "").trim()
  return mode
    ? `PaiCLI is analyzing the request in ${mode} mode and choosing the next action.`
    : "PaiCLI is analyzing the request and choosing the next action."
}
function toolLabel(event: RuntimeEvent) {
  const name = String(event.payload.name ?? event.payload.tool ?? "tool")
  const labels: Record<string, string> = {
    read_file: "Reading files", write_file: "Writing files", edit_file: "Editing files",
    search: "Searching code", bash: "Running commands", exec: "Running commands",
    spawn_subagent: "Coordinating subagents",
  }
  return labels[name] ?? name.replaceAll("_", " ")
}
function processLabel(event: RuntimeEvent) {
  if (event.type === "step/started") return "Analyzing the request"
  if (event.type.startsWith("tool/")) return toolLabel(event)
  if (event.type === "agent/child_started") return "Subagent started"
  if (event.type === "agent/child_completed") return "Subagent completed"
  if (event.type === "agent/child_failed") return "Subagent failed"
  return eventTitle(event)
}
function isVisibleEvent(event: RuntimeEvent) {
  const toolName = String(event.payload.name ?? "")
  return event.type === "user/message"
    || event.type === "step/started"
    || (event.type.startsWith("tool/") && event.type !== "tool/schema_snapshot" && toolName !== "schema_snapshot")
    || ["agent/child_started", "agent/child_completed", "agent/child_failed"].includes(event.type)
    || event.type.includes("failed")
}
function projectTurns(events: RuntimeEvent[]): TurnGroup[] {
  const groups: TurnGroup[] = []
  for (const event of events) {
    const childEvent = Boolean(event.agent_id || event.source?.startsWith("child:"))
    if (childEvent) continue
    if (!isVisibleEvent(event) && event.type !== "assistant/message") continue
    const existing = !childEvent && event.turn_id ? groups.find((group) => group.id === event.turn_id) : undefined
    const group = existing ?? (!childEvent && event.type === "user/message" || groups.length === 0 ? (() => {
      const next: TurnGroup = { id: event.turn_id ?? `turn-${groups.length + 1}`, process: [], completed: false }
      groups.push(next)
      return next
    })() : groups[groups.length - 1])
    if (!group.startedAt) group.startedAt = event.created_at
    if (event.type === "user/message") group.user = event
    else if (event.type === "assistant/message") { group.assistant = event; group.completed = true }
    else group.process.push(event)
  }
  return groups
}
function compactProcess(events: RuntimeEvent[]) {
  const compacted: RuntimeEvent[] = []
  const pendingToolIds = new Set<string>()
  const pendingToolNames = new Set<string>()
  for (const event of events.filter(isVisibleEvent)) {
    if (event.type === "tool/execution_started") {
      const callId = String(event.tool_call_id ?? event.payload.tool_call_id ?? "")
      const name = String(event.payload.name ?? "")
      if (callId) pendingToolIds.add(callId)
      if (name) pendingToolNames.add(name)
    }
    if (event.type === "tool/result") {
      const callId = String(event.tool_call_id ?? event.payload.tool_call_id ?? "")
      const name = String(event.payload.name ?? "")
      const matched = (callId && pendingToolIds.has(callId)) || (!callId && name && pendingToolNames.has(name))
      if (matched) {
        if (callId) pendingToolIds.delete(callId)
        if (name) pendingToolNames.delete(name)
        continue
      }
    }
    const previous = compacted.at(-1)
    if (event.type === "step/started" && previous?.type === "step/started") continue
    compacted.push(event)
  }
  return compacted
}

function App() {
  const [sessions, setSessions] = useState<Session[]>([])
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [events, setEvents] = useState<RuntimeEvent[]>([])
  const [, setCursor] = useState(0)
  const [agents, setAgents] = useState<Map<string, AgentNode>>(new Map())
  const [message, setMessage] = useState("")
  const [mode, setMode] = useState("react")
  const [selectedEvent, setSelectedEvent] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [showSettings, setShowSettings] = useState(false)
  const [apiKey, setApiKey] = useState(sessionStorage.getItem(KEY_STORAGE) ?? "")
  const scrollRef = useRef<HTMLDivElement>(null)
  const activeSessionRef = useRef<string | null>(null)
  const cursorRef = useRef(0)

  const ingest = useCallback((incoming: RuntimeEvent[]) => {
    setEvents((previous) => {
      const seen = new Set(previous.map((event) => event.id))
      return [...previous, ...incoming.filter((event) => !seen.has(event.id))]
    })
    setAgents((previous) => {
      const next = new Map(previous)
      for (const event of incoming) {
        const p = event.payload
        const id = event.agent_id ?? String(p.child_id ?? "")
        if (!id) continue
        const agent = next.get(id) ?? { id, parentId: String(p.parent_id ?? "root"), state: "running", updated: event.created_at }
        const updated = { ...agent, updated: event.created_at }
        if (event.type === "agent/subagent_catalog") Object.assign(updated, { description: p.description ?? p.label, parentId: p.parent_id ?? "root", state: "running" })
        if (event.type === "agent/child_completed") Object.assign(updated, { state: "completed", phase: "ended" })
        if (event.type === "agent/child_failed") Object.assign(updated, { state: "failed", phase: "ended", error: p.error_code ?? p.result })
        if (event.type === "step/started") updated.phase = "reasoning"
        if (event.type === "tool/execution_started") updated.phase = `tool: ${String(p.name ?? "running")}`
        if (event.type === "tool/result") updated.phase = "processing result"
        next.set(id, updated)
      }
      return next
    })
  }, [])

  const loadSessions = useCallback(async () => {
    try {
      const response = await api<{ sessions: Session[] }>("/v1/sessions")
      setSessions(response.sessions); setConnected(true); setError(null)
      if (!activeSessionRef.current && response.sessions[0]) {
        activeSessionRef.current = response.sessions[0].id
        cursorRef.current = 0
        setCursor(0)
        setSessionId(response.sessions[0].id)
      }
    } catch (caught) {
      setConnected(false); setError(caught instanceof Error ? caught.message : "Runtime unavailable")
    }
  }, [])

  const poll = useCallback(async () => {
    if (!sessionId) return
    try {
      const requestedSession = sessionId
      const response = await api<{ events: RuntimeEvent[]; next_cursor: number; state: { status: string } }>(`/v1/sessions/${requestedSession}/events.json?cursor=${cursorRef.current}`)
      if (activeSessionRef.current !== requestedSession) return
      ingest(response.events); setCursor(response.next_cursor)
      cursorRef.current = response.next_cursor
      setSessions((previous) => previous.map((session) => session.id === requestedSession ? { ...session, status: response.state.status, last_sequence: response.next_cursor } : session))
      setConnected(true)
    } catch (caught) { setConnected(false); setError(caught instanceof Error ? caught.message : "Could not read events") }
  }, [ingest, sessionId])

  useEffect(() => { void loadSessions() }, [loadSessions])
  useEffect(() => {
    if (!sessionId) return
    void poll()
    const timer = window.setInterval(() => void poll(), 900)
    return () => window.clearInterval(timer)
  }, [poll, sessionId])
  useEffect(() => { scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" }) }, [events.length])

  const selectSession = (id: string) => {
    activeSessionRef.current = id
    cursorRef.current = 0
    setSessionId(id); setEvents([]); setAgents(new Map()); setCursor(0); setSelectedEvent(null)
  }
  const createSession = async () => {
    try { const response = await api<{ id: string }>("/v1/sessions", { method: "POST", body: "{}" }); await loadSessions(); selectSession(response.id) }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Unable to create session") }
  }
  const submit = async () => {
    const prompt = message.trim()
    if (!prompt || loading) return
    setLoading(true)
    try {
      let activeSession = sessionId
      if (!activeSession) {
        const created = await api<{ id: string }>("/v1/sessions", { method: "POST", body: "{}" })
        activeSession = created.id; activeSessionRef.current = activeSession; cursorRef.current = 0; setSessionId(activeSession)
      }
      await api(`/v1/sessions/${activeSession}/turns/async`, { method: "POST", body: JSON.stringify({ message: prompt, mode }) })
      setMessage(""); await loadSessions()
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Unable to start run") }
    finally { setLoading(false) }
  }
  const saveKey = () => { sessionStorage.setItem(KEY_STORAGE, apiKey.trim()); setShowSettings(false); void loadSessions() }
  const currentSession = sessions.find((session) => session.id === sessionId)
  const childAgents = useMemo(() => [...agents.values()], [agents])
  const liveStatus = currentSession?.status ?? (loading ? "running" : "created")
  const turnGroups = useMemo(() => projectTurns(events), [events])

  return (
    <>
    <div className="workspace-shell">
        <aside className="workspace-sidebar">
          <div className="brand-lockup"><div className="brand-symbol">P</div><div><div className="brand-name">PaiCLI</div><div className="brand-caption">Agent workspace</div></div></div>
          <Button className="new-session-button" onClick={() => void createSession()}><MessageSquarePlus size={16} /> new session</Button>
          <div className="sidebar-label">RECENT SESSIONS</div>
          <ScrollArea className="session-scroll"><div className="session-list">{sessions.map((session) => <button className={`session-item ${session.id === sessionId ? "selected" : ""}`} key={session.id} onClick={() => selectSession(session.id)}><span className="session-icon"><Terminal size={14} /></span><span className="session-copy"><strong>{session.id}</strong></span></button>)}</div></ScrollArea>
          <div className="sidebar-bottom"><div className="runtime-state"><span className={connected ? "connection-dot live" : "connection-dot"}></span>{connected ? "Runtime connected" : "Runtime offline"}</div><Button variant="ghost" size="icon" title="Runtime API key" onClick={() => setShowSettings(true)}><Settings2 size={16} /></Button></div>
        </aside>

        <main className="conversation-panel">
          <header className="conversation-header"><div><div className="overline">SESSION / {currentSession ? currentSession.id.slice(-10) : "NEW"}</div><h1>{currentSession?.id || "What should PaiCLI work on?"}</h1></div><div className="header-actions"><Badge className={`status-badge ${statusTone(liveStatus)}`}><span className="status-pip"></span>{liveStatus}</Badge></div></header>
          {error && <div className="error-strip"><CircleAlert size={15} />{error}<Button variant="ghost" size="icon" onClick={() => setError(null)}><X size={14} /></Button></div>}
          <div className="conversation-scroll" ref={scrollRef}>
            {!events.length && !loading ? <div className="welcome-state"><div className="welcome-icon"><Sparkles size={25} /></div><h2>Run your agent from the browser</h2><p>Ask PaiCLI to inspect code, use tools, delegate to subagents, and return a grounded answer.</p><div className="suggestion-row"><button onClick={() => setMessage("Review the current project and report the top three risks.")}><FileSearch size={14} />Review this project</button><button onClick={() => setMessage("Explain what the active subagents are doing right now.")}><Activity size={14} />Inspect agent activity</button></div></div> : <div className="message-stream">{turnGroups.map((group) => <TurnGroupCard group={group} selectedEvent={selectedEvent} onSelect={setSelectedEvent} key={group.id} />)}{loading && <div className="thinking-row"><LoaderCircle className="spin" size={16} />PaiCLI is starting the run...</div>}</div>}
          </div>
          <div className="composer-wrap"><div className="composer"><Textarea value={message} onChange={(event) => setMessage(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void submit() } }} placeholder="Ask PaiCLI to inspect, plan, or change something..." /><div className="composer-toolbar"><div className="composer-hint"><span className="key-hint">Enter</span> run <span className="key-hint">Shift + Enter</span> newline</div><div className="composer-actions"><select value={mode} onChange={(event) => setMode(event.target.value)}><option value="react">ReAct mode</option><option value="plan">Plan mode</option></select><Button onClick={() => void submit()} disabled={loading || !message.trim()}><Send size={15} /> Run</Button></div></div></div></div>
        </main>

        <aside className="inspector-panel"><div className="inspector-header"><div><div className="overline">LIVE INSPECTOR</div><h2>Agent activity</h2></div><PanelRight size={17} className="muted-icon" /></div><div className="root-agent"><div className="agent-avatar root"><Bot size={15} /></div><div className="agent-meta"><strong>root</strong><span>Orchestrator</span></div></div><Separator />{childAgents.length ? <div className="agent-tree">{childAgents.map((agent) => <AgentCard agent={agent} key={agent.id} />)}</div> : <div className="inspector-empty"><Bot size={22} /><p>Child agents appear here when PaiCLI delegates work.</p></div>}</aside>
      </div>
      {showSettings && <div className="modal-backdrop" onClick={() => setShowSettings(false)}><Card className="settings-modal" onClick={(event) => event.stopPropagation()}><CardHeader><CardTitle>Runtime connection</CardTitle></CardHeader><CardContent><p className="settings-copy">The key stays in this browser session and is sent only to your local Runtime API.</p><input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder="PAICLI_RUNTIME_API_KEY" /><div className="settings-actions"><Button variant="ghost" onClick={() => setShowSettings(false)}>Cancel</Button><Button onClick={saveKey}>Save key</Button></div></CardContent></Card></div>}
      </>
  )
}

function TurnGroupCard({ group, selectedEvent, onSelect }: { group: TurnGroup; selectedEvent: string | null; onSelect: (id: string) => void }) {
  const [open, setOpen] = useState(!group.completed)
  const [reasoningOpen, setReasoningOpen] = useState(false)
  const process = compactProcess(group.process)
  const hasSubagents = process.some((event) => event.type.startsWith("agent/"))
  const hasTools = process.some((event) => event.type.startsWith("tool/"))
  const toolCount = process.filter((event) => event.type === "tool/execution_started").length
  const subagentCount = new Set(process.filter((event) => event.type === "agent/child_started").map((event) => String(event.payload.child_id ?? event.agent_id ?? event.id))).size
  const summaryParts = [
    hasSubagents ? "Coordinated subagents" : hasTools ? "Called tools" : process.length ? "Analyzed the request" : "Completed",
    toolCount > 0 ? `${toolCount} tool${toolCount === 1 ? "" : "s"}` : "",
    subagentCount > 0 ? `${subagentCount} subagent${subagentCount === 1 ? "" : "s"}` : "",
  ].filter(Boolean)
  const processSummary = summaryParts.join(" · ")
  return <section className={`turn-group ${open ? "open" : "closed"}`}>
    {group.user && <EventCard compact event={group.user} selected={selectedEvent === group.user.id} onSelect={() => onSelect(group.user!.id)} />}
    <button className="turn-process-trigger" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
      <span className="turn-process-chevron">{open ? "⌄" : "›"}</span>
      <span className="turn-process-label">{group.completed ? processSummary : "Working"}</span>
    </button>
    {open && <div className="turn-process-body">
      {process.map((event) => event.type === "step/started" ? <div className="reasoning-disclosure" key={event.id}>
        <button className="process-row" aria-expanded={reasoningOpen} onClick={() => setReasoningOpen((value) => !value)}><span className="process-icon">✦</span><span>{processLabel(event)}</span><span className="process-muted">{reasoningOpen ? "Hide" : "Show"}</span></button>
        {reasoningOpen && <div className="reasoning-body">{reasoningSummary(event)}</div>}
      </div> : <EventCard compact event={event} selected={selectedEvent === event.id} onSelect={() => onSelect(event.id)} key={event.id} />)}
    </div>}
    {group.assistant && <div className="final-answer"><div className="answer-label"><Bot size={15} /> Latest answer</div><p>{String(group.assistant.payload.content ?? "")}</p></div>}
  </section>
}

function EventCard({ event, selected, onSelect, compact = false }: { event: RuntimeEvent; selected: boolean; onSelect: () => void; compact?: boolean }) {
  const [expanded, setExpanded] = useState(false)
  const isMessage = event.type === "user/message" || event.type === "assistant/message"
  const isTool = event.type.includes("tool")
  const isError = event.type.includes("failed") || Boolean(event.payload.is_error)
  return <div className={`event-row ${compact ? "compact" : ""} ${isMessage ? "message-event" : "activity-event"} ${selected ? "selected" : ""}`} onClick={onSelect}><div className={`event-marker ${isError ? "danger" : isTool ? "tool" : isMessage ? "message" : "default"}`}>{isError ? <CircleAlert size={13} /> : isTool ? <Terminal size={13} /> : isMessage ? <MessageSquarePlus size={13} /> : <Clock3 size={13} />}</div><div className="event-body"><div className="event-meta-line"><span className="event-title">{event.type.startsWith("tool/") ? processLabel(event) : eventTitle(event)}</span><span className="event-time">{timeLabel(event.created_at)}</span>{event.agent_id && <Badge variant="outline" className="agent-chip">{event.agent_id}</Badge>}</div><div className="event-summary">{eventSummary(event)}</div>{!isMessage && <button className="details-trigger" onClick={(click) => { click.stopPropagation(); setExpanded((value) => !value) }}>{expanded ? "Hide details" : "View details"}</button>}{expanded && <pre className="event-payload">{JSON.stringify(event.payload, null, 2)}</pre>}</div></div>
}

function AgentCard({ agent }: { agent: AgentNode }) {
  return <div className="agent-card"><div className="agent-card-main"><div className="agent-avatar"><Bot size={14} /></div><div className="agent-meta"><strong>{agent.id}</strong><span>{agent.description || "delegated child"}</span></div><Badge className={`agent-status ${statusTone(agent.state)}`}>{agent.state}</Badge></div><div className="agent-phase">{agent.phase || "waiting for first event"}</div>{agent.error && <div className="agent-error"><CircleAlert size={13} />{String(agent.error)}</div>}</div>
}

export default App
