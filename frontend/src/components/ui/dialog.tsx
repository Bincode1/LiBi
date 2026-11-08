import * as DialogPrimitive from '@radix-ui/react-dialog'
import { X } from 'lucide-react'
import { AnimatePresence } from 'motion/react'
import * as m from 'motion/react-m'
import { useRef, type ReactNode } from 'react'
import { useSurfaceMotion } from '@/lib/motion'
import { Button } from './button'

export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  side,
  restoreFocus,
  children,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description?: string
  side?: 'left' | 'right'
  restoreFocus?: () => void
  children: ReactNode
}) {
  const opener = useRef<HTMLElement | null>(null)
  const backdrop = useSurfaceMotion({}, 0.18)
  const panel = useSurfaceMotion(
    side ? { x: side === 'left' ? -22 : 22 } : { y: 8, scale: 0.975 },
    side ? 0.24 : 0.22,
  )

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <AnimatePresence>
        {open && (
          <DialogPrimitive.Portal forceMount key="surface">
            <DialogPrimitive.Overlay asChild>
              <m.div className={side ? 'drawer-overlay' : 'dialog-overlay'} {...backdrop} />
            </DialogPrimitive.Overlay>
            <DialogPrimitive.Content
              asChild
              {...(!description && { 'aria-describedby': undefined })}
              onOpenAutoFocus={() => {
                const active = document.activeElement
                if (active instanceof HTMLElement) opener.current = active
              }}
              onCloseAutoFocus={(event) => {
                event.preventDefault()
                if (restoreFocus) restoreFocus()
                else opener.current?.focus({ preventScroll: true })
              }}
            >
              <m.div
                className={side ? `workspace-drawer drawer-${side}` : 'dialog-content'}
                {...panel}
              >
                <DialogPrimitive.Title className={side ? 'sr-only' : 'dialog-title'}>
                  {title}
                </DialogPrimitive.Title>
                {description && (
                  <DialogPrimitive.Description className="dialog-description">
                    {description}
                  </DialogPrimitive.Description>
                )}
                {children}
                {!side && (
                  <DialogPrimitive.Close asChild>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="dialog-close"
                      aria-label="关闭弹窗"
                    >
                      <X size={18} />
                    </Button>
                  </DialogPrimitive.Close>
                )}
              </m.div>
            </DialogPrimitive.Content>
          </DialogPrimitive.Portal>
        )}
      </AnimatePresence>
    </DialogPrimitive.Root>
  )
}
