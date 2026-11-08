import { useState } from 'react'
import * as Dropdown from '@radix-ui/react-dropdown-menu'
import { useMutation, useQuery } from '@tanstack/react-query'
import { ChevronDown, CircleAlert, Ellipsis, Plug, Plus, RefreshCw, Search } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { ApiError } from '@/lib/api'
import { errorMessage } from '@/lib/utils'
import { mcpApi, type McpServer } from './api'
import amapIcon from './amap.svg'
import { McpEditor, transportLabel } from './editor'

const brands = { amap: { name: '高德地图', icon: amapIcon } }

function serviceTitle(server: McpServer) {
  const brand = server.identity?.brand
  return (brand && brands[brand].name) || server.identity?.title || server.name
}

function ServiceAvatar({ server, title }: { server: McpServer; title: string }) {
  const [failed, setFailed] = useState<string[]>([])
  const { brand, icons } = server.identity ?? {}
  const fallback = brand && brands[brand].icon
  const glyph = title.match(/[\p{L}\p{N}]/u)?.[0].toUpperCase() ?? '·'
  const tone = Array.from(server.name).reduce(
    (value, char) => (value + char.codePointAt(0)!) % 4,
    0,
  )
  return (
    <span className="mcp-service-icon" data-tone={tone} aria-hidden="true">
      {(['light', 'dark'] as const).map((theme) => {
        const source = [icons?.[theme], icons?.default, fallback].find(
          (icon): icon is string => !!icon && !failed.includes(icon),
        )
        return (
          <span className={`mcp-icon-${theme}`} key={theme}>
            {source ? (
              <img
                alt=""
                src={source}
                width={24}
                height={24}
                onError={() => setFailed((previous) => [...previous, source])}
              />
            ) : (
              glyph
            )}
          </span>
        )
      })}
    </span>
  )
}

function status(server: McpServer, autoStart: boolean, testing: boolean) {
  if (server.blocked) return { label: '未受信任', tone: 'warning' }
  if (server.overridden) return { label: '已被项目覆盖', tone: 'neutral' }
  if (server.problem && !server.config.type) return { label: '配置错误', tone: 'error' }
  if (!server.config.enabled) {
    return { label: testing ? '已关闭 · 检测中…' : '已关闭', tone: 'neutral' }
  }
  if (!autoStart) return { label: 'Runtime 已关闭 MCP', tone: 'warning' }
  if (server.problem) return { label: '已启用 · 配置错误', tone: 'error' }
  if (testing) return { label: '已启用 · 检测中…', tone: 'neutral' }
  if (server.check?.error) return { label: '已启用 · 连接失败', tone: 'error' }
  if (server.check) return { label: '已启用 · 连接正常', tone: 'success' }
  return { label: '已启用 · 未检测', tone: 'neutral' }
}

