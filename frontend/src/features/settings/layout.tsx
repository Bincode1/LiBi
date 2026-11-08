import type { ReactNode } from 'react'
import { ArrowLeft, Boxes, Plug, Settings2 } from 'lucide-react'
import { Link } from 'react-router-dom'
import '../mcp/settings.css'

export function SettingsLayout({
  page,
  returnTo,
  onConnection,
  children,
}: {
  page: 'models' | 'mcp'
  returnTo: string
  onConnection: () => void
  children: ReactNode
}) {
  return (
    <div className="mcp-settings">
      <nav className="mcp-nav" aria-label="设置导航">
        <Link to={returnTo}>
          <ArrowLeft size={16} aria-hidden="true" /> 返回工作台
        </Link>
        <div className="mcp-nav-group">
          <span>工作台</span>
          <Link
            to="/settings/models"
            state={{ returnTo }}
            aria-current={page === 'models' ? 'page' : undefined}
          >
            <Boxes size={16} aria-hidden="true" /> 模型
          </Link>
          <button type="button" onClick={onConnection}>
            <Settings2 size={16} aria-hidden="true" /> 本地连接
          </button>
        </div>
        <div className="mcp-nav-group">
          <span>集成</span>
          <Link
            to="/settings/mcp"
            state={{ returnTo }}
            aria-current={page === 'mcp' ? 'page' : undefined}
          >
            <Plug size={16} aria-hidden="true" /> MCP 服务
          </Link>
        </div>
      </nav>
      {children}
    </div>
  )
}
