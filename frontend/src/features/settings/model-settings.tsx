import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, CircleAlert, FlaskConical, LoaderCircle } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { request } from '@/lib/api'
import { errorMessage } from '@/lib/utils'
import './model-settings.css'

export interface ModelConnection {
  base_url: string
  model: string
  has_api_key: boolean
  image_input: boolean | null
  supports_images: boolean
}

export const modelSettingsEndpoint = '/v1/settings/model'
export const modelSettingsKey = ['model-settings'] as const
const normalizeUrl = (value: string) => value.trim().replace(/\/+$/, '')

export function ModelSettings() {
  const cache = useQueryClient()
  const settings = useQuery({
    queryKey: modelSettingsKey,
    queryFn: ({ signal }) => request<ModelConnection>(modelSettingsEndpoint, { signal }),
    retry: false,
  })

  return (
    <main className="mcp-panel">
      <div className="mcp-page">
        <header className="mcp-heading">
          <div>
            <h1>模型</h1>
            <p>配置 LiBi 在工作台里使用的 OpenAI 兼容对话服务。</p>
          </div>
        </header>
        {settings.isPending && (
          <p className="model-feedback" role="status">
            正在读取模型配置…
          </p>
        )}
        {settings.isError && (
          <div className="model-feedback">
            <p role="alert">{errorMessage(settings.error)}</p>
            <Button variant="outline" onClick={() => void settings.refetch()}>
              重试
            </Button>
          </div>
        )}
        {settings.data && (
          <ModelForm
            current={settings.data}
            onSaved={(saved) => cache.setQueryData(modelSettingsKey, saved)}
          />
        )}
      </div>
    </main>
  )
}

