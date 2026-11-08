import { useEffect, useState } from 'react'

const storageKey = 'paicli-drafts'

export function useTabDrafts() {
  const [drafts, setDrafts] = useState<Record<string, string>>(() => {
    try {
      const saved: unknown = JSON.parse(sessionStorage.getItem(storageKey) ?? '{}')
      if (saved && typeof saved === 'object' && !Array.isArray(saved)) {
        return Object.fromEntries(
          Object.entries(saved).filter(([, value]) => typeof value === 'string'),
        )
      }
    } catch {
      // The composer still works when browser storage is unavailable.
    }
    return {}
  })

  useEffect(() => {
    try {
      sessionStorage.setItem(
        storageKey,
        JSON.stringify(Object.fromEntries(Object.entries(drafts).filter(([, value]) => value))),
      )
    } catch {
      // Keep the current drafts in memory if browser storage is full or disabled.
    }
  }, [drafts])

  return [drafts, setDrafts] as const
}
