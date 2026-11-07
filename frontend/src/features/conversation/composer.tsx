import { useEffect, useRef, useState } from 'react'
import {
  ArrowUp,
  ChevronDown,
  CornerDownLeft,
  GitBranch,
  ListChecks,
  LoaderCircle,
  ShieldCheck,
  Square,
  Zap,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Dropdown } from '@/components/ui/dropdown-menu'
import type { Mode, PermissionMode, Source } from '@/lib/types'
export function Composer({
  draft,
  setDraft,
  mode,
  setMode,
  permissionMode,
  setPermissionMode,
  changingPermission,
  source,
  busy,
  unmanagedRun,
  submitting,
  syncing,
  canStop,
  stopping,
  onSend,
  onStop,
  onNewConversation,
}: {
  draft: string
  setDraft: (value: string) => void
  mode: Mode
  setMode: (mode: Mode) => void
  permissionMode: PermissionMode
  setPermissionMode: (mode: PermissionMode) => void
  changingPermission: boolean
  source: Source
  busy: boolean
  unmanagedRun: boolean
  submitting: boolean
  syncing: boolean
  canStop: boolean
  stopping: boolean
  onSend: () => void
  onStop: () => void
  onNewConversation: () => void
}) {
  const ref = useRef<HTMLTextAreaElement>(null)
  const [composing, setComposing] = useState(false)
  const sendBlocked = submitting || syncing || changingPermission
  const permissionTiming = busy ? ' · 下一轮生效' : ''
  const permissionDescription =
    permissionMode === 'auto' ? '自动执行工具，可访问项目外路径' : '按当前策略审批操作'
  useEffect(() => {
    if (ref.current) {
      ref.current.style.height = 'auto'
      ref.current.style.height = Math.min(ref.current.scrollHeight, 190) + 'px'
    }
  }, [draft])
  useEffect(() => {
    const focus = () => ref.current?.focus()
    window.addEventListener('paicli:focus', focus)
    return () => window.removeEventListener('paicli:focus', focus)
  }, [])
  return (
    <div className="composer-area">
      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault()
          if (draft.trim() && !busy && !sendBlocked) onSend()
        }}
      >
        <textarea
          ref={ref}
          aria-label="向 PaiCLI 发送消息"
          placeholder={
            unmanagedRun
              ? '可先写下想法，或新建会话…'
              : busy
                ? 'Agent 正在工作，你可以先写下下一步想法…'
                : '描述你的想法，剩下的交给 PaiCLI…'
          }
          value={draft}
          rows={2}
          onChange={(e) => setDraft(e.target.value)}
          onCompositionStart={() => setComposing(true)}
          onCompositionEnd={() => setComposing(false)}
          onKeyDown={(e) => {
            if (
              e.key === 'Enter' &&
              !e.shiftKey &&
              !e.nativeEvent.isComposing &&
              !composing &&
              e.keyCode !== 229
            ) {
              e.preventDefault()
              if (draft.trim() && !busy && !sendBlocked) onSend()
            }
          }}
        />
        <div className="composer-toolbar">
          <div className="composer-options">
            <Dropdown
              trigger={
                <Button variant="ghost" size="sm" className="mode-button">
                  {mode === 'react' ? <Zap size={14} /> : <ListChecks size={14} />}
                  <span>{mode === 'react' ? '智能执行' : '规划并执行'}</span>
                  <ChevronDown size={12} />
                </Button>
              }
              items={[
                { label: '智能执行 · ReAct', onSelect: () => setMode('react') },
                { label: '规划并执行 · Plan', onSelect: () => setMode('plan') },
              ]}
            />
            <Dropdown
              trigger={
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className={`mode-button permission-button ${permissionMode === 'auto' ? 'full-access' : ''}`}
                  aria-label="审批模式"
                  disabled={sendBlocked}
                  title={permissionDescription + permissionTiming}
                >
                  {changingPermission ? (
                    <LoaderCircle size={14} className="spin" />
                  ) : (
                    <ShieldCheck size={14} />
                  )}
                  <span>{permissionMode === 'auto' ? '完全访问' : '默认审批'}</span>
                  <ChevronDown size={12} />
                </Button>
              }
              items={[
                {
                  label: '默认审批' + permissionTiming,
                  onSelect: () => setPermissionMode('default'),
                },
                {
                  label: '完全访问 · 自动执行' + permissionTiming,
                  onSelect: () => setPermissionMode('auto'),
                },
              ]}
            />
            <span className="composer-divider" />
            <span className="model-label">
              {source === 'demo' ? '演示 Agent' : 'Runtime 默认模型'}
            </span>
          </div>
          {unmanagedRun ? (
            <Button type="button" size="sm" onClick={onNewConversation}>
              新建会话
            </Button>
          ) : busy ? (
            <Button
              type="button"
              size="icon"
              className="send-button"
              disabled={!canStop || submitting || stopping}
              title={stopping ? '正在停止' : canStop ? '停止执行' : '当前没有可停止的回合'}
              aria-label={stopping ? '正在停止' : '停止执行'}
              onClick={onStop}
            >
              {stopping ? (
                <LoaderCircle size={17} className="spin" />
              ) : (
                <Square size={13} fill="currentColor" />
              )}
            </Button>
          ) : (
            <Button
              size="icon"
              className="send-button"
              type="submit"
              disabled={!draft.trim() || sendBlocked}
              aria-label="发送消息"
            >
              {submitting ? <LoaderCircle size={17} className="spin" /> : <ArrowUp size={19} />}
            </Button>
          )}
        </div>
      </form>
      <div className="composer-caption">
        <span>
          <GitBranch size={11} /> 当前项目
        </span>
        <span>
          {unmanagedRun
            ? '当前 Runtime 无法管理本轮运行'
            : busy
              ? '权限切换下一轮生效'
              : source === 'demo'
                ? '演示模式 · 不会修改真实文件'
                : permissionMode === 'auto'
                  ? '完全访问 · 工具自动执行，可访问项目外路径'
                  : '操作将在本地项目中执行'}
        </span>
        <span>
          <CornerDownLeft size={11} />
          发送 <span className="caption-shift">· Shift + Enter 换行</span>
        </span>
      </div>
    </div>
  )
}
