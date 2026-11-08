/// <reference types="vite/client" />

import { Terminal } from 'lucide-react'
import { avatarRole, avatarRoles } from '@/lib/activity'
import type { Subagent } from '@/lib/types'

export function AgentMark() {
  return (
    <span className="agent-mark" aria-hidden="true">
      <Terminal size={19} strokeWidth={1.9} />
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
