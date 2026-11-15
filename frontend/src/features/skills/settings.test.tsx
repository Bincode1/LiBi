import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it } from 'vitest'
import { SkillSettings, filterSkills, skillStatus } from './settings'
import { skillsKey, type Skill } from './api'

const base: Skill = {
  id: 'user:release-check',
  slug: 'release-check',
  name: 'release-check',
  scope: 'user',
  description: '验证发布流程和测试结果',
  version: '2.1',
  tags: ['release', '发布'],
  path: '/user/skills/release-check/SKILL.md',
  enabled: true,
  overridden: false,
  overridden_by: null,
  activation_source: 'default',
  problem: null,
  file_count: 3,
  characters: 2700,
  modified_at: 1,
}

it('combines search, source and effective state filters without hiding overridden definitions', () => {
  const project: Skill = { ...base, id: 'project:release-check', scope: 'project' }
  const overridden: Skill = { ...base, overridden: true, overridden_by: 'project' }
  const disabled: Skill = { ...base, id: 'user:writing', name: 'writing', enabled: false }
  const skills = [project, overridden, disabled]
  expect(filterSkills(skills, '发布 release', 'all', 'all')).toHaveLength(3)
  expect(filterSkills(skills, '', 'all', 'enabled')).toEqual([project])
  expect(filterSkills(skills, '', 'user', 'disabled')).toEqual([disabled])
  expect(filterSkills(skills, '', 'all', 'other')).toEqual([overridden])
  expect(skillStatus(overridden).label).toBe('被覆盖')
})

it('renders real inventory metadata, scope controls and runtime-disabled feedback', () => {
  const client = new QueryClient()
  client.setQueryData(skillsKey, {
    skills: [base],
    revision: 'r1',
    workspace: '/projects/libi',
    feature_enabled: false,
    warnings: [],
  })
  try {
    const html = renderToStaticMarkup(
      <QueryClientProvider client={client}>
        <SkillSettings />
      </QueryClientProvider>,
    )
    expect(html).toContain('release-check')
    expect(html).toContain('验证发布流程和测试结果')
    expect(html).toContain('3 文件')
    expect(html).toContain('v2.1')
    expect(html).toContain('Runtime 已关闭 Skills 功能')
    expect(html).toContain('aria-label="搜索技能"')
    expect(html).toContain('role="switch"')
    expect(html).toContain('aria-checked="true"')
  } finally {
    client.clear()
  }
})
