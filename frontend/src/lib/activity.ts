import type { Subagent } from './types'

export const avatarRoles = {
  explorer: '项目探索',
  researcher: '资料调研',
  builder: '开发实现',
  reviewer: '代码审查',
  tester: '测试验证',
  writer: '文档写作',
  planner: '方案规划',
  general: '通用助手',
}

type AvatarRole = keyof typeof avatarRoles

const roleHints: [AvatarRole, RegExp][] = [
  ['reviewer', /审查|审阅|审计|评审|复核|\b(review(?:er|ing)?|audit)\b/i],
  ['researcher', /调研|研究|资料检索|查阅文献|\b(research(?:er|ing)?|investigate|survey)\b/i],
  ['writer', /写作|撰写|编写文档|文案|写报告|\b(writer|writing|copywriting|documentation)\b/i],
  ['tester', /测试|回归|验收|复现|验证|\b(test(?:er|ing|s)?|qa|regression|verify)\b/i],
  ['planner', /规划|排期|拆解任务|方案设计|架构设计|\b(plan(?:ner|ning)?|architect|roadmap)\b/i],
  ['builder', /实现|开发|修复|重构|构建|\b(implement(?:er|ation)?|builder|build|refactor|fix)\b/i],
  ['explorer', /探索|目录|代码阅读|梳理结构|\b(explor(?:e|er|ation)|scan|inventory)\b/i],
]

export function avatarRole(agent: Pick<Subagent, 'role' | 'label'>): AvatarRole {
  if (agent.role && Object.hasOwn(avatarRoles, agent.role)) return agent.role as AvatarRole
  return roleHints.find(([, hint]) => hint.test(agent.label))?.[0] ?? 'general'
}

const toolLabels = new Map([
  ['read_file', '读取项目文件'],
  ['write_file', '更新相关文件'],
  ['edit_file', '编辑文件'],
  ['bash', '执行命令'],
  ['grep', '搜索代码'],
  ['search_code', '搜索代码'],
  ['list_dir', '查看目录'],
  ['spawn_subagent', '委派子任务'],
  ['subagent', '委派子任务'],
  ['list_agents', '查看子 agent 状态'],
  ['send_message', '向子 agent 追加指令'],
  ['interrupt_agent', '停止子 agent'],
  ['web_search', '搜索网页'],
  ['web_fetch', '读取网页'],
  ['web_read', '查阅网页摘录'],
])

export const toolLabel = (name: string, fallback = name) => toolLabels.get(name) ?? fallback

export function subagentStatus(agent: Subagent) {
  const priorResult = agent.result_count > 0
  switch (agent.status) {
    case 'queued':
      return '排队中'
    case 'starting':
      return '正在启动'
    case 'running':
      return agent.tool_name
        ? `正在${toolLabel(agent.tool_name, `执行 ${agent.tool_name}`)}`
        : '正在处理'
    case 'approval':
      return '等待审批'
    case 'idle':
      return '已交付 · 等待指令'
    case 'completed': {
      const result = agent.result_count > 1 ? `已完成 · ${agent.result_count} 轮交付` : '已完成'
      return agent.close_reason === 'idle_expired' ? `${result} · 空闲到期` : result
    }
    case 'partial':
      return '已部分交付'
    case 'failed':
      return priorResult ? '续跑失败 · 有历史交付' : '失败'
    case 'timed_out':
      return priorResult ? '续跑超时 · 有历史交付' : '超时'
    case 'canceled':
      return priorResult ? '续跑已停止 · 有历史交付' : '已停止'
    case 'interrupted':
      return priorResult ? '已中断 · 有历史交付' : '已中断'
    default:
      return '状态未知'
  }
}

export const subagentIsActive = (agent: Subagent) =>
  ['queued', 'starting', 'running', 'approval'].includes(agent.status)

const launchFailures = new Map([
  ['PARENT_CLOSED', '本轮正在结束'],
  ['PROVIDER_UNAVAILABLE', '执行服务不可用'],
  ['INVALID_CWD', '工作目录无效'],
  ['DEPTH_LIMIT_REACHED', '已达到委派层级上限'],
  ['ACTIVATION_LIMIT_REACHED', '并行任务已达上限'],
  ['SESSION_PERSISTENCE_UNAVAILABLE', '无法保存子会话'],
])

export const subagentLaunchFailure = (content: string) =>
  launchFailures.get(content.split(':', 1)[0]) ?? '查看详情'
