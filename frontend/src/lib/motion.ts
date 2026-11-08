import { useReducedMotion } from 'motion/react'

export const easeOut = [0.2, 0.8, 0.25, 1] as const
const easeIn = [0.45, 0, 0.85, 1] as const

export function useSurfaceMotion(
  offset: { x?: number | string; y?: number; scale?: number } = {},
  duration = 0.2,
) {
  const reduced = useReducedMotion()
  const rest = {
    ...(offset.x !== undefined && { x: 0 }),
    ...(offset.y !== undefined && { y: 0 }),
    ...(offset.scale !== undefined && { scale: 1 }),
  }

  return {
    initial: reduced ? (false as const) : { opacity: 0, ...offset },
    animate: {
      opacity: 1,
      ...rest,
      transition: { duration: reduced ? 0 : duration, ease: easeOut },
    },
    exit: {
      opacity: 0,
      ...(reduced ? rest : offset),
      transition: { duration: reduced ? 0 : duration * 0.65, ease: easeIn },
    },
  }
}
