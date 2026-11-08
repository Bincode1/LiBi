export type Mode = 'react' | 'plan'
export type PermissionMode = 'default' | 'auto'
export interface Session {
  id: string
  cwd?: string
  created_at: string
  updated_at: string
  status: string
  last_sequence: number
  title?: string | null
}
export interface WorkspaceDirectory {
  path: string
  parent: string | null
  home: string
  roots: string[]
  directories: { name: string; path: string }[]
}
export interface RuntimeEvent {
  id: string
  sequence: number
  type: string
  payload: Record<string, unknown>
  created_at: string
  turn_id?: string | null
  step_id?: string | null
  tool_call_id?: string | null
  approval_id?: string | null
  source?: string
}
export interface AssistantPreview {
  turn_id: string
  step_id: string
  text: string
}
export interface ActiveRun {
  run_id: string
  status: 'running' | 'canceling'
}
export interface Subagent {
  id: string
  session_id: string
  label: string
  role?: string | null
  parent_turn_id: string | null
  parent_step_id: string | null
  tool_call_id: string | null
  sequence: number
  status: string
  close_reason?: 'idle_expired'
  report?: string
  result_count: number
  started_at: string
  updated_at: string
  closed: boolean
  tool_name: string | null
}
export interface EventPage {
  session_id: string
  cwd?: string
  events: RuntimeEvent[]
  next_cursor: number
  state: { status: string; active_turn_id?: string | null; title?: string | null }
  active_run?: ActiveRun | null
  permission_mode?: PermissionMode
  assistant_preview?: AssistantPreview | null
  subagents?: Subagent[]
  subagent?: Subagent | null
}
export interface Task {
  id: string
  prompt: string
  status: string
  mode: Mode
  result?: string | null
  error?: string | null
  created_at: string
  updated_at: string
}
export interface Capabilities {
  cancelTurn: boolean
  approvals: boolean
  changes: boolean
  modelSelection: boolean
}
export interface ChangeFile {
  path: string
  added: number
  removed: number
  lines: { kind: 'context' | 'add' | 'remove'; text: string; number?: number }[]
}
export interface Attachment {
  id: string
  name: string
  media_type: string
  size: number
  session_id: string
}
export interface RuntimeClient {
  capabilities: Capabilities
  health(signal?: AbortSignal): Promise<{ status: string; cwd: string }>
  sessions(signal?: AbortSignal): Promise<Session[]>
  createSession(cwd?: string): Promise<Session>
  browseWorkspace(path: string, signal?: AbortSignal): Promise<WorkspaceDirectory>
  renameSession(id: string, title: string): Promise<{ title: string }>
  events(id: string, cursor: number, signal?: AbortSignal): Promise<EventPage>
  subscribe(
    id: string,
    cursor: number,
    signal: AbortSignal,
    receive: (page: EventPage) => void,
    parentId?: string,
  ): Promise<void>
  send(
    id: string,
    message: string,
    mode: Mode,
    attachmentIds?: string[],
  ): Promise<{ run_id: string; turn_id?: string; after_sequence?: number }>
  uploadAttachment(id: string, file: File): Promise<Attachment>
  cancelTurn(id: string, runId: string): Promise<void>
  setPermissionMode(id: string, mode: PermissionMode): Promise<{ mode: PermissionMode }>
  decideApproval(id: string, approvalId: string, decision: 'approve' | 'deny'): Promise<void>
  changes(id: string): Promise<ChangeFile[]>
  tasks(signal?: AbortSignal): Promise<Task[]>
  addTask(prompt: string, mode: Mode): Promise<void>
  cancelTask(id: string): Promise<void>
}
