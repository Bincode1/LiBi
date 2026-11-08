import { useEffect, useRef, useState, type ReactNode } from 'react'
import { ArrowUp, CircleAlert, LoaderCircle, ShieldCheck, Square } from 'lucide-react'
import { AnimatePresence } from 'motion/react'
import * as m from 'motion/react-m'
import { Button } from '@/components/ui/button'
import { ChoiceMenu } from '@/components/ui/dropdown-menu'
import { useSurfaceMotion } from '@/lib/motion'
import type { Mode, PermissionMode } from '@/lib/types'
import { ModePicker } from './mode-picker'
export function Composer({
  workspace,
  draft,
  setDraft,
  mode,
  setMode,
  permissionMode,
  setPermissionMode,
  changingPermission,
  busy,
  unmanagedRun,
  submitting,
  syncing,
  connectionError,
  onReconnect,
  canStop,
  stopping,
  onSend,
  onStop,
  onNewConversation,
}: {
  workspace?: ReactNode
  draft: string
  setDraft: (value: string) => void
  mode: Mode
  setMode: (mode: Mode) => void
  permissionMode: PermissionMode
  setPermissionMode: (mode: PermissionMode) => void
  changingPermission: boolean
  busy: boolean
  unmanagedRun: boolean
  submitting: boolean
  syncing: boolean
  connectionError?: string
  onReconnect?: () => void
  canStop: boolean
  stopping: boolean
  onSend: () => void
  onStop: () => void
  onNewConversation: () => void
}) {
  const ref = useRef<HTMLTextAreaElement>(null)
  const [composing, setComposing] = useState(false)
  const sendBlocked = submitting || syncing || changingPermission || Boolean(connectionError)
  const actionMotion = useSurfaceMotion({ scale: 0.82 }, 0.14)
  const action = submitting || stopping ? 'pending' : busy ? 'stop' : 'send'
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
      {connectionError && (
        <div className="composer-notice connection-notice" role="status">
          <CircleAlert size={16} aria-hidden="true" />
          <span>{connectionError}</span>
          <Button type="button" variant="ghost" size="sm" onClick={onReconnect}>
            重试连接
          </Button>
        </div>
      )}
      {workspace}
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
          rows={1}
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
            <ModePicker
              label="执行模式"
              value={mode}
              onValueChange={setMode}
              className="mode-button"
            />
            <ChoiceMenu
              label="审批模式"
              value={permissionMode}
              onValueChange={setPermissionMode}
              className={`mode-button permission-button ${permissionMode === 'auto' ? 'full-access' : ''}`}
              disabled={sendBlocked}
              title={permissionDescription + permissionTiming}
              icon={
                changingPermission ? (
                  <LoaderCircle size={14} className="spin" aria-hidden="true" />
                ) : (
                  <ShieldCheck size={14} aria-hidden="true" />
                )
              }
              options={[
                {
                  value: 'default',
                  label: '默认审批',
                  description: busy ? '下一轮生效' : undefined,
                },
                {
                  value: 'auto',
                  label: '完全访问',
                  description: '无需审批，可访问项目外路径' + permissionTiming,
                },
              ]}
            />
          </div>
          {unmanagedRun ? (
            <Button type="button" size="sm" onClick={onNewConversation}>
              新建会话
            </Button>
          ) : (
            <Button
              size="icon"
              className="send-button"
              type={busy ? 'button' : 'submit'}
              disabled={busy ? !canStop || submitting || stopping : !draft.trim() || sendBlocked}
              aria-label={busy ? (stopping ? '正在停止' : '停止执行') : '发送消息'}
              title={
                busy
                  ? stopping
                    ? '正在停止'
                    : canStop
                      ? '停止执行'
                      : '当前没有可停止的回合'
                  : 'Enter 发送，Shift + Enter 换行'
              }
              onClick={busy ? onStop : undefined}
            >
              <span className="composer-action-icon" aria-hidden="true">
                <AnimatePresence initial={false}>
                  <m.span key={action} {...actionMotion}>
                    {action === 'pending' ? (
                      <LoaderCircle size={17} className="spin" />
                    ) : action === 'stop' ? (
                      <Square size={13} fill="currentColor" />
                    ) : (
                      <ArrowUp size={19} />
                    )}
                  </m.span>
                </AnimatePresence>
              </span>
            </Button>
          )}
        </div>
      </form>
      {unmanagedRun && <p className="composer-notice">当前 Runtime 无法管理本轮运行</p>}
      {!unmanagedRun && permissionMode === 'auto' && (
        <p className="composer-notice">完全访问 · 自动执行，可访问项目外路径</p>
      )}
    </div>
  )
}
