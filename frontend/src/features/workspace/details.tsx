import { useState, type ReactNode } from 'react'
import * as m from 'motion/react-m'
import {
  Bot,
  ChevronDown,
  ChevronRight,
  FileCode2,
  GitCompareArrows,
  ListChecks,
  Terminal,
  X,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useSurfaceMotion } from '@/lib/motion'
import type { ChangeFile } from '@/lib/types'
import type { ToolView } from '@/lib/events'

export type DetailsTab = 'changes' | 'plan' | 'subagents'

const tabs = [
  { id: 'changes', label: '变更', Icon: GitCompareArrows },
  { id: 'plan', label: '计划', Icon: ListChecks },
  { id: 'subagents', label: '子 agent', Icon: Bot },
] as const

export function Details({
  changes,
  fileTools,
  tool,
  onClose,
  onClearTool,
  onInspect,
  tab,
  onTab,
  subagents,
  subagentCount,
}: {
  changes: ChangeFile[]
  fileTools: ToolView[]
  tool: ToolView | null
  onClose: () => void
  onClearTool: () => void
  onInspect: (tool: ToolView) => void
  tab: DetailsTab
  onTab: (tab: DetailsTab) => void
  subagents: ReactNode
  subagentCount: number
}) {
  const [file, setFile] = useState('')
  const contentMotion = useSurfaceMotion({ y: 4 }, 0.16)
  const selected = changes.find((c) => c.path === file) ?? changes[0]
  const fileActivity = new Map<string, ToolView>()
  for (const entry of fileTools) {
    if (entry.status !== 'completed') continue
    const prefix =
      entry.name === 'write_file' ? 'Wrote ' : entry.name === 'edit_file' ? 'Edited ' : ''
    // These built-in results confirm a write; dry runs and no-ops use different summaries.
    if (!prefix || !entry.content.startsWith(prefix)) continue
    const path = entry.content.split('\n', 1)[0].slice(prefix.length)
    if (path) fileActivity.set(path, entry)
  }
  return (
    <aside className="details-panel" aria-label="任务详情">
      <div className="details-header">
        <span>{tool ? '执行详情' : '工作详情'}</span>
        <Button variant="ghost" size="icon" aria-label="关闭详情" onClick={onClose}>
          <X size={16} />
        </Button>
      </div>
      {tool ? (
        <m.div className="tool-detail" key={tool.id} {...contentMotion}>
          <Button variant="ghost" size="sm" onClick={onClearTool}>
            ← 返回工作详情
          </Button>
          <h3>
            <Terminal size={16} />
            {tool.name}
          </h3>
          <span className="pill">
            {tool.status === 'completed' ? '已完成' : tool.status === 'failed' ? '失败' : '运行中'}
          </span>
          <pre>{tool.content || '等待工具返回结果…'}</pre>
        </m.div>
      ) : (
        <>
          <div className="detail-tabs" role="tablist" aria-label="工作详情分类">
            {tabs.map(({ id, label, Icon }, index) => {
              const count =
                id === 'changes'
                  ? changes.length || fileActivity.size
                  : id === 'subagents'
                    ? subagentCount
                    : 0
              return (
                <button
                  key={id}
                  role="tab"
                  id={`${id}-tab`}
                  aria-controls="detail-content"
                  aria-selected={tab === id}
                  tabIndex={tab === id ? 0 : -1}
                  onKeyDown={(event) => {
                    const delta =
                      event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0
                    if (!delta) return
                    event.preventDefault()
                    const next = tabs[(index + delta + tabs.length) % tabs.length].id
                    onTab(next)
                    document.getElementById(`${next}-tab`)?.focus()
                  }}
                  onClick={() => onTab(id)}
                >
                  <Icon size={14} aria-hidden="true" />
                  {label}
                  {count > 0 && <span>{count}</span>}
                </button>
              )
            })}
          </div>
          <m.div
            key={tab}
            {...contentMotion}
            className={tab === 'subagents' ? 'subagents-content' : 'detail-scroll'}
            id="detail-content"
            role="tabpanel"
            aria-labelledby={`${tab}-tab`}
          >
            {tab === 'subagents' ? (
              subagents
            ) : tab === 'changes' ? (
              changes.length > 0 ? (
                <>
                  <div className="change-summary">
                    <span>{changes.length} 个文件变更</span>
                    <span>
                      <b>+{changes.reduce((n, c) => n + c.added, 0)}</b>{' '}
                      <em>−{changes.reduce((n, c) => n + c.removed, 0)}</em>
                    </span>
                  </div>
                  <div className="file-list">
                    {changes.map((change) => (
                      <button
                        key={change.path}
                        className={selected?.path === change.path ? 'active' : ''}
                        onClick={() => setFile(change.path)}
                      >
                        <FileCode2 size={14} />
                        <span title={change.path}>{change.path.split('/').at(-1)}</span>
                        <b>+{change.added}</b>
                        {change.removed > 0 && <em>−{change.removed}</em>}
                        <ChevronRight size={12} />
                      </button>
                    ))}
                  </div>
                  {selected && (
                    <div className="diff-card">
                      <div className="diff-heading">
                        <ChevronDown size={13} />
                        <span title={selected.path}>{selected.path}</span>
                      </div>
                      <div className="diff-code">
                        {selected.lines.map((line, index) => (
                          <div className={`diff-line ${line.kind}`} key={index}>
                            <span className="line-number">{line.number ?? ''}</span>
                            <span className="line-sign">
                              {line.kind === 'add' ? '+' : line.kind === 'remove' ? '−' : ' '}
                            </span>
                            <code>{line.text || ' '}</code>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </>
              ) : fileActivity.size ? (
                <>
                  <p className="change-summary">此会话的文件工具记录</p>
                  <div className="file-list">
                    {[...fileActivity].map(([path, entry]) => (
                      <button key={path} onClick={() => onInspect(entry)}>
                        <FileCode2 size={14} />
                        <span title={path}>{path}</span>
                        <ChevronRight size={12} />
                      </button>
                    ))}
                  </div>
                </>
              ) : (
                <div className="panel-empty">
                  <GitCompareArrows size={28} />
                  <h3>暂无文件工具记录</h3>
                </div>
              )
            ) : (
              <div className="plan-content">
                <p className="muted">在对话中查看 Agent 的计划。</p>
              </div>
            )}
          </m.div>
        </>
      )}
    </aside>
  )
}
