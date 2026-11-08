import * as DropdownPrimitive from '@radix-ui/react-dropdown-menu'
import { Check } from 'lucide-react'
import { AnimatePresence } from 'motion/react'
import * as m from 'motion/react-m'
import { useState, type ReactNode } from 'react'
import { useSurfaceMotion } from '@/lib/motion'
import { cn } from '@/lib/utils'
import { Button } from './button'
import { DisclosureChevron } from './disclosure'

export function ChoiceMenu<Value extends string>({
  label,
  value,
  options,
  onValueChange,
  icon,
  className,
  menuClassName,
  disabled,
  title,
}: {
  label: string
  value: Value
  options: readonly { value: Value; label: string; description?: string }[]
  onValueChange: (value: Value) => void
  icon?: ReactNode
  className?: string
  menuClassName?: string
  disabled?: boolean
  title?: string
}) {
  const [open, setOpen] = useState(false)
  const selectedLabel = options.find((option) => option.value === value)?.label ?? label
  const menu = useSurfaceMotion({ scale: 0.97 }, 0.16)

  return (
    <DropdownPrimitive.Root open={open} onOpenChange={setOpen}>
      <DropdownPrimitive.Trigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className={cn('choice-trigger', className)}
          aria-label={`${label}：${selectedLabel}`}
          disabled={disabled}
          title={title}
        >
          {icon}
          <span>{selectedLabel}</span>
          <DisclosureChevron expanded={open} />
        </Button>
      </DropdownPrimitive.Trigger>
      <AnimatePresence>
        {open && (
          <DropdownPrimitive.Portal forceMount key="menu">
            <DropdownPrimitive.Content asChild align="start" sideOffset={6} collisionPadding={10}>
              <m.div className={cn('dropdown-content', menuClassName)} {...menu}>
                <DropdownPrimitive.RadioGroup
                  value={value}
                  onValueChange={(next) => {
                    const option = options.find((item) => item.value === next)
                    if (option) onValueChange(option.value)
                  }}
                >
                  {options.map((option) => (
                    <DropdownPrimitive.RadioItem
                      className="dropdown-item"
                      key={option.value}
                      value={option.value}
                      textValue={option.label}
                    >
                      <span>
                        {option.label}
                        {option.description && (
                          <span className="dropdown-item-description">{option.description}</span>
                        )}
                      </span>
                      <span className="dropdown-check" aria-hidden="true">
                        <DropdownPrimitive.ItemIndicator>
                          <Check size={15} strokeWidth={2.4} />
                        </DropdownPrimitive.ItemIndicator>
                      </span>
                    </DropdownPrimitive.RadioItem>
                  ))}
                </DropdownPrimitive.RadioGroup>
              </m.div>
            </DropdownPrimitive.Content>
          </DropdownPrimitive.Portal>
        )}
      </AnimatePresence>
    </DropdownPrimitive.Root>
  )
}
