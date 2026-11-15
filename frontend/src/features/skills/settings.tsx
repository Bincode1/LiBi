import { useState } from 'react'
import * as Dropdown from '@radix-ui/react-dropdown-menu'
import { useMutation, useQuery } from '@tanstack/react-query'
import {
  BookOpen,
  CircleAlert,
  Ellipsis,
  FileText,
  FolderInput,
  Layers,
  Plus,
  RefreshCw,
  Search,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { ConversationMarkdown } from '@/features/conversation/markdown'
import { ApiError } from '@/lib/api'
import { errorMessage } from '@/lib/utils'
import { scopeLabels, skillsApi, skillsKey, type Skill, type SkillScope } from './api'
import { SkillEditor } from './editor'
import './settings.css'

export function skillStatus(skill: Skill) {
  if (skill.problem) return { label: '需要检查', tone: 'warning' }
  if (skill.overridden) return { label: '被覆盖', tone: 'neutral' }
  return skill.enabled ? { label: '已启用', tone: 'success' } : { label: '已停用', tone: 'neutral' }
}

export function filterSkills(skills: Skill[], query: string, scope: string, state: string) {
  const terms = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean)
  return skills.filter((skill) => {
    const text = [skill.name, skill.description, ...skill.tags].join(' ').toLocaleLowerCase()
    return (
      terms.every((term) => text.includes(term)) &&
      (scope === 'all' || skill.scope === scope) &&
      (state === 'all' ||
        (state === 'enabled'
          ? skill.enabled && !skill.overridden && !skill.problem
          : state === 'disabled'
            ? !skill.enabled && !skill.overridden && !skill.problem
            : !!skill.overridden || !!skill.problem))
    )
  })
}

function SkillCard({
  skill,
  busy,
  writable,
  onOpen,
  onEdit,
  onRemove,
  onToggle,
}: {
  skill: Skill
  busy: boolean
  writable: boolean
  onOpen: () => void
  onEdit: () => void
  onRemove: () => void
  onToggle: () => void
}) {
  const state = skillStatus(skill)
  return (
    <article
      className="skill-card"
      aria-label={skill.name}
      data-inactive={!skill.enabled || skill.overridden}
    >
      <div className="skill-card-top">
        <span className="skill-glyph" aria-hidden="true">
          <BookOpen size={19} strokeWidth={1.7} />
        </span>
        <div className="skill-card-title">
          <button type="button" onClick={onOpen}>
            {skill.name}
          </button>
          <span className="skill-state" data-tone={state.tone}>
            <i />
            {state.label}
          </span>
        </div>
        <Dropdown.Root>
          <Dropdown.Trigger asChild>
            <Button variant="ghost" size="icon" aria-label={`管理 ${skill.name}`} disabled={busy}>
              <Ellipsis size={17} />
            </Button>
          </Dropdown.Trigger>
          <Dropdown.Portal>
            <Dropdown.Content className="dropdown-content" align="end" sideOffset={4}>
              <Dropdown.Item className="dropdown-item" onSelect={onOpen}>
                查看详情
              </Dropdown.Item>
              {skill.scope !== 'builtin' && writable && (
                <>
                  <Dropdown.Item className="dropdown-item" onSelect={onEdit}>
                    编辑技能
                  </Dropdown.Item>
                  <Dropdown.Item className="dropdown-item skill-error" onSelect={onRemove}>
                    移除技能
                  </Dropdown.Item>
                </>
              )}
            </Dropdown.Content>
          </Dropdown.Portal>
        </Dropdown.Root>
      </div>
      <p className="skill-description">
        {skill.description || '尚未填写使用说明，编辑 description 可帮助 Agent 选择此技能。'}
      </p>
      <div className="skill-tags" aria-label="标签">
        {skill.tags.length ? (
          skill.tags.map((tag, index) => <span key={`${index}-${tag}`}>{tag}</span>)
        ) : (
          <span className="skill-no-tags">无标签</span>
        )}
      </div>
      {(skill.problem || skill.overridden) && (
        <p className="skill-card-note" title={skill.problem ?? undefined}>
          {skill.problem || `当前工作区使用同名的${scopeLabels[skill.overridden_by!]}技能。`}
        </p>
      )}
      <footer className="skill-card-footer">
        <div className="skill-meta">
          <span>{scopeLabels[skill.scope]}</span>
          <span title={skill.version || '未标注版本'}>
            {skill.version ? `v${skill.version.replace(/^v/, '')}` : '无版本'}
          </span>
          <span>{skill.file_count} 文件</span>
        </div>
        <button
          type="button"
          className="mcp-switch"
          role="switch"
          aria-checked={skill.enabled}
          aria-label={`启用 ${skill.name}`}
          title="只影响当前工作区，下一轮对话生效"
          disabled={busy || !writable || skill.overridden || !!skill.problem}
          onClick={onToggle}
        >
          <span className="mcp-switch-track" aria-hidden="true" />
        </button>
      </footer>
    </article>
  )
}

