import { useEffect, useState } from 'react'
import { FileText, Image, X } from 'lucide-react'
import { Dialog } from '@/components/ui/dialog'
import './attachments.css'

// Compact Attachment pattern adapted from shadcn/ui for the LiBi composer and transcript.
export function AttachmentChip({
  name,
  image,
  url,
  onRemove,
  disabled,
}: {
  name: string
  image?: boolean
  url?: string
  onRemove?: () => void
  disabled?: boolean
}) {
  const [previewOpen, setPreviewOpen] = useState(false)
  const thumbnail = image ? (
    url ? (
      <img src={url} alt="" />
    ) : (
      <Image size={16} aria-hidden="true" />
    )
  ) : (
    <FileText size={16} aria-hidden="true" />
  )
  const content = (
    <>
      {thumbnail}
      <span title={name}>{name}</span>
    </>
  )
  return (
    <li className="attachment-chip">
      {image && url ? (
        <>
          <button
            type="button"
            className="attachment-chip-preview"
            aria-haspopup="dialog"
            aria-label={`预览 ${name}`}
            onClick={() => setPreviewOpen(true)}
          >
            {content}
          </button>
          <Dialog
            open={previewOpen}
            onOpenChange={setPreviewOpen}
            title={name}
            className="attachment-image-dialog"
          >
            <AttachmentImagePreview key={url} url={url} name={name} />
          </Dialog>
        </>
      ) : url && !onRemove ? (
        <a href={url} rel="noreferrer" download={name}>
          {content}
        </a>
      ) : (
        <div>{content}</div>
      )}
      {onRemove && (
        <button
          type="button"
          className="attachment-chip-remove"
          aria-label={`移除 ${name}`}
          disabled={disabled}
          onClick={onRemove}
        >
          <X size={15} aria-hidden="true" />
        </button>
      )}
    </li>
  )
}

function AttachmentImagePreview({ url, name }: { url: string; name: string }) {
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')

  return (
    <>
      <img
        src={url}
        alt={name}
        hidden={status !== 'ready'}
        onLoad={() => setStatus('ready')}
        onError={() => setStatus('error')}
      />
      {status !== 'ready' && (
        <p className="attachment-image-status" role="status">
          {status === 'loading' ? '正在加载图片…' : '图片加载失败，请关闭后重试。'}
        </p>
      )}
    </>
  )
}

export function DraftAttachment({
  file,
  image,
  onRemove,
  disabled,
}: {
  file: File
  image: boolean
  onRemove: () => void
  disabled: boolean
}) {
  const [url, setUrl] = useState<string>()
  useEffect(() => {
    if (!image) return
    const objectUrl = URL.createObjectURL(file)
    setUrl(objectUrl)
    return () => URL.revokeObjectURL(objectUrl)
  }, [file, image])
  return (
    <AttachmentChip
      name={file.name}
      image={image}
      url={url}
      onRemove={onRemove}
      disabled={disabled}
    />
  )
}
