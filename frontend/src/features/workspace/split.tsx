import { useLayoutEffect, useRef, type ReactNode } from 'react'
import { Group, Panel, Separator, useGroupRef } from 'react-resizable-panels'

export const conversationMinWidth = 500
export const detailsMinWidth = 288
export const splitterWidth = 10

const sidePanels = {
  navigation: { label: '调整会话栏宽度', defaultWidth: 224, minWidth: 200, maxWidth: 320 },
  details: {
    label: '调整工作详情宽度',
    defaultWidth: 332,
    minWidth: detailsMinWidth,
    maxWidth: 560,
  },
} as const

export function WorkspaceSplit({
  side,
  panel,
  contentMinWidth,
  children,
}: {
  side: keyof typeof sidePanels
  panel?: ReactNode
  contentMinWidth: number
  children: ReactNode
}) {
  const { label, defaultWidth, minWidth, maxWidth } = sidePanels[side]
  const storageKey = `libi-${side}-width`
  const preferredWidth = useRef<number | null>(null)
  if (preferredWidth.current === null) {
    const saved = Number(localStorage.getItem(storageKey))
    preferredWidth.current = saved >= minWidth && saved <= maxWidth ? saved : defaultWidth
  }

  const groupRef = useGroupRef()
  const element = useRef<HTMLDivElement>(null)
  const contentId = `${side}-content`
  const hasPanel = Boolean(panel)

  useLayoutEffect(() => {
    const group = element.current
    if (!group || !hasPanel) return

    // Reapply the preferred pixel width when docking or when the available space changes.
    const observer = new ResizeObserver(() => {
      const available = group.clientWidth - splitterWidth
      if (available < contentMinWidth + minWidth) return
      const width = Math.min(preferredWidth.current ?? defaultWidth, available - contentMinWidth)
      const percent = (width / available) * 100
      const layout =
        side === 'navigation'
          ? { [side]: percent, [contentId]: 100 - percent }
          : { [contentId]: 100 - percent, [side]: percent }
      groupRef.current?.setLayout(layout)
    })
    observer.observe(group)
    return () => observer.disconnect()
  }, [contentId, contentMinWidth, defaultWidth, groupRef, hasPanel, minWidth, side])

  const sidePanel = hasPanel && (
    <Panel
      id={side}
      defaultSize={defaultWidth}
      minSize={minWidth}
      maxSize={maxWidth}
      groupResizeBehavior="preserve-pixel-size"
      className="workspace-split-content"
      style={{ overflow: 'hidden' }}
    >
      {panel}
    </Panel>
  )
  const separator = hasPanel && (
    <Separator
      className="workspace-splitter"
      aria-label={label}
      style={{ width: splitterWidth }}
      disableDoubleClick
    />
  )

  return (
    <Group
      orientation="horizontal"
      groupRef={groupRef}
      elementRef={element}
      onLayoutChanged={(layout, { isUserInteraction }) => {
        const group = element.current
        if (!isUserInteraction || !group || layout[side] === undefined) return
        const width = Math.round((layout[side] / 100) * (group.clientWidth - splitterWidth))
        if (width < minWidth || width > maxWidth) return
        preferredWidth.current = width
        localStorage.setItem(storageKey, String(width))
      }}
    >
      {side === 'navigation' && sidePanel}
      {side === 'navigation' && separator}
      <Panel
        id={contentId}
        minSize={hasPanel ? contentMinWidth : 0}
        className="workspace-split-content"
        style={{ overflow: 'hidden' }}
      >
        {children}
      </Panel>
      {side === 'details' && separator}
      {side === 'details' && sidePanel}
    </Group>
  )
}