function SkillDetails({
  skill,
  canWrite,
  onClose,
  onEdit,
  onReset,
}: {
  skill: Skill
  canWrite: boolean
  onClose: () => void
  onEdit: () => void
  onReset: () => void
}) {
  const detail = useQuery({
    queryKey: ['skill-detail', skill.id],
    queryFn: () => skillsApi.detail(skill),
    retry: false,
  })
  const [tab, setTab] = useState<'body' | 'source' | 'files'>('body')
  return (
    <Dialog
      open
      className="skill-editor skill-detail"
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
      title={skill.name}
      description={skill.description || '技能详情'}
    >
      <dl className="skill-facts">
        <div>
          <dt>来源</dt>
          <dd>
            {scopeLabels[skill.scope]}
            {skill.scope === 'builtin' && ' · 只读'}
          </dd>
        </div>
        <div>
          <dt>版本</dt>
          <dd>{skill.version || '未标注'}</dd>
        </div>
        <div>
          <dt>正文</dt>
          <dd>{skill.characters.toLocaleString()} 字符</dd>
        </div>
        <div>
          <dt>文件</dt>
          <dd>{skill.file_count}</dd>
        </div>
      </dl>
      <p className="skill-path">
        <FileText size={14} aria-hidden="true" />
        <code>{skill.path}</code>
      </p>
      <div className="skill-detail-state">
        <span className="skill-state" data-tone={skillStatus(skill).tone}>
          <i />
          {skillStatus(skill).label}
        </span>
        <span>
          {skill.activation_source === 'project' ? '已为此工作区单独设置' : '沿用全局启停设置'}
        </span>
        {skill.activation_source === 'project' && canWrite && (
          <Button variant="ghost" size="sm" onClick={onReset}>
            恢复全局设置
          </Button>
        )}
      </div>
      {skill.overridden && (
        <p className="skill-field-help">
          同名技能优先级：项目 &gt; 用户 &gt; 内置。当前生效来源：
          {scopeLabels[skill.overridden_by!]}。
        </p>
      )}
      <div className="skill-tabs" role="group" aria-label="技能详情视图">
        <button type="button" aria-pressed={tab === 'body'} onClick={() => setTab('body')}>
          正文
        </button>
        <button type="button" aria-pressed={tab === 'source'} onClick={() => setTab('source')}>
          源文件
        </button>
        <button type="button" aria-pressed={tab === 'files'} onClick={() => setTab('files')}>
          资源文件
        </button>
      </div>
      {detail.isPending && <p role="status">正在读取完整技能…</p>}
      {detail.isError && (
        <p role="alert" className="skill-error">
          {errorMessage(detail.error)}
        </p>
      )}
      {detail.data && (
        <div className="skill-detail-content">
          {tab === 'body' ? (
            <ConversationMarkdown
              text={detail.data.document.replace(/^---\s*\n[\s\S]*?\n---\s*/, '')}
            />
          ) : tab === 'source' ? (
            <pre className="skill-source">{detail.data.document}</pre>
          ) : (
            <ul className="skill-resource-list">
              {detail.data.resources.map((file) => (
                <li key={file.path}>
                  <code>{file.path}</code>
                  <span>{file.bytes.toLocaleString()} B</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      <div className="skill-form-actions">
        <Button variant="outline" onClick={onClose}>
          关闭
        </Button>
        {skill.scope !== 'builtin' && canWrite && <Button onClick={onEdit}>编辑技能</Button>}
      </div>
    </Dialog>
  )
}

export function SkillSettings() {
  const list = useQuery({
    queryKey: skillsKey,
    queryFn: ({ signal }) => skillsApi.list(signal),
    retry: false,
  })
  const [query, setQuery] = useState('')
  const [scope, setScope] = useState<SkillScope | 'all'>('all')
  const [state, setState] = useState('all')
  const [editor, setEditor] = useState<{ revision: string; skill?: Skill; importing?: boolean }>()
  const [selected, setSelected] = useState<Skill>()
  const [remove, setRemove] = useState<Skill>()
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const skills = list.data?.skills ?? []
  const revision = list.data?.revision
  const effective = skills.filter(
    (skill) => skill.enabled && !skill.overridden && !skill.problem,
  ).length
  const matches = filterSkills(skills, query, scope, state)
  const change = useMutation({
    mutationFn: ({ action, skill }: { action: 'toggle' | 'reset' | 'remove'; skill: Skill }) =>
      skillsApi.save(action, {
        slug: skill.slug,
        scope: skill.scope,
        enabled: !skill.enabled,
        revision,
      }),
    onMutate: () => {
      setError('')
      setNotice('')
    },
    onSuccess: async (_, { action, skill }) => {
      setRemove(undefined)
      setSelected(undefined)
      setNotice(
        action === 'remove'
          ? `已移除 ${skill.name}。`
          : action === 'reset'
            ? '已恢复全局设置，下一轮对话生效。'
            : `${skill.name} 已${skill.enabled ? '停用' : '启用'}，仅影响此工作区，下一轮对话生效。`,
      )
      await list.refetch()
    },
    onError: (exception) => {
      setError(errorMessage(exception))
      setRemove(undefined)
      if (exception instanceof ApiError && exception.status === 409) void list.refetch()
    },
  })
  function edit(skill?: Skill, importing = false) {
    if (revision) {
      setSelected(undefined)
      setEditor({ revision, skill, importing })
    }
  }

  return (
    <>
      <main className="mcp-panel skills-panel">
        <div className="skills-page">
          <header className="mcp-heading skills-heading">
            <div>
              <h1>Skills</h1>
              <p>将工作流程变成技能，让 Agent 在合适的任务中复用。</p>
            </div>
            <div className="skills-heading-actions">
              <Button variant="outline" onClick={() => edit(undefined, true)} disabled={!revision}>
                <FolderInput size={15} /> 导入技能
              </Button>
              <Button onClick={() => edit()} disabled={!revision}>
                <Plus size={16} /> 新建技能
              </Button>
            </div>
          </header>
          <div className="skills-summary">
            <span>
              <Layers size={14} aria-hidden="true" />
              {skills.length} 个技能
            </span>
            <span className="skill-active-count">{effective} 个已启用</span>
            <span className="skills-workspace" title={list.data?.workspace}>
              当前工作区 · {list.data?.workspace || '读取中…'}
            </span>
          </div>
          <div className="skills-toolbar">
            <label className="skills-search">
              <Search size={16} aria-hidden="true" />
              <input
                type="search"
                placeholder="搜索名称、描述或标签"
                aria-label="搜索技能"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </label>
            <label className="sr-only" htmlFor="skills-scope">
              技能来源
            </label>
            <select
              id="skills-scope"
              value={scope}
              onChange={(e) => setScope(e.target.value as SkillScope | 'all')}
            >
              <option value="all">全部来源</option>
              <option value="user">用户技能</option>
              <option value="project">项目技能</option>
              <option value="builtin">内置技能</option>
            </select>
            <label className="sr-only" htmlFor="skills-state">
              技能状态
            </label>
            <select id="skills-state" value={state} onChange={(e) => setState(e.target.value)}>
              <option value="all">全部状态</option>
              <option value="enabled">已启用</option>
              <option value="disabled">已停用</option>
              <option value="other">被覆盖 / 需检查</option>
            </select>
            <Button
              variant="ghost"
              size="icon"
              aria-label="刷新技能列表"
              title="刷新列表"
              disabled={list.isFetching}
              onClick={() => {
                setError('')
                void list.refetch()
              }}
            >
              <RefreshCw size={16} />
            </Button>
          </div>
          {list.isPending && (
            <p className="mcp-feedback" role="status">
              正在扫描技能目录…
            </p>
          )}
          {list.isError && (
            <div className="skills-feedback skill-error" role="alert">
              <CircleAlert size={16} />
              {errorMessage(list.error)}
              <Button variant="ghost" onClick={() => void list.refetch()}>
                重试
              </Button>
            </div>
          )}
          {list.data && !list.data.feature_enabled && (
            <p className="skills-feedback" role="status">
              Runtime 已关闭 Skills 功能。可以管理技能，开启 LIBI_SKILL 后 Agent 才会加载。
            </p>
          )}
          {list.data?.warnings.map((warning) => (
            <p key={warning} className="skills-feedback skill-error" role="alert">
              {warning}
            </p>
          ))}
          {error && (
            <p className="skills-feedback skill-error" role="alert">
              {error}
            </p>
          )}
          {notice && (
            <p className="skills-feedback" role="status">
              {notice}
            </p>
          )}
          {list.isSuccess && !!matches.length && (
            <>
              <div className="skills-grid">
                {matches.map((skill) => (
                  <SkillCard
                    key={skill.id}
                    skill={skill}
                    busy={change.isPending}
                    writable={!!revision}
                    onOpen={() => setSelected(skill)}
                    onEdit={() => edit(skill)}
                    onRemove={() => setRemove(skill)}
                    onToggle={() => change.mutate({ action: 'toggle', skill })}
                  />
                ))}
              </div>
              <p className="skills-footnote">
                显示 {matches.length} / {skills.length} 个技能 · 开关仅影响当前工作区 ·
                同名优先级：项目 &gt; 用户 &gt; 内置
              </p>
            </>
          )}
          {list.isSuccess && !matches.length && (
            <div className="skills-empty">
              <BookOpen size={28} strokeWidth={1.5} aria-hidden="true" />
              <h2>{skills.length ? '没有匹配的技能' : '添加你的第一个技能'}</h2>
              <p>
                {skills.length
                  ? '调整搜索词、来源或状态筛选。'
                  : '导入已有技能，或把常用的工作流程写成 SKILL.md。'}
              </p>
              <Button
                variant="outline"
                disabled={!revision && !skills.length}
                onClick={() => {
                  if (skills.length) {
                    setQuery('')
                    setScope('all')
                    setState('all')
                  } else edit(undefined, true)
                }}
              >
                {skills.length ? '清除筛选' : '导入技能'}
              </Button>
            </div>
          )}
        </div>
      </main>
      {editor && (
        <SkillEditor
          {...editor}
          onClose={() => setEditor(undefined)}
          onSaved={(message) => {
            setEditor(undefined)
            setNotice(message)
            setError('')
            void list.refetch()
          }}
        />
      )}
      {selected && (
        <SkillDetails
          key={selected.id}
          skill={selected}
          canWrite={!!revision && !change.isPending}
          onClose={() => setSelected(undefined)}
          onEdit={() => edit(selected)}
          onReset={() => change.mutate({ action: 'reset', skill: selected })}
        />
      )}
      {remove && (
        <Dialog
          open
          title="移除技能"
          description={`将删除${scopeLabels[remove.scope]}技能 ${remove.name} 的整个目录及其 ${remove.file_count} 个文件。`}
          onOpenChange={(open) => {
            if (!open && !change.isPending) setRemove(undefined)
          }}
        >
          <p className="skill-path">
            <code>{remove.path}</code>
          </p>
          <p className="skill-field-help">若存在同名的低优先级技能，移除后会重新使用该版本。</p>
          <div className="skill-form-actions">
            <Button
              variant="outline"
              disabled={change.isPending}
              onClick={() => setRemove(undefined)}
            >
              取消
            </Button>
            <Button
              className="skill-danger"
              disabled={change.isPending}
              onClick={() => change.mutate({ action: 'remove', skill: remove })}
            >
              {change.isPending ? '移除中…' : '移除技能'}
            </Button>
          </div>
        </Dialog>
      )}
    </>
  )
}
