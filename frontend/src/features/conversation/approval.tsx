import { useEffect, useState } from 'react'
import { Check, ShieldCheck, X } from 'lucide-react'
import { Disclosure } from '@/components/ui/disclosure'
import {
  Confirmation,
  ConfirmationAccepted,
  ConfirmationAction,
  ConfirmationActions,
  ConfirmationRejected,
  ConfirmationRequest,
} from '@/components/ai-elements/confirmation'
import { ApiError } from '@/lib/api'
import type { ApprovalView } from '@/lib/events'
import { errorMessage } from '@/lib/utils'

type ApprovalDecision = 'approve' | 'deny'

export function ApprovalCard({
  approval,
  canApprove,
  onApprove,
  notify,
  agentLabel,
}: {
  approval: ApprovalView
  canApprove: boolean
  onApprove: (id: string, decision: ApprovalDecision) => Promise<void>
  notify: (message: string) => void
  agentLabel?: string
}) {
  const [deciding, setDeciding] = useState(false)
  const expiresAt = approval.expiresAt ? Date.parse(approval.expiresAt) : NaN
  const [unavailable, setUnavailable] = useState(() => expiresAt <= Date.now())
  const pending = approval.status === 'pending' && !unavailable
  const accepted = approval.status === 'approved'
  const input = approval.input ?? {}
  const inputKeys = Object.keys(input)
  const fileAction =
    approval.toolName === 'write_file'
      ? '写入文件'
      : approval.toolName === 'edit_file'
        ? '编辑文件'
        : undefined
  const filePath = fileAction && typeof input.path === 'string' ? input.path : undefined
  let detail = approval.command
  if (inputKeys.length > 0) {
    detail =
      inputKeys.length === 1 && typeof input.command === 'string'
        ? input.command
        : JSON.stringify(input, null, 2)
  }

  useEffect(() => {
    if (!pending || !Number.isFinite(expiresAt)) return
    const timer = setTimeout(() => setUnavailable(true), Math.max(0, expiresAt - Date.now()))
    return () => clearTimeout(timer)
  }, [expiresAt, pending])

  const decide = async (decision: ApprovalDecision) => {
    if (deciding || !pending || !canApprove) return
    if (expiresAt <= Date.now()) {
      setUnavailable(true)
      return
    }
    setDeciding(true)
    try {
      await onApprove(approval.id, decision)
    } catch (error) {
      if (error instanceof ApiError && (error.status === 404 || error.status === 409))
        setUnavailable(true)
      setDeciding(false)
      notify(errorMessage(error))
    }
  }

  return (
    <Confirmation
      className="approval-card"
      approval={{ id: approval.id, approved: pending ? undefined : accepted }}
      state={pending ? 'approval-requested' : accepted ? 'approval-responded' : 'output-denied'}
    >
      <div className="approval-heading">
        <ShieldCheck size={16} aria-hidden="true" />
        <strong>
          <ConfirmationRequest>
            {agentLabel ? `${agentLabel} · 等待审批` : '等待审批'}
          </ConfirmationRequest>
          <ConfirmationAccepted>已允许本次操作</ConfirmationAccepted>
          <ConfirmationRejected>
            {approval.status === 'rejected'
              ? '已拒绝本次操作'
              : approval.status === 'canceled'
                ? '审批已取消'
                : '审批已失效'}
          </ConfirmationRejected>
        </strong>
        {approval.toolName && !filePath && <code>{approval.toolName}</code>}
      </div>
      {filePath && (
        <p className="approval-target">
          {fileAction} <code>{filePath}</code>
        </p>
      )}
      {detail &&
        (filePath ? (
          <Disclosure className="approval-parameters" label="查看操作参数">
            <pre>{detail}</pre>
          </Disclosure>
        ) : (
          <pre>{detail}</pre>
        ))}
      {canApprove ? (
        <ConfirmationActions className="approval-actions" aria-busy={deciding}>
          <ConfirmationAction
            variant="outline"
            disabled={deciding}
            onClick={() => void decide('deny')}
          >
            <X size={14} />
            拒绝
          </ConfirmationAction>
          <ConfirmationAction disabled={deciding} onClick={() => void decide('approve')}>
            <Check size={14} />
            允许本次
          </ConfirmationAction>
        </ConfirmationActions>
      ) : (
        <ConfirmationRequest>当前无法提交审批。</ConfirmationRequest>
      )}
    </Confirmation>
  )
}
