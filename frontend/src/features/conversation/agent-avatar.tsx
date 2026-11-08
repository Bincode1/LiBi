/// <reference types="vite/client" />

import { useEffect, useRef } from 'react'
import { avatarRole, avatarRoles } from '@/lib/activity'
import type { Subagent } from '@/lib/types'

type AgentMarkVariant = 'primary' | 'avatar' | 'working' | 'effort' | 'done'

export function AgentMark({ variant = 'primary' }: { variant?: AgentMarkVariant }) {
  const markRef = useRef<HTMLSpanElement>(null)
  const animated = variant === 'effort'

  useEffect(() => {
    const mark = markRef.current
    if (!animated || !mark || typeof IntersectionObserver === 'undefined') return

    let inView = false
    const syncPlayback = () => mark.toggleAttribute('data-playing', inView && !document.hidden)
    const observer = new IntersectionObserver(([entry]) => {
      inView = entry.isIntersecting
      syncPlayback()
    })
    observer.observe(mark)
    document.addEventListener('visibilitychange', syncPlayback)
    return () => {
      observer.disconnect()
      document.removeEventListener('visibilitychange', syncPlayback)
      mark.removeAttribute('data-playing')
    }
  }, [animated])

  return (
    <span className={animated ? 'agent-mark agent-mark--effort' : 'agent-mark'} ref={markRef}>
      <img alt="" src={`${import.meta.env.BASE_URL}libi-icons/${variant}.png`} />
    </span>
  )
}

export function AgentAvatar({ agent }: { agent: Pick<Subagent, 'role' | 'label'> }) {
  const role = avatarRole(agent)
  const label = avatarRoles[role]
  return (
    <span className="subagent-mark" role="img" aria-label={label} title={label}>
      <img
        alt=""
        width={36}
        height={36}
        src={`${import.meta.env.BASE_URL}agent-icons/${role}.svg`}
      />
    </span>
  )
}
