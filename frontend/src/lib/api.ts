import type { RuntimeClient, EventPage, Session, Task } from './types'
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
  }
}
export class CapabilityError extends Error {
  constructor(capability: string) {
    super(`${capability}尚未接入 Runtime API。`)
  }
}
export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const timeout = AbortSignal.timeout(15_000)
  const response = await fetch(path, {
    ...init,
    headers: { ...(init?.body ? { 'Content-Type': 'application/json' } : {}), ...init?.headers },
    signal: init?.signal ? AbortSignal.any([init.signal, timeout]) : timeout,
  })
  if (!response.ok) {
    // Do not echo arbitrary provider diagnostics or credentials from an error body.
    const message =
      response.status === 401
        ? 'Runtime 鉴权失败，请检查服务端环境配置。'
        : response.status === 503
          ? '本地桥接未配置，请查看连接设置。'
          : response.status === 502
            ? '无法连接 Runtime，请确认本地服务已启动。'
            : `Runtime 请求失败（${response.status}）。`
    throw new ApiError(response.status, message)
  }
  return response.json() as Promise<T>
}
const post = <T>(path: string, body: unknown = {}) =>
  request<T>(path, { method: 'POST', body: JSON.stringify(body) })
const sid = (id: string) => `/v1/sessions/${encodeURIComponent(id)}`
export const liveClient: RuntimeClient = {
  capabilities: { cancelTurn: true, approvals: true, changes: false, modelSelection: false },
  health: (signal) => request('/v1/health', { signal }),
  sessions: async (signal) =>
    (await request<{ sessions: Session[] }>('/v1/sessions', { signal })).sessions,
  createSession: async () => {
    const { id } = await post<{ id: string }>('/v1/sessions')
    return {
      id,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      status: 'idle',
      last_sequence: 0,
    }
  },
  events: (id, cursor, signal) =>
    request<EventPage>(`${sid(id)}/events.json?cursor=${cursor}`, { signal }),
  subscribe: (id, cursor, signal, receive, parentId) =>
    new Promise<void>((resolve, reject) => {
      if (signal.aborted) {
        resolve()
        return
      }
      const path = parentId ? `${sid(parentId)}/subagents/${encodeURIComponent(id)}` : sid(id)
      const source = new EventSource(`${path}/events/stream?cursor=${cursor}&view=conversation`)
      const close = () => {
        source.close()
        clearTimeout(connectTimeout)
        signal.removeEventListener('abort', abort)
      }
      const abort = () => {
        close()
        resolve()
      }
      const fail = (message: string) => {
        close()
        reject(new Error(message))
      }
      const connectTimeout = setTimeout(() => fail('实时连接超时。'), 15_000)
      signal.addEventListener('abort', abort, { once: true })
      source.onerror = () => fail('实时连接已断开。')
      source.addEventListener('page', (event: MessageEvent<string>) => {
        clearTimeout(connectTimeout)
        try {
          receive(JSON.parse(event.data) as EventPage)
        } catch {
          fail('实时事件格式无效。')
        }
      })
    }),
  send: (id, message, mode) =>
    post(`${sid(id)}/turns/async`, { message, mode, interactive_approval: true }),
  setPermissionMode: (id, mode) => post(`${sid(id)}/permissions`, { mode }),
  cancelTurn: async (id, runId) => {
    try {
      await post(`${sid(id)}/runs/${encodeURIComponent(runId)}/cancel`)
    } catch (error) {
      if (error instanceof ApiError && (error.status === 404 || error.status === 409))
        throw new ApiError(error.status, '本轮执行已结束或已失效。')
      throw error
    }
  },
  decideApproval: async (id, approvalId, decision) => {
    try {
      await post(`${sid(id)}/approvals/${encodeURIComponent(approvalId)}/decision`, { decision })
    } catch (error) {
      if (error instanceof ApiError && (error.status === 404 || error.status === 409))
        throw new ApiError(error.status, '审批已过期或已处理。')
      throw error
    }
  },
  changes: async () => {
    throw new CapabilityError('文件变更')
  },
  tasks: async (signal) => (await request<{ tasks: Task[] }>('/v1/tasks', { signal })).tasks,
  addTask: async (prompt, mode) => {
    await post('/v1/tasks', { prompt, mode })
  },
  cancelTask: async (id) => {
    const result = await post<{ canceled: boolean }>(`/v1/tasks/${encodeURIComponent(id)}/cancel`)
    if (!result.canceled) throw new Error('任务未取消，可能已经结束，请刷新列表。')
  },
}
