import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { FileCode2, FolderInput, Upload } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { ConversationMarkdown } from '@/features/conversation/markdown'
import { ApiError } from '@/lib/api'
import { errorMessage } from '@/lib/utils'
import { skillsApi, type ImportPreview, type Skill } from './api'

const template = `---
name: my-workflow
description: 说明任务或触发条件，帮助 Agent 判断何时使用此技能。
version: "1.0.0"
tags: []
---

# 工作流程

1. 明确任务目标和约束。
2. 按顺序执行必要步骤。
3. 验证结果并说明结论。
`

export function SkillEditor({
  skill,
  importing = false,
  revision,
  onClose,
  onSaved,
}: {
  skill?: Skill
  importing?: boolean
  revision: string
  onClose: () => void
  onSaved: (message: string) => void
}) {
  const detail = useQuery({
    queryKey: ['skill-document', skill?.id, revision],
    queryFn: () => skillsApi.detail(skill!),
    enabled: !!skill,
    retry: false,
    staleTime: 0,
  })
  const [slug, setSlug] = useState(skill?.slug ?? (importing ? '' : 'my-workflow'))
  const [scope, setScope] = useState<'user' | 'project'>(
    skill?.scope === 'project' ? 'project' : 'user',
  )
  const [draft, setDraft] = useState<string>()
  const [source, setSource] = useState('')
  const [mode, setMode] = useState<'directory' | 'markdown'>(importing ? 'directory' : 'markdown')
  const [tab, setTab] = useState<'edit' | 'preview'>('edit')
  const [preview, setPreview] = useState<ImportPreview>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [stale, setStale] = useState(false)
  const [saveRevision, setSaveRevision] = useState(revision)
  const [latest, setLatest] = useState<string>()
  const document = draft ?? detail.data?.document ?? template
  const invalidate = () => {
    setPreview(undefined)
    setError('')
  }
  const body = {
    slug: slug.trim(),
    scope,
    ...(mode === 'directory' ? { source_path: source.trim() } : { document }),
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      if (importing && !preview) {
        setPreview(await skillsApi.preview(body))
      } else {
        await skillsApi.save(skill ? 'edit' : importing ? 'import' : 'add', {
          ...body,
          revision: saveRevision,
          ...(preview && { fingerprint: preview.fingerprint }),
        })
        onSaved(skill ? '技能已更新，下一轮对话生效。' : '技能已添加，Agent 可按需加载。')
      }
    } catch (exception) {
      setError(errorMessage(exception))
      if (exception instanceof ApiError && exception.status === 409) {
        setPreview(undefined)
        setStale(true)
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !busy) onClose()
      }}
      className="skill-editor"
      title={skill ? `编辑 ${skill.name}` : importing ? '导入技能' : '新建技能'}
      description={
        importing
          ? '导入单个技能目录，或粘贴完整的 SKILL.md。目录导入会保留配套资源。'
          : '用 Markdown 定义工作流程，description 帮助 Agent 判断何时加载。'
      }
    >
      <form onSubmit={submit} className="skill-form">
        <div className="skill-form-row">
          <label>
            技能名称
            <input
              required
              pattern="[a-z0-9][a-z0-9_-]{0,63}"
              maxLength={64}
              disabled={!!skill || busy}
              value={slug}
              placeholder="例如 release-check"
              onChange={(e) => {
                const value = e.target.value
                setSlug(value)
                if (!skill && !importing)
                  setDraft(document.replace(/^name: .+$/m, `name: ${value}`))
                invalidate()
              }}
            />
          </label>
          <label>
            保存范围
            <select
              value={scope}
              disabled={!!skill || busy}
              onChange={(e) => {
                setScope(e.target.value as 'user' | 'project')
                invalidate()
              }}
            >
              <option value="user">用户 · 所有工作区可用</option>
              <option value="project">项目 · 仅当前工作区</option>
            </select>
          </label>
        </div>
        {importing && (
          <div className="skill-tabs" role="group" aria-label="导入方式">
            <button
              type="button"
              aria-pressed={mode === 'directory'}
              onClick={() => {
                setMode('directory')
                invalidate()
              }}
            >
              <FolderInput size={15} /> 本地目录
            </button>
            <button
              type="button"
              aria-pressed={mode === 'markdown'}
              onClick={() => {
                setMode('markdown')
                invalidate()
              }}
            >
              <FileCode2 size={15} /> Markdown
            </button>
          </div>
        )}
        {importing && mode === 'directory' ? (
          <label>
            技能目录绝对路径
            <input
              required
              value={source}
              placeholder="D:\skills\release-check"
              onChange={(e) => {
                setSource(e.target.value)
                invalidate()
              }}
            />
            <span className="skill-field-help">
              选择 Runtime 所在电脑上包含 SKILL.md 的目录。最多 200 个文件、10 MiB。
            </span>
          </label>
        ) : (
          <>
            <div className="skill-document-bar">
              <div className="skill-tabs" role="group" aria-label="文档视图">
                <button type="button" aria-pressed={tab === 'edit'} onClick={() => setTab('edit')}>
                  SKILL.md
                </button>
                <button
                  type="button"
                  aria-pressed={tab === 'preview'}
                  onClick={() => setTab('preview')}
                >
                  预览正文
                </button>
              </div>
              {importing && (
                <label className="skill-upload">
                  <Upload size={14} /> 选择文件
                  <input
                    type="file"
                    accept=".md,text/markdown,text/plain"
                    className="sr-only"
                    onChange={async (e) => {
                      const file = e.target.files?.[0]
                      if (!file) return
                      if (file.size > 256_000) {
                        setError('SKILL.md 不能超过 256 KB。')
                        return
                      }
                      try {
                        setDraft(await file.text())
                        invalidate()
                      } catch {
                        setError('无法读取文件，请重新选择。')
                      }
                    }}
                  />
                </label>
              )}
            </div>
            {detail.isPending && skill ? (
              <p role="status">正在读取技能…</p>
            ) : detail.isError && skill ? (
              <p role="alert" className="skill-error">
                {errorMessage(detail.error)}
              </p>
            ) : tab === 'edit' ? (
              <textarea
                className="skill-code"
                aria-label="SKILL.md 内容"
                spellCheck={false}
                value={document}
                required
                onChange={(e) => {
                  setDraft(e.target.value)
                  invalidate()
                }}
              />
            ) : (
              <div className="skill-document-preview">
                <ConversationMarkdown text={document.replace(/^---\s*\n[\s\S]*?\n---\s*/, '')} />
              </div>
            )}
            <p className="skill-field-help">
              name 必须与技能名称一致。保留完整 YAML 元数据；编辑不会改动配套资源。
            </p>
          </>
        )}
        {preview && (
          <section className="skill-import-preview" aria-label="导入预览">
            <strong>{preview.name}</strong>
            <p>{preview.description}</p>
            <span>
              {preview.files.length} 个文件 ·{' '}
              {(preview.files.reduce((n, file) => n + file.bytes, 0) / 1024).toFixed(1)} KB
            </span>
            <ul>
              {preview.files.map((file) => (
                <li key={file.path}>
                  <code>{file.path}</code>
                  <span>{file.bytes.toLocaleString()} B</span>
                </li>
              ))}
            </ul>
            {preview.conflict && (
              <p className="skill-error" role="alert">
                此范围已有同名技能，请更换名称或编辑现有技能。
              </p>
            )}
          </section>
        )}
        {error && (
          <p role="alert" className="skill-error">
            {error}
          </p>
        )}
        {stale && (
          <div className="skill-field-help">
            草稿已保留。请读取最新版本，检查差异后再次保存。
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={busy}
              onClick={async () => {
                setBusy(true)
                try {
                  const inventory = await skillsApi.list(new AbortController().signal)
                  if (!inventory.revision) throw new Error(inventory.warnings.join(' '))
                  if (skill) setLatest((await skillsApi.detail(skill)).document)
                  setSaveRevision(inventory.revision)
                  setStale(false)
                  setError('已读取最新版本。保存时将使用当前编辑区的草稿。')
                } catch (exception) {
                  setError(errorMessage(exception))
                } finally {
                  setBusy(false)
                }
              }}
            >
              读取最新版本
            </Button>
          </div>
        )}
        {latest !== undefined && (
          <details className="skill-latest">
            <summary>查看磁盘上的最新版本</summary>
            <pre className="skill-source">{latest}</pre>
          </details>
        )}
        <div className="skill-form-actions">
          <Button type="button" variant="outline" disabled={busy} onClick={onClose}>
            取消
          </Button>
          <Button
            type="submit"
            disabled={busy || stale || !!preview?.conflict || (!!skill && !detail.isSuccess)}
          >
            {busy
              ? '处理中…'
              : importing && !preview
                ? '预览导入'
                : importing
                  ? '确认导入'
                  : skill
                    ? '保存更改'
                    : '创建技能'}
          </Button>
        </div>
      </form>
    </Dialog>
  )
}
