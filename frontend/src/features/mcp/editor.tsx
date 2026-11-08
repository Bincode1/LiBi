import { useState, type FormEvent } from 'react'
import { Plus, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { ChoiceMenu } from '@/components/ui/dropdown-menu'
import { errorMessage } from '@/lib/utils'
import { mcpApi, type ImportPreview, type McpConfig, type McpServer } from './api'

const transports = [
  { value: 'stdio', label: 'STDIO' },
  { value: 'streamable-http', label: 'HTTP' },
  { value: 'sse', label: 'SSE' },
] as const

export function transportLabel(value: McpConfig['type']) {
  return transports.find((item) => item.value === value)?.label ?? '未配置'
}

const jsonExample = JSON.stringify(
  {
    mcpServers: { example: { type: 'streamable-http', url: 'https://example.com/mcp' } },
  },
  null,
  2,
)

type Pair = { id: number; key: string; value: string | null }

function pairs(values: Record<string, string | null> = {}): Pair[] {
  return Object.entries(values).map(([key, value], id) => ({ id, key, value }))
}

function PairFields({
  label,
  singular,
  rows,
  onChange,
}: {
  label: string
  singular: string
  rows: Pair[]
  onChange: (rows: Pair[]) => void
}) {
  return (
    <fieldset className="mcp-pairs">
      <legend className="sr-only">{label}</legend>
      <div className="mcp-pair-heading">
        <span>{label}</span>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={() =>
            onChange([...rows, { id: (rows.at(-1)?.id ?? -1) + 1, key: '', value: '' }])
          }
        >
          <Plus size={14} /> 添加 {singular}
        </Button>
      </div>
      {rows.map((row, index) => (
        <div className="mcp-pair" key={row.id}>
          <input
            aria-label={`${singular} ${index + 1} 名称`}
            placeholder={singular === 'Header' ? 'Authorization' : 'API_KEY'}
            value={row.key}
            onChange={(event) =>
              onChange(
                rows.map((item) =>
                  item.id === row.id ? { ...item, key: event.target.value } : item,
                ),
              )
            }
          />
          <input
            type="password"
            autoComplete="off"
            aria-label={`${singular} ${index + 1} 值`}
            placeholder={row.value === null ? '已保存，留空保留' : '值或 ${ENV_VAR}'}
            value={row.value ?? ''}
            onChange={(event) =>
              onChange(
                rows.map((item) =>
                  item.id === row.id ? { ...item, value: event.target.value } : item,
                ),
              )
            }
          />
          <Button
            type="button"
            size="icon"
            variant="ghost"
            aria-label={`移除 ${singular} ${index + 1}`}
            onClick={() => onChange(rows.filter((item) => item.id !== row.id))}
          >
            <X size={15} />
          </Button>
        </div>
      ))}
    </fieldset>
  )
}

function pairValues(rows: Pair[]) {
  const entries = rows.map(({ key, value }) => [key.trim(), value] as const)
  const keys = new Set(entries.map(([key]) => key))
  if (keys.has('') || keys.size !== entries.length)
    throw new Error('请填写名称，且每个名称只出现一次。')
  return Object.fromEntries(entries)
}

export function McpEditor({
  server,
  revision,
  onClose,
  onSaved,
}: {
  server?: McpServer
  revision: string
  onClose: () => void
  onSaved: (message: string) => void
}) {
  const initial = server?.config
  const [tab, setTab] = useState<'form' | 'json'>('form')
  const [name, setName] = useState(server?.name ?? '')
  const [type, setType] = useState<McpConfig['type']>(initial?.type ?? '')
  const [url, setUrl] = useState(initial?.url ?? '')
  const [command, setCommand] = useState(initial?.command ?? '')
  const [args, setArgs] = useState(initial?.args?.join('\n') ?? '')
  const [cwd, setCwd] = useState(initial?.cwd ?? '')
  const [headers, setHeaders] = useState(() => pairs(initial?.headers))
  const [env, setEnv] = useState(() => pairs(initial?.env))
  const [source, setSource] = useState('')
  const [preview, setPreview] = useState<{ report: ImportPreview; document: unknown }>()
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (busy) return
    setError('')
    setBusy(true)
    try {
      if (tab === 'json') {
        if (preview) {
          await mcpApi.save('import', {
            document: preview.document,
            revision: preview.report.revision,
          })
          onSaved(`已导入 ${preview.report.servers.length} 个服务，可在列表中启用。`)
        } else {
          let document: unknown
          try {
            document = JSON.parse(source)
          } catch {
            throw new Error('JSON 格式有误，请检查引号、逗号和括号。')
          }
          setPreview({ report: await mcpApi.preview(document), document })
        }
        return
      }
      if (!type) throw new Error('请选择传输类型。')
      const argsChanged = args !== (initial?.args?.join('\n') ?? '')
      const config =
        type === 'stdio'
          ? {
              type,
              command: command.trim(),
              ...(argsChanged && { args: args ? args.split('\n') : [] }),
              cwd: cwd.trim(),
              env: pairValues(env),
            }
          : { type, url: url.trim(), headers: pairValues(headers) }
      await mcpApi.save(server ? 'edit' : 'add', { name: name.trim(), config, revision })
      onSaved(server ? '配置已保存，从下一轮对话生效。' : '服务已添加，可在列表中测试并启用。')
    } catch (exception) {
      setError(errorMessage(exception))
    } finally {
      setBusy(false)
    }
  }

  const hasConflict = preview?.report.servers.some((item) => item.conflict)
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !busy) onClose()
      }}
      className="mcp-editor"
      title={server ? '编辑 MCP 服务' : '添加自定义 MCP'}
      description="用户级服务保存在 ~/.libi/mcp.json，可在所有工作区使用。"
    >
      <form className="mcp-editor-form" onSubmit={submit}>
        {!server && (
          <div className="mcp-segments" role="group" aria-label="配置方式">
            {(['form', 'json'] as const).map((mode) => (
              <button
                key={mode}
                type="button"
                aria-pressed={tab === mode}
                disabled={busy}
                onClick={() => {
                  setTab(mode)
                  setPreview(undefined)
                  setError('')
                }}
              >
                {mode === 'form' ? '表单' : 'JSON'}
              </button>
            ))}
          </div>
        )}
        {tab === 'form' ? (
          <div className="mcp-fields">
            <label className="mcp-field">
              <span>MCP 名称</span>
              <input
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="例如 github-search"
                disabled={Boolean(server)}
                required
                maxLength={64}
              />
              <small>Agent 使用此名称识别服务。</small>
            </label>
            <div className="mcp-field">
              <span>传输类型</span>
              <ChoiceMenu
                label="选择传输类型"
                className="mcp-transport-select"
                menuClassName="mcp-transport-options"
                value={type}
                onValueChange={setType}
                options={transports}
                disabled={busy}
              />
            </div>
            {type === 'stdio' && (
              <>
                <label className="mcp-field">
                  <span>启动命令</span>
                  <input
                    placeholder="例如 npx 或 uvx"
                    value={command}
                    onChange={(event) => setCommand(event.target.value)}
                    required
                  />
                </label>
                <label className="mcp-field">
                  <span>启动参数</span>
                  <textarea
                    rows={3}
                    placeholder={'-y\n@modelcontextprotocol/server-filesystem'}
                    value={args}
                    onChange={(event) => setArgs(event.target.value)}
                  />
                  <small>每行填写一个参数。路径含空格时可直接填写。</small>
                </label>
                <label className="mcp-field">
                  <span>
                    工作目录 <small>可选</small>
                  </span>
                  <input
                    value={cwd}
                    onChange={(event) => setCwd(event.target.value)}
                    placeholder="默认使用当前工作区"
                  />
                </label>
                <PairFields label="环境变量" singular="变量" rows={env} onChange={setEnv} />
              </>
            )}
            {type && type !== 'stdio' && (
              <>
                <label className="mcp-field">
                  <span>服务地址</span>
                  <input
                    type="url"
                    placeholder="https://example.com/mcp"
                    value={url}
                    onChange={(event) => setUrl(event.target.value)}
                    required
                  />
                  <small>支持 HTTP 或 HTTPS。访问令牌请通过请求头传递。</small>
                </label>
                <PairFields label="请求头" singular="Header" rows={headers} onChange={setHeaders} />
              </>
            )}
          </div>
        ) : preview ? (
          <div className="mcp-preview">
            <h3>预览导入 · {preview.report.servers.length} 个服务</h3>
            <ul>
              {preview.report.servers.map((item) => (
                <li key={item.name}>
                  <strong>{item.name}</strong>
                  <span>{transportLabel(item.config.type)}</span>
                  <span>{item.conflict ? '名称已存在' : '导入后停用'}</span>
                </li>
              ))}
            </ul>
            {hasConflict && <p role="alert">请返回 JSON 修改同名服务，再预览导入。</p>}
          </div>
        ) : (
          <textarea
            className="mcp-json"
            aria-label="MCP JSON 配置"
            placeholder={jsonExample}
            value={source}
            onChange={(event) => setSource(event.target.value)}
            spellCheck={false}
            required
          />
        )}
        {error && (
          <p className="mcp-error" role="alert">
            {error}
          </p>
        )}
        <div className="mcp-editor-actions">
          <Button
            type="button"
            variant="outline"
            disabled={busy}
            onClick={() => {
              if (preview) {
                setPreview(undefined)
                setError('')
              } else onClose()
            }}
          >
            {preview ? '返回编辑' : '取消'}
          </Button>
          <Button type="submit" className="mcp-primary" disabled={busy || hasConflict}>
            {busy
              ? '处理中…'
              : preview
                ? '确认导入'
                : tab === 'json'
                  ? '预览导入'
                  : server
                    ? '保存配置'
                    : '添加 MCP'}
          </Button>
        </div>
      </form>
    </Dialog>
  )
}
