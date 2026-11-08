import { useEffect, useState } from 'react'
import { FileText, Image, X } from 'lucide-react'
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
      {url && !onRemove ? (
        <a
          href={url}
          target={image ? '_blank' : undefined}
          rel="noreferrer"
          download={image ? undefined : name}
        >
          {content}
        </a>
      ) : (
        <div>{content}</div>
      )}
      {onRemove && (
        <button type="button" aria-label={`移除 ${name}`} disabled={disabled} onClick={onRemove}>
          <X size={15} aria-hidden="true" />
        </button>
      )}
    </li>
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
