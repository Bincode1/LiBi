import * as DropdownPrimitive from '@radix-ui/react-dropdown-menu'
import type { ReactNode } from 'react'
export function Dropdown({
  trigger,
  items,
}: {
  trigger: ReactNode
  items: { label: string; onSelect: () => void; disabled?: boolean }[]
}) {
  return (
    <DropdownPrimitive.Root>
      <DropdownPrimitive.Trigger asChild>{trigger}</DropdownPrimitive.Trigger>
      <DropdownPrimitive.Portal>
        <div className="dropdown-portal">
          <DropdownPrimitive.Content className="dropdown-content" align="start" sideOffset={8}>
            {items.map((item) => (
              <DropdownPrimitive.Item
                className="dropdown-item"
                key={item.label}
                onSelect={item.onSelect}
                disabled={item.disabled}
              >
                {item.label}
              </DropdownPrimitive.Item>
            ))}
          </DropdownPrimitive.Content>
        </div>
      </DropdownPrimitive.Portal>
    </DropdownPrimitive.Root>
  )
}
