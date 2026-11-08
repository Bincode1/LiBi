# PaiCLI Workspace

本地 Agent 工作台，使用 React、TypeScript、Vite、shadcn/ui 风格的 Radix 基础组件与 Tailwind CSS 4。开发环境通过 Vite 代理连接 Python Runtime。

## 启动

需要 Node.js 22.12+（或符合 Vite 要求的较新版本）与 pnpm 10。

```powershell
pnpm --dir frontend install
uv run paicli workbench --cwd .
```

在仓库根目录运行上述命令。工作台会启动本机 Runtime，等待前端能访问后端后打开浏览器。默认前端端口为 `24157`，已占用时可用 `--port` 指定其他端口。使用 `--no-browser` 可手动打开终端中显示的地址。

每次启动会为 Runtime 分配一个本机端口，并自动把临时访问凭据交给前端代理。模型凭据沿用项目现有配置。已启用的 STDIO MCP 由 Runtime 自动启动，HTTP 和 SSE 服务按配置地址连接。

关闭浏览器标签后工作台继续运行。重启时，在启动终端按 Ctrl+C 停止工作台及其 Runtime，再运行同一个命令。后台启动可以加 `--no-browser`。命令输出 `PaiCLI workbench ready` 表示前端已连通 Runtime。

如果已有分开启动的 Vite 和 Runtime，切换前先停止两者，再运行 `uv run paicli workbench --cwd .`。统一入口会重新配置代理，原浏览器地址可继续使用。

## 独立 Runtime API 与手动前端联调

需要固定 Runtime 端口供外部程序接入，或单独调试前端代理时，可分别启动。

1. 在项目根目录按现有配置方式提供模型凭据，通过环境变量注入 Runtime 访问凭据 `PAICLI_RUNTIME_API_KEY`。
2. 在根目录运行 `uv run paicli serve --cwd .`。后端按 `.paicli/config.json` 的 `runtime.port` 监听，默认 `24158`，端口占用时启动会退出。修改 Python 后端代码后，重启 Runtime 以加载新代码。
3. 复制本目录 `.env.example` 为 `.env.local`，将 **同一个 Runtime 访问凭据** 通过 `PAICLI_WEB_RUNTIME_KEY` 提供给 Vite 服务端；可以改用进程环境变量注入。不要把模型密钥用作网页配置。
4. 重启 `pnpm dev`，打开工作台。在“设置与连接”中可以查看桥接状态。

代理目标在 `vite.config.ts` 的 `defaultRuntimeOrigin` 中配置为 `http://127.0.0.1:24158`。只有连接独立测试后端时才需用 `PAICLI_WEB_RUNTIME_URL` 显式覆盖，该值只允许本机 HTTP origin。访问凭据只存在于 Vite 服务端，并通过代理 Header 注入，不进入浏览器 bundle、URL 或浏览器存储。跨源浏览器请求被本地桥接拒绝。

当前代理仅供本机开发，绑定 127.0.0.1。`pnpm preview` 只预览静态构建，**不包含 Runtime 代理**。生产交付需要 Python 静态托管或同源网关，并补 Web 会话鉴权。不要把 Vite 开发代理作为公网服务部署。

## 已接入与待补齐

| 能力 | 当前真实模式 |
| --- | --- |
| 健康状态 | `GET /v1/health` |
| 列出、创建会话 | `GET / POST /v1/sessions` |
| 会话标题 | 首条消息触发一次后台生成，顶部的铅笔按钮可手动改名。标题通过 SSE 同步 |
| 历史与运行事件 | `GET /v1/sessions/{id}/events/stream?cursor=N&view=conversation`，SSE 推送会话界面所需的历史和更新 |
| 异步执行 | `POST /v1/sessions/{id}/turns/async`，提交 message 和 react/plan mode |
| 后台任务 | `GET / POST /v1/tasks`，`POST /v1/tasks/{id}/cancel` |
| 连接器 | `GET /v1/mcp/servers` 查看服务，支持测试连接、编辑用户级服务、启停和导入 JSON |
| 文字实时增量 | SSE 推送 `assistant_preview`，Streamdown 渲染生成中的 Markdown。带工具调用的 ReAct 文字作为中途说明 |
| 子 agent | 主对话显示委派与状态，右侧「子 agent」列出进行中和已结束的任务。点开后订阅独立的历史与实时输出 |
| 停止异步回合 | `POST /v1/sessions/{id}/runs/{runId}/cancel` 请求停止，收到 `turn/canceled` 后可继续原会话 |
| 网页审批 | 异步回合启用 `interactive_approval`，通过 `POST /v1/sessions/{id}/approvals/{approvalId}/decision` 允许本次或拒绝 |
| 会话权限 | `GET / POST /v1/sessions/{id}/permissions` 查询或设置 `default / auto`；完全访问自动执行工具并允许项目外路径 |
| 文件 diff、结构化计划 | 待后端提供，真实模式明确显示待接入 |
| 模型选择 | 使用 Runtime 默认模型，不显示虚假的可选模型 |