function ServiceRow({
  server,
  autoStart,
  testing,
  pending,
  canWrite,
  onTest,
  onEdit,
  onToggle,
  onRemove,
}: {
  server: McpServer
  autoStart: boolean
  testing: boolean
  pending: boolean
  canWrite: boolean
  onTest: () => void
  onEdit: () => void
  onToggle: () => void
  onRemove: () => void
}) {
  const checking = testing || server.checking
  const state = status(server, autoStart, checking)
  const detail = server.blocked
    ? '信任当前项目后，Runtime 才会加载此服务。'
    : server.overridden
      ? '当前工作区使用同名的项目级配置。'
      : server.problem || server.check?.error
  const title = serviceTitle(server)
  const canTest = !server.blocked && !server.overridden && !!server.config.type
  const canToggle = canWrite && canTest
  return (
    <div className="mcp-service">
      <div className="mcp-service-row">
        <ServiceAvatar server={server} title={title} />
        <div className="mcp-service-copy">
          <div className="mcp-service-title">
            <span title={server.name === title ? undefined : server.name}>{title}</span>
            <span className="mcp-state" data-tone={state.tone} role="status">
              {state.label}
            </span>
          </div>
          <p className={detail ? 'mcp-row-detail' : ''}>
            {detail ||
              [
                transportLabel(server.config.type),
                server.identity?.source,
                server.check && `发现 ${server.check.tool_count} 个工具`,
              ]
                .filter(Boolean)
                .join(' · ')}
          </p>
        </div>
        <div className="mcp-service-actions">
          {canToggle && (
            <button
              type="button"
              className="mcp-switch"
              role="switch"
              aria-checked={server.config.enabled}
              aria-label={`启用 ${title}`}
              disabled={pending || (!autoStart && !server.config.enabled)}
              title={server.config.enabled ? '关闭服务' : '启动服务'}
              onClick={onToggle}
            >
              <span className="mcp-switch-track" aria-hidden="true" />
            </button>
          )}
          {!server.blocked && (canWrite || canTest) && (
            <Dropdown.Root>
              <Dropdown.Trigger asChild>
                <Button variant="ghost" size="icon" disabled={pending} aria-label={`管理 ${title}`}>
                  <Ellipsis size={18} />
                </Button>
              </Dropdown.Trigger>
              <Dropdown.Portal>
                <Dropdown.Content className="dropdown-content" align="end" sideOffset={4}>
                  {canTest && (
                    <Dropdown.Item className="dropdown-item" disabled={checking} onSelect={onTest}>
                      测试连接
                    </Dropdown.Item>
                  )}
                  {canWrite && server.config.type && (
                    <Dropdown.Item className="dropdown-item" onSelect={onEdit}>
                      编辑配置
                    </Dropdown.Item>
                  )}
                  {canWrite && (
                    <Dropdown.Item className="dropdown-item mcp-remove" onSelect={onRemove}>
                      移除服务
                    </Dropdown.Item>
                  )}
                </Dropdown.Content>
              </Dropdown.Portal>
            </Dropdown.Root>
          )}
        </div>
      </div>
      {server.check && !server.check.error && (
        <details className="mcp-tools">
          <summary>
            <ChevronDown size={14} /> 查看工具 · {server.check.tool_count}
          </summary>
          {server.check.tools.length ? (
            <ul>
              {server.check.tools.map((tool, index) => (
                <li key={`${index}-${tool.name}`}>
                  <code>{tool.name}</code>
                  {tool.description && <p>{tool.description}</p>}
                </li>
              ))}
            </ul>
          ) : (
            <p>此服务未提供可调用的工具。</p>
          )}
          {server.check.tool_count > server.check.tools.length && <p>仅显示前 100 项。</p>}
        </details>
      )}
    </div>
  )
}

