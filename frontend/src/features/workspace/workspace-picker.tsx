import * as Dropdown from '@radix-ui/react-dropdown-menu'
import { useQuery } from '@tanstack/react-query'
import { ArrowUp, Check, ChevronDown, ChevronRight, Folder, FolderOpen, Home } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import type { RuntimeClient } from '@/lib/types'
import { errorMessage } from '@/lib/utils'

const storageKey = 'paicli-recent-workspaces-v1'
const recentLimit = 8

export function useRecentWorkspaces() {
  const [recent, setRecent] = useState<string[]>(() => {
    try {
      const stored: unknown = JSON.parse(localStorage.getItem(storageKey) ?? '[]')
      if (Array.isArray(stored)) {
        return stored
          .filter((path): path is string => typeof path === 'string' && !!path.trim())
          .slice(0, recentLimit)
      }
    } catch {
      // Directory selection also works when browser storage is unavailable.
    }
    return []
  })
  useEffect(() => {
    try {
      localStorage.setItem(storageKey, JSON.stringify(recent))
    } catch {
      // Keep the selection in memory for this tab.
    }
  }, [recent])
  const remember = (path: string) =>
    setRecent((previous) =>
      previous[0] === path
        ? previous
        : [path, ...previous.filter((item) => item !== path)].slice(0, recentLimit),
    )
  return { recent, remember }
}

function folderName(path: string) {
  if (/^(?:[A-Za-z]:)?[\\/]+$/.test(path)) return path
  return path.split(/[\\/]/).filter(Boolean).at(-1) ?? path
}

export function SessionWorkspace({ path }: { path: string }) {
  return (
    <div className="workspace-context">
      <span className="workspace-indicator" title={path}>
        <Folder size={15} aria-hidden="true" />
        <span className="sr-only">工作目录</span>
        <span className="workspace-name">{folderName(path)}</span>
      </span>
    </div>
  )
}

export function WorkspacePicker({
  client,
  defaultPath,
  path,
  recent,
  disabled,
  onSelect,
}: {
  client: RuntimeClient
  defaultPath: string
  path: string
  recent: string[]
  disabled: boolean
  onSelect: (path: string) => void
}) {
  const [browsing, setBrowsing] = useState(false)
  const browseAfterMenu = useRef(false)
  const trigger = useRef<HTMLButtonElement>(null)
  const paths = [...new Set([...recent, defaultPath].filter(Boolean))]
  return (
    <div className="workspace-context">
      <Dropdown.Root>
        <Dropdown.Trigger asChild>
          <Button
            ref={trigger}
            type="button"
            variant="ghost"
            size="sm"
            className="workspace-indicator choice-trigger"
            aria-label={path ? `工作目录：${folderName(path)}` : '选择工作目录'}
            title={path || undefined}
            disabled={disabled}
          >
            <Folder size={15} aria-hidden="true" />
            <span className="workspace-name">{path ? folderName(path) : '工作目录'}</span>
            <ChevronDown size={13} aria-hidden="true" />
          </Button>
        </Dropdown.Trigger>
        <Dropdown.Portal>
          <Dropdown.Content
            className="dropdown-content workspace-menu"
            side="top"
            align="start"
            sideOffset={6}
            collisionPadding={10}
            onCloseAutoFocus={(event) => {
              if (!browseAfterMenu.current) return
              event.preventDefault()
              browseAfterMenu.current = false
              setBrowsing(true)
            }}
          >
            <Dropdown.Label className="workspace-menu-label">最近使用的目录</Dropdown.Label>
            <Dropdown.RadioGroup value={path} onValueChange={onSelect}>
              {paths.map((item) => (
                <Dropdown.RadioItem
                  className="dropdown-item workspace-menu-item"
                  key={item}
                  value={item}
                  textValue={folderName(item)}
                  title={item}
                >
                  <Folder size={15} aria-hidden="true" />
                  <span className="workspace-menu-text">
                    <span className="workspace-name">{folderName(item)}</span>
                    <span className="workspace-name dropdown-item-description">{item}</span>
                  </span>
                  <Dropdown.ItemIndicator className="dropdown-check">
                    <Check size={15} aria-hidden="true" />
                  </Dropdown.ItemIndicator>
                </Dropdown.RadioItem>
              ))}
            </Dropdown.RadioGroup>
            <Dropdown.Separator className="workspace-menu-separator" />
            <Dropdown.Item
              className="dropdown-item workspace-menu-item"
              onSelect={() => {
                browseAfterMenu.current = true
              }}
            >
              <FolderOpen size={15} aria-hidden="true" />
              浏览文件夹…
            </Dropdown.Item>
          </Dropdown.Content>
        </Dropdown.Portal>
      </Dropdown.Root>
      <Dialog
        open={browsing}
        onOpenChange={setBrowsing}
        title="选择工作目录"
        description="浏览此电脑上的文件夹，或输入完整路径。"
        restoreFocus={() => trigger.current?.focus()}
      >
        <WorkspaceBrowser
          client={client}
          initialPath={path}
          defaultPath={defaultPath}
          onCancel={() => setBrowsing(false)}
          onSelect={(directory) => {
            onSelect(directory)
            setBrowsing(false)
          }}
        />
      </Dialog>
    </div>
  )
}

