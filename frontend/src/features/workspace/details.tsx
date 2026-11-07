import { useState, type ReactNode } from 'react'
import {
  Check,
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
import type { ChangeFile, Source } from '@/lib/types'
import type { ToolView } from '@/lib/events'

export type DetailsTab = 'changes' | 'plan' | 'subagents'

const tabs = [
  { id: 'changes', label: '变更', Icon: GitCompareArrows },
  { id: 'plan', label: '计划', Icon: ListChecks },
  { id: 'subagents', label: '子 agent', Icon: Bot },
] as const

export function Details({
  source,
  changes,
  tool,
  onClose,
  onClearTool,
  tab,
  onTab,
  subagents,
  subagentCount,
}: {
  source: Source
  changes: ChangeFile[]
  tool: ToolView | null
  onClose: () => void
  onClearTool: () => void
  tab: DetailsTab
  onTab: (tab: DetailsTab) => void
  subagents: ReactNode
  subagentCount: number
}) {
  const [file, setFile] = useState('')
  const selected = changes.find((c) => c.path === file) ?? changes[0]
  return (
    <aside className="details-panel" aria-label="任务详情">
      <div className="details-header">
        <span>{tool ? '执行详情' : '工作详情'}</span>
        <Button variant="ghost" size="icon" aria-label="关闭详情" onClick={onClose}>
          <X size={16} />
        </Button>
      </div>
      {tool ? (
        <div className="tool-detail">
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
        </div>
      ) : (
        <>
          <div className="detail-tabs" role="tablist" aria-label="工作详情分类">
            {tabs.map(({ id, label, Icon }, index) => {
              const count =
                id === 'changes' ? changes.length : id === 'subagents' ? subagentCount : 0
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
          <div
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
                        <span>示例</span>
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
                  <div className="detail-note">
                    <span className="note-dot" />
                    演示变更 · 未修改实际项目文件
                  </div>
                </>
              ) : (
                <div className="panel-empty">
                  <GitCompareArrows size={28} />
                  <h3>{source === 'live' ? '文件变更接口待接入' : '这里会显示文件变更'}</h3>
                  <p>
                    {source === 'live'
                      ? '对话与工具结果可以正常查看。接入变更 API 后，将在这里展示真实差异。'
                      : '打开「让会话列表更有条理」查看示例差异。'}
                  </p>
                </div>
              )
            ) : (
              <div className="plan-content">
                {source === 'demo' && changes.length ? (
                  <>
                    <h3>优化会话导航</h3>
                    {[
                      '检查会话列表与数据结构',
                      '按时间分组并优化选中状态',
                      '补充边界测试并验证',
                    ].map((step, i) => (
                      <div className="plan-step" key={step}>
                        <span>
                          <Check size={12} />
                        </span>
                        <div>
                          <b>0{i + 1}</b>
                          <p>{step}</p>
                        </div>
                      </div>
                    ))}
                  </>
                ) : (
                  <p className="muted">
                    {source === 'live'
                      ? '结构化计划接口待接入，当前可在对话中阅读 Agent 输出的计划。'
                      : '当前会话没有示例计划。'}
                  </p>
                )}
              </div>
            )}
          </div>
        </>
      )}
    </aside>
  )
}
