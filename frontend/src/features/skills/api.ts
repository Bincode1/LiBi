import { request } from '@/lib/api'

export type SkillScope = 'builtin' | 'user' | 'project'
export interface Skill {
  id: string
  slug: string
  name: string
  description: string
  scope: SkillScope
  version: string
  tags: string[]
  path: string
  enabled: boolean
  overridden: boolean
  overridden_by: SkillScope | null
  activation_source: 'project' | 'default'
  problem: string | null
  file_count: number
  characters: number
  modified_at: number
}
export interface SkillInventory {
  skills: Skill[]
  revision: string | null
  workspace: string
  feature_enabled: boolean
  warnings: string[]
}
export interface Resource {
  path: string
  bytes: number
}
export interface SkillDetail {
  document: string
  resources: Resource[]
}
export interface ImportPreview {
  fingerprint: string
  name: string
  description: string
  conflict: boolean
  files: Resource[]
}

function post<T>(action: string, body: object) {
  return request<T>(`/v1/skills/${action}`, { method: 'POST', body: JSON.stringify(body) })
}
export const skillsKey = ['skills'] as const
export const skillsApi = {
  list: (signal: AbortSignal) => request<SkillInventory>('/v1/skills', { signal }),
  detail: (skill: Pick<Skill, 'slug' | 'scope'>) => post<SkillDetail>('detail', skill),
  preview: (body: object) => post<ImportPreview>('preview', body),
  save: (action: 'add' | 'edit' | 'toggle' | 'reset' | 'remove' | 'import', body: object) =>
    post<{ saved: true }>(action, body),
}
export const scopeLabels: Record<SkillScope, string> = {
  builtin: '内置',
  user: '用户',
  project: '项目',
}