function WorkspaceBrowser({
  client,
  initialPath,
  defaultPath,
  onSelect,
  onCancel,
}: {
  client: RuntimeClient
  initialPath: string
  defaultPath: string
  onSelect: (path: string) => void
  onCancel: () => void
}) {
  const [target, setTarget] = useState(initialPath)
  const [input, setInput] = useState<string>()
  const listing = useQuery({
    queryKey: ['workspace-directory', target],
    queryFn: ({ signal }) => client.browseWorkspace(target, signal),
    retry: false,
    refetchOnWindowFocus: false,
  })
  const directory = listing.data
  const path = input ?? directory?.path ?? target
  const open = (next: string) => {
    setInput(undefined)
    if (next === target) void listing.refetch()
    else setTarget(next)
  }
  return (
    <>
      <form
        className="workspace-path-form"
        onSubmit={(event) => {
          event.preventDefault()
          if (path.trim()) open(path)
        }}
      >
        <input
          className="workspace-path-input"
          aria-label="文件夹路径"
          placeholder={defaultPath}
          spellCheck={false}
          value={path}
          onChange={(event) => setInput(event.target.value)}
        />
        <Button variant="outline" size="sm" type="submit" disabled={!path.trim()}>
          打开
        </Button>
      </form>
      <div className="workspace-browser-toolbar">
        <Button
          variant="ghost"
          size="sm"
          disabled={!directory?.parent || listing.isFetching}
          onClick={() => directory?.parent && open(directory.parent)}
        >
          <ArrowUp size={15} aria-hidden="true" />
          上一级
        </Button>
        {directory && (
          <Button
            variant="ghost"
            size="sm"
            disabled={listing.isFetching}
            onClick={() => open(directory.home)}
          >
            <Home size={15} aria-hidden="true" />
            主目录
          </Button>
        )}
        {directory && directory.roots.length > 0 && (
          <select
            className="workspace-roots"
            aria-label="切换到根目录"
            value=""
            disabled={listing.isFetching}
            onChange={(event) => open(event.target.value)}
          >
            <option value="" disabled>
              {directory.roots.length > 1 ? '磁盘' : '根目录'}
            </option>
            {directory.roots.map((root) => (
              <option key={root} value={root}>
                {root}
              </option>
            ))}
          </select>
        )}
      </div>
      <div className="workspace-directory-list" aria-busy={listing.isFetching}>
        {listing.isFetching ? (
          <p className="workspace-directory-state" role="status">
            正在读取文件夹…
          </p>
        ) : listing.error ? (
          <div className="workspace-directory-state" role="alert">
            <p className="inline-failure">{errorMessage(listing.error)}</p>
            <Button variant="ghost" size="sm" onClick={() => open(defaultPath)}>
              返回 Runtime 目录
            </Button>
          </div>
        ) : directory?.directories.length ? (
          directory.directories.map((item) => (
            <button
              className="workspace-directory-row"
              key={item.path}
              type="button"
              onClick={() => open(item.path)}
            >
              <Folder size={16} aria-hidden="true" />
              <span className="workspace-name">{item.name}</span>
              <ChevronRight size={14} aria-hidden="true" />
            </button>
          ))
        ) : (
          <p className="workspace-directory-state">此位置没有子文件夹。</p>
        )}
      </div>
      <div className="settings-footer">
        <Button variant="ghost" onClick={onCancel}>
          取消
        </Button>
        <Button
          disabled={
            !directory || listing.isFetching || Boolean(listing.error) || path !== directory.path
          }
          onClick={() => directory && onSelect(directory.path)}
        >
          使用此文件夹
        </Button>
      </div>
    </>
  )
}