function ModelForm({
  current,
  onSaved,
}: {
  current: ModelConnection
  onSaved: (saved: ModelConnection) => void
}) {
  const [urlDraft, setUrlDraft] = useState<string>()
  const [key, setKey] = useState('')
  const [modelDraft, setModelDraft] = useState<string>()
  const [imageInput, setImageInput] = useState<boolean | null>()
  const [models, setModels] = useState<string[]>([])
  const [busy, setBusy] = useState<'test' | 'save'>()
  const [feedback, setFeedback] = useState<{
    action: 'test' | 'save'
    error?: boolean
    message: string
  }>()
  const baseUrl = urlDraft ?? current.base_url
  const model = modelDraft ?? current.model
  const imageMode =
    imageInput !== undefined
      ? imageInput
      : model === current.model && normalizeUrl(baseUrl) === current.base_url
        ? current.image_input
        : null
  const reuseKey = current.has_api_key && normalizeUrl(baseUrl) === current.base_url
  const canTest = Boolean(baseUrl.trim() && (key.trim() || reuseKey))

  async function submit(action: 'test' | 'save') {
    if (busy || !canTest || (action === 'save' && !model.trim())) return
    setBusy(action)
    setFeedback(undefined)
    const data = {
      method: 'POST',
      body: JSON.stringify({ base_url: baseUrl, api_key: key, model, image_input: imageMode }),
    }
    try {
      if (action === 'test') {
        setModels([])
        const result = await request<{ models: string[] }>(modelSettingsEndpoint + '/test', data)
        setModels(result.models)
        setFeedback({
          action,
          message: result.models.length
            ? `连接成功，服务返回 ${result.models.length} 个模型。`
            : '连接成功，服务返回了空列表。可以手动填写模型 ID。',
        })
      } else {
        const saved = await request<ModelConnection>(modelSettingsEndpoint, data)
        onSaved(saved)
        setUrlDraft(undefined)
        setModelDraft(undefined)
        setImageInput(undefined)
        setKey('')
        setFeedback({ action, message: '已保存。下一轮对话将使用此配置。' })
      }
    } catch (exception) {
      setFeedback({ action, error: true, message: errorMessage(exception) })
    } finally {
      setBusy(undefined)
    }
  }

  const message = feedback && (
    <p
      className={`model-feedback ${feedback.error ? 'model-feedback-error' : ''}`}
      role={feedback.error ? 'alert' : 'status'}
    >
      {feedback.error ? (
        <CircleAlert size={16} aria-hidden="true" />
      ) : (
        <Check size={16} aria-hidden="true" />
      )}
      {feedback.message}
    </p>
  )

  return (
    <form
      className="model-form"
      onSubmit={(event) => {
        event.preventDefault()
        void submit('save')
      }}
      aria-busy={Boolean(busy)}
    >
      <fieldset disabled={Boolean(busy)}>
        <legend>服务连接</legend>
        <label htmlFor="model-base-url">Base URL</label>
        <input
          id="model-base-url"
          type="url"
          required
          value={baseUrl}
          placeholder="https://api.example.com/v1"
          autoCapitalize="none"
          spellCheck={false}
          aria-describedby="model-base-url-hint"
          onChange={(event) => {
            const next = event.target.value
            setUrlDraft(next)
            setModelDraft(normalizeUrl(next) === current.base_url ? undefined : '')
            setImageInput(undefined)
            setKey('')
            setModels([])
            setFeedback(undefined)
          }}
        />
        <p id="model-base-url-hint">填写服务的 API 根地址，例如包含 /v1 的地址。</p>
        <label htmlFor="model-api-key">API Key</label>
        <input
          id="model-api-key"
          type="password"
          autoComplete="new-password"
          required={!reuseKey}
          value={key}
          placeholder={reuseKey ? '已配置，留空可保留' : '输入此服务的 API Key'}
          aria-describedby="model-api-key-hint"
          onChange={(event) => {
            setKey(event.target.value)
            setModels([])
            setFeedback(undefined)
          }}
        />
        <p id="model-api-key-hint">
          {reuseKey ? '更换密钥时输入新值。' : '每个服务地址使用自己的密钥。'}
        </p>
        <div className="model-test">
          <Button
            type="button"
            variant="outline"
            disabled={!canTest}
            onClick={() => void submit('test')}
          >
            {busy === 'test' ? (
              <LoaderCircle size={15} className="spin" aria-hidden="true" />
            ) : (
              <FlaskConical size={15} aria-hidden="true" />
            )}
            {busy === 'test' ? '正在获取模型…' : '测试并获取模型'}
          </Button>
          {feedback?.action === 'test' && message}
        </div>
      </fieldset>
      <fieldset disabled={Boolean(busy)}>
        <legend>对话模型</legend>
        {models.length > 0 && (
          <>
            <label htmlFor="available-models">可用模型</label>
            <select
              id="available-models"
              value={models.includes(model) ? model : ''}
              onChange={(event) => {
                setModelDraft(event.target.value)
                setImageInput(undefined)
              }}
            >
              <option value="" disabled>
                从服务返回的模型中选择
              </option>
              {models.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
          </>
        )}
        <label htmlFor="model-id">模型 ID</label>
        <input
          id="model-id"
          value={model}
          required
          maxLength={256}
          placeholder="例如 provider/model"
          autoCapitalize="none"
          spellCheck={false}
          onChange={(event) => {
            setModelDraft(event.target.value)
            setImageInput(undefined)
          }}
          aria-describedby="model-id-hint"
        />
        <p id="model-id-hint">也可以手动填写。返回列表中的模型能否用于对话，以服务商支持为准。</p>
        <label htmlFor="model-image-input">图片输入</label>
        <select
          id="model-image-input"
          aria-describedby="model-image-hint"
          value={imageMode === true ? 'enabled' : imageMode === false ? 'disabled' : 'auto'}
          onChange={(event) =>
            setImageInput(event.target.value === 'auto' ? null : event.target.value === 'enabled')
          }
        >
          <option value="auto">自动识别</option>
          <option value="enabled">支持图片</option>
          <option value="disabled">仅文本</option>
        </select>
        <p id="model-image-hint">自定义服务请按模型实际能力设置。获取模型列表不会测试视觉能力。</p>
      </fieldset>
      <div className="model-footer">
        <p>保存到本机，供工作台后续对话使用。</p>
        <Button type="submit" disabled={Boolean(busy) || !canTest || !model.trim()}>
          {busy === 'save' ? '正在保存…' : '保存模型配置'}
        </Button>
      </div>
      {feedback?.action === 'save' && message}
    </form>
  )
}
