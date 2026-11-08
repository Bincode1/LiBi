import { useEffect, useRef, useState, type ReactNode } from 'react'
import { ArrowUp, CircleAlert, LoaderCircle, Plus, ShieldCheck, Square } from 'lucide-react'
import { AnimatePresence } from 'motion/react'
import * as m from 'motion/react-m'
import { DraftAttachment } from '@/components/ai-elements/attachments'
import { Button } from '@/components/ui/button'
import { ChoiceMenu } from '@/components/ui/dropdown-menu'
import { useSurfaceMotion } from '@/lib/motion'
import type { Mode, PermissionMode } from '@/lib/types'
import { ModePicker } from './mode-picker'

const imageSuffix = /\.(png|jpe?g|webp)$/i
const fileSuffix =
  /\.(png|jpe?g|webp|txt|md|log|csv|tsv|jsonl?|ya?ml|toml|xml|html|css|[jt]sx?|py|rs|go|java|c|h|cpp|sql|sh|ps1)$/i
const fileLimit = 10 * 1024 * 1024
export const isImageAttachment = (file: File) => imageSuffix.test(file.name)
const fileIdentity = (file: File) => `${file.name}:${file.size}:${file.lastModified}`

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
  attachments = [],
  onAttachmentsChange,
  imagesBlocked = false,
  onModelSettings,
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
  attachments?: File[]
  onAttachmentsChange?: (files: File[]) => void
  imagesBlocked?: boolean
  onModelSettings?: () => void
}) {
  const ref = useRef<HTMLTextAreaElement>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const [composing, setComposing] = useState(false)
  const [fileError, setFileError] = useState('')
  const sendBlocked = submitting || syncing || changingPermission || Boolean(connectionError)
  const canSend = Boolean(draft.trim() || attachments.length) && !sendBlocked && !imagesBlocked
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
    window.addEventListener('libi:focus', focus)
    return () => window.removeEventListener('libi:focus', focus)
  }, [])
  function addFiles(incoming: FileList) {
    if (!onAttachmentsChange || submitting) return
    const next = [...attachments]
    let error = ''
    for (const file of incoming) {
      if (next.some((item) => fileIdentity(item) === fileIdentity(file))) continue
      if (!fileSuffix.test(file.name)) {
        error = '请选择图片、文本、日志或常见代码文件。'
      } else if (!file.size || file.size > fileLimit) {
        error = '附件不能为空，单个文件不能超过 10 MiB。'
      } else if (
        next.length >= 5 ||
        next.reduce((size, item) => size + item.size, file.size) > 2 * fileLimit
      ) {
        error = '每条消息最多 5 个附件，合计不超过 20 MiB。'
      } else {
        next.push(file)
      }
    }
    setFileError(error)
    onAttachmentsChange(next)
  }
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
        onDragOver={(event) => {
          if (event.dataTransfer.types.includes('Files')) {
            event.preventDefault()
            event.dataTransfer.dropEffect = submitting ? 'none' : 'copy'
          }
        }}
        onDrop={(event) => {
          if (event.dataTransfer.files.length) {
            event.preventDefault()
            addFiles(event.dataTransfer.files)
          }
        }}
        onPaste={(event) => {
          if (event.clipboardData.files.length) {
            event.preventDefault()
            addFiles(event.clipboardData.files)
          }
        }}
        onSubmit={(e) => {
          e.preventDefault()
          if (canSend && !busy) onSend()
        }}
      >
        <input
          ref={fileInput}
          type="file"
          hidden
          multiple
          accept="image/png,image/jpeg,image/webp,text/*,.log,.csv,.tsv,.json,.jsonl,.xml,.yaml,.yml,.toml,.py,.js,.jsx,.ts,.tsx,.rs,.go,.java,.c,.h,.cpp,.sql,.sh,.ps1"
          onChange={(event) => {
            if (event.target.files) addFiles(event.target.files)
            event.target.value = ''
          }}
        />
        {attachments.length > 0 && (
          <ul className="attachment-list" aria-label="待发送附件">
            {attachments.map((file) => (
              <DraftAttachment
                key={fileIdentity(file)}
                file={file}
                image={isImageAttachment(file)}
                disabled={submitting}
                onRemove={() => {
                  setFileError('')
                  onAttachmentsChange?.(attachments.filter((item) => item !== file))
                }}
              />
            ))}
          </ul>
        )}
        {fileError && (
          <p className="composer-attachment-notice" role="alert">
            {fileError}
          </p>
        )}
        {imagesBlocked && (
          <p className="composer-attachment-notice" role="status">
            当前模型未启用图片输入。{' '}
            <button type="button" onClick={onModelSettings}>
              设置模型
            </button>
          </p>
        )}
        {submitting && attachments.length > 0 && (
          <p className="composer-attachment-notice" role="status">
            正在提交附件…
          </p>
        )}
        {attachments.length > 0 && !submitting && !imagesBlocked && !fileError && (
          <p className="composer-attachment-notice">发送后，附件内容会提供给当前模型。</p>
        )}
        <textarea
          ref={ref}
          aria-label="向 LiBi 发送消息"
          placeholder={
            unmanagedRun
              ? '可先写下想法，或新建会话…'
              : busy
                ? 'Agent 正在工作，你可以先写下下一步想法…'
                : '描述你的想法，剩下的交给 LiBi…'
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
              if (canSend && !busy) onSend()
            }
          }}
        />
        <div className="composer-toolbar">
          <div className="composer-options">
            <Button
              type="button"
              size="icon"
              variant="ghost"
              title="添加附件，也可粘贴或拖放"
              aria-label="添加附件"
              disabled={submitting || !onAttachmentsChange}
              onClick={() => fileInput.current?.click()}
            >
              <Plus size={17} aria-hidden="true" />
            </Button>
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
              disabled={busy ? !canStop || submitting || stopping : !canSend}
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
