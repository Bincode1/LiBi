import type { TurnUsage } from '@/lib/events'

const tokenFormat = new Intl.NumberFormat('zh-CN')
const cacheFormat = new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 1 })

export function TurnUsageSummary({
  usage,
  current = false,
}: {
  usage?: TurnUsage
  current?: boolean
}) {
  const tokens = usage ? `${tokenFormat.format(usage.totalTokens)} tokens` : '—'
  const rate = usage?.cacheContextTokens
    ? cacheFormat.format(Math.min(1, usage.cacheHitTokens / usage.cacheContextTokens))
    : '—'
  return (
    <span className="turn-usage">
      <span>{current ? `本轮 ${tokens}` : tokens}</span>
      <span title="按已报告缓存数据的输入 Token 统计">缓存命中 {rate}</span>
    </span>
  )
}
