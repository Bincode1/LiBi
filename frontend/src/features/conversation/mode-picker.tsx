import { ListChecks, Zap } from 'lucide-react'
import { ChoiceMenu } from '@/components/ui/dropdown-menu'
import type { Mode } from '@/lib/types'

const modes = [
  { value: 'react', label: '智能执行' },
  { value: 'plan', label: '规划并执行' },
] as const

export function ModePicker({
  label,
  value,
  onValueChange,
  className,
}: {
  label: string
  value: Mode
  onValueChange: (mode: Mode) => void
  className?: string
}) {
  const Icon = value === 'react' ? Zap : ListChecks
  return (
    <ChoiceMenu
      label={label}
      value={value}
      onValueChange={onValueChange}
      options={modes}
      icon={<Icon size={14} aria-hidden="true" />}
      className={className}
    />
  )
}