export function McpSettings() {
  const list = useQuery({
    queryKey: ['mcp-servers'],
    queryFn: ({ signal }) => mcpApi.list(signal),
    retry: false,
    refetchInterval: (query) =>
      query.state.data?.servers.some((server) => server.checking) ? 1000 : false,
  })
  const [editor, setEditor] = useState<{ revision: string; server?: McpServer }>()
  const [remove, setRemove] = useState<McpServer>()
  const [filterOpen, setFilterOpen] = useState(false)
  const [filter, setFilter] = useState('')
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')

  const change = useMutation({
    mutationFn: ({ action, server }: { action: 'toggle' | 'remove'; server: McpServer }) =>
      mcpApi.save(action, {
        name: server.name,
        enabled: !server.config.enabled,
        revision: list.data?.revision,
      }),
    onMutate: () => {
      setNotice('')
      setError('')
    },
    onSuccess: async (_, { action, server }) => {
      setRemove(undefined)
      setNotice(
        action === 'remove'
          ? '服务已移除。'
          : `服务已${server.config.enabled ? '关闭' : '启用'}，从下一轮对话生效。`,
      )
      await list.refetch()
    },
    onError: (exception) => {
      setRemove(undefined)
      setError(errorMessage(exception))
      if (exception instanceof ApiError && exception.status === 409) void list.refetch()
    },
  })
  const check = useMutation({
    mutationFn: mcpApi.test,
    onMutate: () => {
      setNotice('')
      setError('')
    },
    onSuccess: async (result) => {
      if (!result.error) setNotice(`检测通过，发现 ${result.tool_count} 个工具。`)
      await list.refetch()
    },
    onError: (exception) => setError(errorMessage(exception)),
  })

  const revision = list.data?.revision
  const servers = list.data?.servers ?? []
  const searchTerm = filter.trim().toLowerCase()
  const matches = servers.filter((server) =>
    [server.name, serviceTitle(server), server.identity?.source].some((value) =>
      value?.toLowerCase().includes(searchTerm),
    ),
  )
  return (
    <>
      <main className="mcp-panel">
        <div className="mcp-page">
          <header className="mcp-heading">
            <div>
              <h1>MCP 服务</h1>
              <p>管理 Agent 可以使用的外部工具和服务。</p>
            </div>
            <Button
              variant="ghost"
              size="icon"
              aria-label="刷新 MCP 列表"
              title="刷新列表"
              disabled={list.isFetching}
              onClick={() => {
                void list.refetch()
              }}
            >
              <RefreshCw size={16} />
            </Button>
          </header>
          <div className="mcp-toolbar">
            <span className="mcp-count">
              连接器 <span>{servers.length}</span>
            </span>
            <div className="mcp-toolbar-actions">
              {filterOpen && (
                <input
                  type="search"
                  aria-label="搜索 MCP 服务"
                  placeholder="搜索服务…"
                  autoFocus
                  value={filter}
                  onChange={(event) => setFilter(event.target.value)}
                />
              )}
              <Button
                variant="outline"
                size="icon"
                aria-label="搜索 MCP 服务"
                aria-expanded={filterOpen}
                onClick={() => {
                  setFilterOpen(!filterOpen)
                  setFilter('')
                }}
              >
                <Search size={17} />
              </Button>
              <Button
                variant="outline"
                size="icon"
                aria-label="添加自定义 MCP"
                title="添加自定义 MCP"
                disabled={!revision}
                onClick={() => revision && setEditor({ revision })}
              >
                <Plus size={18} />
              </Button>
            </div>
          </div>
          {list.isPending && (
            <p className="mcp-feedback" role="status">
              正在读取 MCP 配置…
            </p>
          )}
          {list.isError && (
            <p className="mcp-feedback mcp-error" role="alert">
              <CircleAlert size={16} /> {errorMessage(list.error)}
            </p>
          )}
          {list.data?.warnings.map((warning) => (
            <p key={warning.scope} className="mcp-feedback mcp-error" role="alert">
              <CircleAlert size={16} /> {warning.scope === 'user' ? '用户级' : '项目级'} ·{' '}
              {warning.message}
            </p>
          ))}
          {error && (
            <p className="mcp-feedback mcp-error" role="alert">
              {error}
            </p>
          )}
          {notice && (
            <p className="mcp-feedback" role="status">
              {notice}
            </p>
          )}
          {list.isSuccess && !matches.length && (
            <div className="mcp-empty">
              <span className="mcp-service-icon">
                <Plug size={19} aria-hidden="true" />
              </span>
              <h2>{filter ? '没有匹配的服务' : '还没有 MCP 服务'}</h2>
              <p>
                {filter
                  ? '试试其他服务名称。'
                  : '添加本地命令或远程服务，也可以粘贴 JSON 批量导入。'}
              </p>
              {!filter && (
                <Button
                  className="mcp-primary"
                  disabled={!revision}
                  onClick={() => revision && setEditor({ revision })}
                >
                  <Plus size={15} /> 添加 MCP
                </Button>
              )}
            </div>
          )}
          {(['user', 'project'] as const).map((scope) => {
            const group = matches.filter((server) => server.scope === scope)
            if (!group.length) return null
            return (
              <section key={scope} className="mcp-group" aria-labelledby={`mcp-${scope}`}>
                <h2 id={`mcp-${scope}`}>
                  {scope === 'user' ? '自定义 MCP' : '由当前项目提供'} <span>{group.length}</span>
                </h2>
                <div className="mcp-service-card">
                  {group.map((server) => (
                    <ServiceRow
                      key={server.name}
                      server={server}
                      autoStart={!!list.data?.auto_start}
                      canWrite={scope === 'user' && !!revision}
                      pending={change.isPending || check.isPending}
                      testing={
                        check.isPending &&
                        check.variables?.name === server.name &&
                        check.variables.scope === scope
                      }
                      onEdit={() => revision && setEditor({ revision, server })}
                      onTest={() => check.mutate(server)}
                      onToggle={() => change.mutate({ action: 'toggle', server })}
                      onRemove={() => setRemove(server)}
                    />
                  ))}
                </div>
              </section>
            )
          })}
        </div>
      </main>
      {editor && (
        <McpEditor
          {...editor}
          onClose={() => setEditor(undefined)}
          onSaved={(message) => {
            setEditor(undefined)
            setError('')
            setNotice(message)
            void list.refetch()
          }}
        />
      )}
      {remove && (
        <Dialog
          open
          onOpenChange={(open) => {
            if (!open && !change.isPending) setRemove(undefined)
          }}
          title="移除 MCP 服务"
          description={`从用户级配置移除 ${remove.name}。`}
        >
          <div className="mcp-editor-actions">
            <Button
              variant="outline"
              disabled={change.isPending}
              onClick={() => setRemove(undefined)}
            >
              取消
            </Button>
            <Button
              disabled={change.isPending}
              onClick={() => change.mutate({ action: 'remove', server: remove })}
            >
              移除服务
            </Button>
          </div>
        </Dialog>
      )}
    </>
  )
}
