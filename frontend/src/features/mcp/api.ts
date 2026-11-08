import { request } from '@/lib/api'

export type Transport = 'stdio' | 'streamable-http' | 'sse'
export type Scope = 'user' | 'project'

export interface McpConfig {
  type: Transport | ''
  enabled: boolean
  command?: string
  args?: string[]
  cwd?: string
  url?: string
  headers?: Record<string, string | null>
  env?: Record<string, string | null>
  timeout?: number
}

export interface McpCheck {
  checked_at: string
  error: string | null
  tools: { name: string; description: string }[]
  tool_count: number
}

export interface McpServer {
  name: string
  scope: Scope
  config: McpConfig
  blocked: boolean
  overridden: boolean
  problem: string | null
  check: McpCheck | null
  identity?: {
    source: string | null
    brand: 'amap' | null
    title?: string | null
    icons?: Partial<Record<'default' | 'light' | 'dark', string>>
  } | null
}

export interface McpInventory {
  servers: McpServer[]
  revision: string | null
  workspace: string
  auto_start: boolean
  warnings: { scope: Scope; message: string }[]
}

export interface ImportPreview {
  revision: string
  servers: { name: string; config: McpConfig; conflict: boolean }[]
}

const root = '/v1/mcp/servers'

function post<T>(action: string, body: unknown) {
  return request<T>(`${root}/${action}`, { method: 'POST', body: JSON.stringify(body) })
}

export const mcpApi = {
  list: (signal: AbortSignal) => request<McpInventory>(root, { signal }),
  preview: (document: unknown) => post<ImportPreview>('preview', { document }),
  test: (server: Pick<McpServer, 'name' | 'scope'>) =>
    post<McpCheck>('test', { name: server.name, scope: server.scope }),
  save: (action: 'add' | 'edit' | 'toggle' | 'remove' | 'import', body: object) =>
    post<{ saved: true }>(action, body),
}
