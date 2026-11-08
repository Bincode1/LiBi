import { ChevronDown } from 'lucide-react'
import { AnimatePresence, useIsPresent, useReducedMotion } from 'motion/react'
import * as m from 'motion/react-m'
import { useId, useState, type ReactNode } from 'react'
import { easeOut } from '@/lib/motion'

export function DisclosureChevron({ expanded }: { expanded: boolean }) {
  const reduced = useReducedMotion()
  return (
    <m.span
      className="disclosure-chevron"
      aria-hidden="true"
      initial={false}
      animate={{ rotate: expanded ? 180 : 0 }}
      transition={{ duration: reduced ? 0 : 0.16, ease: easeOut }}
    >
      <ChevronDown size={13} />
    </m.span>
  )
}

function RevealBody({ children }: { children: ReactNode }) {
  const present = useIsPresent()
  const reduced = useReducedMotion()
  return (
    <m.div
      className="reveal-body"
      inert={!present}
      initial={reduced ? false : { height: 0, opacity: 0 }}
      animate={{ height: 'auto', opacity: 1, transitionEnd: { overflow: 'visible' } }}
      exit={{ height: 0, opacity: 0, overflow: 'clip' }}
      transition={{ duration: reduced ? 0 : 0.17, ease: easeOut }}
      style={{ overflow: 'clip' }}
    >
      {children}
    </m.div>
  )
}

export function Reveal({ open, children }: { open: boolean; children: ReactNode }) {
  return (
    <AnimatePresence initial={false}>{open && <RevealBody>{children}</RevealBody>}</AnimatePresence>
  )
}

export function Disclosure({
  className,
  label,
  children,
}: {
  className: string
  label: ReactNode
  children: ReactNode
}) {
  const [open, setOpen] = useState(false)
  const id = useId()
  return (
    <div className={className}>
      <button
        type="button"
        className="disclosure-trigger"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((value) => !value)}
      >
        {label}
        <DisclosureChevron expanded={open} />
      </button>
      <div id={id}>
        <Reveal open={open}>{children}</Reveal>
      </div>
    </div>
  )
}
