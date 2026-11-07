import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
export function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : '操作失败，请重试。'
}
