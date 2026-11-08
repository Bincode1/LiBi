// Copyright 2023 Vercel, Inc.
// SPDX-License-Identifier: Apache-2.0
// Adapted from https://elements.ai-sdk.dev/components/confirmation for LiBi Runtime events.
// License: frontend/public/licenses/ai-elements.txt
import { createContext, useContext, type ComponentProps, type ReactNode } from 'react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

type ConfirmationState = 'approval-requested' | 'approval-responded' | 'output-denied'

interface ConfirmationContextValue {
  approval: { id: string; approved?: boolean }
  state: ConfirmationState
}

const ConfirmationContext = createContext<ConfirmationContextValue | null>(null)

function useConfirmation() {
  const context = useContext(ConfirmationContext)
  if (!context) throw new Error('Confirmation components must be used within Confirmation')
  return context
}

export function Confirmation({
  approval,
  state,
  ...props
}: ComponentProps<'div'> & ConfirmationContextValue) {
  return (
    <ConfirmationContext.Provider value={{ approval, state }}>
      <div role={state === 'approval-requested' ? 'alert' : undefined} {...props} />
    </ConfirmationContext.Provider>
  )
}

export function ConfirmationRequest({ children }: { children: ReactNode }) {
  return useConfirmation().state === 'approval-requested' ? children : null
}

export function ConfirmationAccepted({ children }: { children: ReactNode }) {
  const { approval, state } = useConfirmation()
  return state === 'approval-responded' && approval.approved === true ? children : null
}

export function ConfirmationRejected({ children }: { children: ReactNode }) {
  const { approval, state } = useConfirmation()
  return state === 'output-denied' && approval.approved === false ? children : null
}

export function ConfirmationActions({ className, ...props }: ComponentProps<'div'>) {
  if (useConfirmation().state !== 'approval-requested') return null
  return <div className={cn('flex items-center justify-end gap-2', className)} {...props} />
}

export function ConfirmationAction(props: ComponentProps<typeof Button>) {
  return <Button size="sm" type="button" {...props} />
}