真实模式的列表按 Runtime 的工作区过滤。尚无标题的会话使用缩短的会话 ID，手动命名优先于自动生成。
同一会话有运行中的回合时，再次提交返回 409。

## 后端接口预留约定

`src/lib/types.ts` 定义 `RuntimeClient`，`src/lib/api.ts` 实现本地 Runtime 适配器。UI 通过 capabilities 判定已接入能力，未接入的操作返回 `CapabilityError`。

后端下一阶段可以按以下**建议接口**补齐（当前尚未调用）：

| 建议接口 | 返回与约束 |
| --- | --- |
| `GET /v1/capabilities` | 返回 cancelTurn、approvals、changes、modelSelection 与协议版本 |
| `GET /v1/sessions/{id}/changes` | 当前会话范围内的文件路径、增删统计与 diff，不用整个工作树冒充 Agent 改动 |
| `GET /v1/sessions/{id}/plan` | 结构化计划及步骤状态；不是只有文字的计划回答 |

SSE 的 `page` 事件使用与 `events.json` 相同的数据结构。`assistant_preview` 是当前步骤的累计文本 `{ turn_id, step_id, text }`，独立于历史游标。生成完成后，同一步骤的 `assistant/message` 接管显示。整轮终态由 `turn/completed`、`turn/failed`、`turn/canceled` 确定。`sequence` 只要求同会话有序，不假定数值连续。
异步提交先保存用户消息，再返回 `run_id`、`turn_id` 和 `after_sequence`。前端通过 `turn_id` 匹配 SSE 回合，可在第一帧 SSE 到达前用 `run_id` 停止回合。`active_run` 提供 `{ run_id, status }`，浏览器刷新后可恢复对活跃回合的停止操作。

审批 UI 使用 AI Elements Confirmation，并通过事件信封中的 `approval_id` 对应 `approval/started`、`approval/approved`、`approval/rejected`、`approval/canceled`。审批请求有效期为 5 分钟。浏览器断线后可重新连接并重放事件。同一 Runtime 监听端口重启后会结束它能识别的中断回合及待处理审批。无法确认归属的旧回合会显示说明，并提供新建会话入口。

待处理审批直接显示，已处理审批合并到一行“已处理 N 项审批”，默认折叠，展开后在固定高度内滚动。输入框下方可切换“默认审批 / 完全访问”。提交时固定本轮权限，完全访问关闭该轮的审批、路径和命令守卫。运行中切换只影响下一轮，等待中的审批仍需明确处理。选择由 SSE 的 `permission_mode` 同步，浏览器刷新保留，新会话和 Runtime 重启后使用默认策略。

订阅实现在 `src/hooks/use-event-feed.ts`。切换会话时关闭原连接，回到已打开的会话时先显示本页内存中的历史，再从该会话的游标续接。断开时自动重试最多三次，连接失败后也可以手动重试。

主会话的 `subagents` 摘要随 SSE 同步，子会话正文通过
`GET /v1/sessions/{id}/subagents/{childSessionId}/events/stream` 按需读取，每个页面只保持
当前主会话和打开的子会话订阅。待审批操作带来源名称，详情入口跳回主会话审批卡。内部结果
投递在会话视图里表示为 `agent/results_received`，汇总后的回答接管正文，前一稿可以在执行过程里查看。
子 agent 头像由委派时的可选角色决定。历史任务根据任务名称匹配角色，无法归类时显示通用头像。

## 验证

```powershell
pnpm test
pnpm build
```

单元测试覆盖事件重放去重、稀疏序号、并行工具关联、增量与最终文本归并、回合终态、审批和 API 错误边界。浏览器验证检查会话、发送/停止、审批、搜索、任务、主题和窄屏布局。

另以临时数据库启动现有 Python Runtime HTTP Handler，通过带服务端代理的独立 Vite 实例验证了健康、会话创建/查询、异步 Plan turn、事件游标、后台任务创建/查询/取消及跨源拒绝。该联调使用隔离执行器，不调用外部模型，不代表已完成真实模型端到端验证。

Markdown 不开启 raw HTML。错误提示不原样显示 provider 返回的诊断正文。工具输出的敏感信息脱敏应继续由后端 SessionStore 负责。
