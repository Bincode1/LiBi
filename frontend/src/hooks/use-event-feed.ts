import { useEffect, useMemo, useState } from 'react'
import { skipToken, useQuery, useQueryClient } from '@tanstack/react-query'
import { mergeEvents } from '@/lib/events'
import { errorMessage } from '@/lib/utils'
import type {
  ActiveRun,
  AssistantPreview,
  PermissionMode,
  RuntimeClient,
  RuntimeEvent,
  Session,
  Subagent,
} from '@/lib/types'

const emptyEvents: RuntimeEvent[] = []

interface FeedCheckpoint {
  cursor: number
  cwd?: string
  events: RuntimeEvent[]
  preview?: AssistantPreview | null
  activeRun?: ActiveRun | null
  connectedAt: number
  lastSeenRunId?: string
  permissionMode?: PermissionMode
  status: string
  title?: string | null
  subagents?: Subagent[]
  subagent?: Subagent | null
}

export function useEventFeed(
  client: RuntimeClient,
  sessionId: string | undefined,
  retry: number,
  parentId?: string,
) {
  const queryClient = useQueryClient()
  const key = useMemo(
    () => ['session-feed', sessionId, parentId] as const,
    [sessionId, parentId],
  )
  const { data: checkpoint } = useQuery<FeedCheckpoint>({ queryKey: key, queryFn: skipToken })
  const [connection, setConnection] = useState<{
    key: typeof key
    retry: number
    error: string
  } | null>(null)
  useEffect(() => {
    if (!sessionId) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    let failures = 0
    const connect = async () => {
      let received = false
      try {
        const connectedAt = performance.now()
        const cursor = queryClient.getQueryData<FeedCheckpoint>(key)?.cursor ?? 0
        await client.subscribe(
          sessionId,
          cursor,
          controller.signal,
          (page) => {
            if (controller.signal.aborted || page.session_id !== sessionId) return
            queryClient.setQueryData<FeedCheckpoint>(key, (previous) => {
              const events = previous?.events ?? emptyEvents
              return {
                cursor: page.next_cursor,
                cwd: page.cwd,
                events: page.events.length ? mergeEvents(events, page.events) : events,
                preview: page.assistant_preview,
                activeRun: page.active_run,
                connectedAt,
                lastSeenRunId: page.active_run?.run_id ?? previous?.lastSeenRunId,
                permissionMode: page.permission_mode,
                status: page.state.status,
                title: page.state.title,
                subagents: page.subagents,
                subagent: page.subagent,
              }
            })
            const title = page.state.title
            if (title && !parentId) {
              queryClient.setQueryData<Session[]>(['sessions'], (sessions) => {
                const current = sessions?.find((item) => item.id === sessionId)
                if (!current || current.title === title || current.last_sequence > page.next_cursor)
                  return sessions
                return sessions?.map((item) =>
                  item === current ? { ...item, title, last_sequence: page.next_cursor } : item,
                )
              })
            }
            if (!received) setConnection({ key, retry, error: '' })
            received = true
            failures = 0
          },
          parentId,
        )
      } catch (error) {
        if (controller.signal.aborted) return
        failures++
        setConnection({
          key,
          retry,
          error: `${errorMessage(error)}${failures <= 3 ? ' 正在重新连接…' : ' 请点击重试。'}`,
        })
        if (failures <= 3) timer = setTimeout(connect, 700 * 2 ** (failures - 1))
      }
    }
    void connect()
    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [client, key, queryClient, sessionId, retry, parentId])
  const current = connection?.key === key && connection.retry === retry ? connection : null
  const syncing = Boolean(sessionId && !current?.error && (!current || !checkpoint))
  return {
    cursor: checkpoint?.cursor ?? 0,
    cwd: checkpoint?.cwd,
    events: checkpoint?.events ?? emptyEvents,
    subagents: checkpoint?.subagents,
    subagent: checkpoint?.subagent,
    preview: checkpoint?.preview,
    activeRun: checkpoint?.activeRun,
    connectedAt: checkpoint?.connectedAt,
    lastSeenRunId: checkpoint?.lastSeenRunId,
    permissionMode: checkpoint?.permissionMode,
    setPermissionMode: (permissionMode: PermissionMode) =>
      queryClient.setQueryData<FeedCheckpoint>(key, (previous) =>
        previous ? { ...previous, permissionMode } : previous,
      ),
    status: checkpoint?.status ?? 'idle',
    title: checkpoint?.title,
    error: current?.error ?? '',
    loading: syncing && !checkpoint,
    syncing,
  }
}
