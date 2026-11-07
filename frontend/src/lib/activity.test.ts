import { expect, it } from 'vitest'
import { avatarRole } from './activity'

it('prefers a persisted role and recognizes legacy task labels', () => {
  expect(avatarRole({ role: 'tester', label: '调研架构' })).toBe('tester')
  expect(avatarRole({ label: '检查当前目录文件结构' })).toBe('explorer')
  expect(avatarRole({ label: '调研 piagent 架构' })).toBe('researcher')
  expect(avatarRole({ role: 'future-role', label: 'Review the patch' })).toBe('reviewer')
  expect(avatarRole({ label: '处理下一步' })).toBe('general')
})
