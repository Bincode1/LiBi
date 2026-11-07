import { defaultRehypePlugins, Streamdown } from 'streamdown'

const plugins = [defaultRehypePlugins.sanitize, defaultRehypePlugins.harden]
const animation = {
  animation: 'fadeIn',
  sep: 'char',
  duration: 120,
  easing: 'ease-out',
  stagger: 8,
  maxBacklogMs: 100,
} as const

export function ConversationMarkdown({
  text,
  commentary,
  streaming = false,
}: {
  text: string
  commentary?: boolean
  streaming?: boolean
}) {
  return (
    <div className={commentary ? 'markdown commentary' : 'markdown'}>
      <Streamdown
        animated={animation}
        isAnimating={streaming}
        mode={streaming ? 'streaming' : 'static'}
        rehypePlugins={plugins}
        controls={false}
      >
        {text}
      </Streamdown>
    </div>
  )
}
