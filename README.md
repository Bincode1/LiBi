# PaiCLI Python

PaiCLI Python 是一个运行在终端里的 AI Agent CLI，面向真实项目开发场景：读写文件、搜索代码、执行命令、联网检索、调用 MCP 工具、保存记忆、生成快照、恢复现场，并通过 Runtime API 对外提供线程、turn、事件和后台任务能力。

当前仓库以终端 CLI 为产品入口，支持交互式 REPL、单次 Prompt，以及 SDK、MCP 和后台任务接入。

![](https://cdn.paicoding.com/stutymore/best-city-ai-agent-jd-20260708142704.png)

这个仓库是 PaiCLI 的 Python 版本。它不是一个空壳 Demo，而是按真实 CLI 产品来做：核心路径有测试覆盖，也经过本地 smoke 和真实终端运行验证。

## 配套教程路线

如果你是为了学习 Agent 工程、准备简历或准备面试，可以先看这条教程路线：

[PaiCLI 学习路线：手搓一个 Java 版 Claude Code](https://paicoding.com/paicli-learning-path)

这篇路线不是单纯教你“从第一行源码看到最后一行”，而是按真实学习和求职路径来组织：

- 先把 PaiCLI 在本地跑起来，直观看到 ReAct、工具调用、Plan 模式和联网搜索是怎么工作的
- 再把项目能力拆成可以写进简历的模块，比如 ReAct、Plan-and-Execute、Memory、RAG、MCP、HITL、多模态和 Runtime API
- 然后围绕简历里写到的模块去深挖源码，并同步准备对应的 Agent 面试题
- 最后通过 debug、改 bug、加工具、整理踩坑笔记，把项目真正变成自己的工程经验

教程目录覆盖实战篇、简历篇和面试篇，适合作为学习 PaiCLI Java 版和理解本 Python 版设计取舍的路线图。

![](https://cdn.paicoding.com/stutymore/paicli-python-launch-20260708161001.png)

## 功能特性

- 交互式终端 Agent，基于 Rich 和 prompt-toolkit 渲染
- 单次 prompt 模式，适合脚本、管道和自动化调用
- OpenAI-compatible 流式 LLM 客户端，默认面向 DeepSeek 配置
- 支持 `DEEPSEEK_API_KEY` 等 provider-specific API Key
- ReAct 工具调用循环，支持 thinking、tool call、tool result、final output 和 usage 事件
- Plan-and-Execute 模式，使用独立 Planner 生成 DAG，并按依赖批次执行可并行任务
- ReAct 子 Agent 委派，支持独立上下文、事件观察、取消和生命周期管理
- 内置文件、Shell、grep、glob、记忆、网页搜索、网页抓取、代码搜索等工具
- HITL 人工确认、命令/路径安全策略和 JSONL 审计日志
- MCP client，支持 stdio 和 Streamable HTTP MCP server
- Skill 系统，支持内置、用户级和项目级分层、输入 Top-K 匹配、`load_skill` 当前回合懒加载，
  以及经 HITL 确认的 `save_skill` 流程沉淀
- Chrome DevTools MCP 配置助手
- PaiCLI 自身也可以作为 MCP server 暴露内置工具
- Runtime API，支持有历史的 session、turn、事件日志，以及带原子抢占、租约恢复、取消保护、
  项目隔离和 `react|plan` 模式的持久化后台任务
- 静态项目记忆 + SQLite 动态长期记忆，支持元数据、去重、TTL、容量治理和相关性召回
- 上下文预算与压缩：达到可用输入预算的 80% 后压缩旧轮次，保留近期消息和完整工具调用对
- 完整 usage、缓存命中/未命中 Token、reasoning Token 和可配置成本估算
- 首次获准修改工作区前创建快照，支持恢复现场
- 支持本地图片和远程图片输入，并根据模型能力自动降级

## 环境要求

- Python 3.11 或更新版本
- [uv](https://docs.astral.sh/uv/)
- 可选：`rg`，用于更快的本地搜索
- 可选：Chrome DevTools MCP 需要 Node.js 20.19.0 LTS 或更新版本、npm/npx 和 Chrome

## 快速开始

```bash
git clone https://github.com/itwanger/PaiCLI-Python.git
cd PaiCLI-Python
uv sync --extra dev
uv run paicli --help
```

启动交互模式：

```bash
uv run paicli
```

启动时可以通过持久化的 Session ID 继续之前的会话：

```bash
uv run paicli --session-id session_abc123
uv run paicli --session session_abc123 -p "继续刚才的任务"
```

也可以直接列出最近使用过的会话并交互选择：

```bash
uv run paicli -r
```

命令只读取会话元数据并显示最近 20 个会话，选择编号后才加载对应历史并进入 REPL；输入 `q` 可以取消。更早的会话可使用 `--session-id` 精确恢复。没有已保存会话时，命令会明确提示并退出。

Session ID 可以在 REPL 的 `/context` 输出中查看。指定的 ID 不存在时，CLI 会直接报错，不会自动创建一个新会话。

会话绑定创建时的工作区。CLI 的 `--resume`、`--session-id` 和网页工作台仅访问当前工作区的主会话。

单次查询：

```bash
uv run paicli -p "帮我总结这个项目"
```

选择运行模式并输出机器可读的 usage/cost：

```bash
uv run paicli --mode plan -p "先读取 README，再验证项目" --json
```

当 Bug 检查或代码审查没有指定范围时，默认提示会要求模型从改动、入口和测试证据界定范围，
批量读取独立的线索，在获得有用证据后报告观察和下一步，并在回答里说明验证结果与覆盖范围。
`read_file` 的带行号文本每次最多返回 50 KiB，另附续读提示。遇到超长单行时返回标明截断的预览。

检查当前环境：

```bash
uv run paicli doctor --cwd .
```

## 配置

PaiCLI 的配置优先级如下：

1. 内置默认配置
2. `~/.paicli/config.json`
3. 项目级 `.paicli/config.json`
4. 项目级 `.env`
5. CLI 参数
6. 当前进程环境变量

配置文件不存在时使用下一层配置；已存在的 JSON 配置若损坏、不是 JSON 对象或无法读取，
会明确报错。项目 `.env` 的读取错误也会向上传播，不会静默退回默认配置。

项目配置默认只接收模型名称等常规选项。模型提供商、连接地址、通用凭据、权限策略、
审计和外部资源路径来自用户配置、命令行或当前进程环境。需要加载项目文件里的这些设置时，
在该次启动的进程环境显式设置 `PAICLI_TRUST_PROJECT_CONFIG=1`。项目 `.env` 自身不能授予信任。
项目级 MCP 连接仍有独立的信任开关。

使用内置 DeepSeek 地址时，也可以在项目 `.env` 中提供 `DEEPSEEK_API_KEY`。
这只在可信配置没有提供 Key 或自定义地址时作为后备：

```dotenv
PAICLI_MODEL=deepseek-flash
DEEPSEEK_API_KEY=your_key_here
```

通用 Key 可以在当前进程环境中提供：

```bash
PAICLI_API_KEY=your_key_here uv run paicli
```

DeepSeek 的思考模式默认关闭。如需开启，可在 `.env` 或环境变量中设置：

```dotenv
PAICLI_THINKING=true
```

也可以在 `~/.paicli/config.json` 或项目级 `.paicli/config.json` 中配置
`{"llm": {"thinking_enabled": true}}`。该开关只对 DeepSeek provider 的请求发送
`thinking.type` 参数。

当前支持的 provider-specific API Key 包括：

- `DEEPSEEK_API_KEY`
- `ZAI_API_KEY`（GLM 官方推荐）
- `GLM_API_KEY`
- `STEP_API_KEY`
- `KIMI_API_KEY`

通过命令行临时覆盖 provider 和 model：

```bash
uv run paicli --provider deepseek --model deepseek-flash
```

连接本地 OpenAI-compatible 服务：

```bash
PAICLI_PROVIDER=openai-compatible \
PAICLI_BASE_URL=http://127.0.0.1:11434/v1 \
PAICLI_MODEL=qwen2.5-coder \
uv run paicli -p "解释这个仓库"
```

## 交互命令

进入 `uv run paicli` 后，可以使用这些 slash commands：

```text
/help
/exit
/clear
/context
/memory
/memory search <query>
/memory stats
/memory delete <id>
/memory clear
/save <fact>
/config
/tools
/hitl default|auto
/policy
/audit [N]
/index [path]
/search <query>
/plan <task>
/model
/model <model-id>
/model <provider> <model-id>
/usage
/skill
/skill list
/skill show <name>
/skill on <name>
/skill off <name>
/skill reload
/mcp
/task
/task add [--mode react|plan] <task>
/task cancel <task_id>
/task log <task_id>
/snapshot
/snapshot clean
/restore <snapshot-id-or-index>
```

`/model` 会打开交互式模型选择器：`Tab` 或左右方向键在 `Default`、`Custom`
之间切换，上下方向键选择模型，`Enter` 立即切换当前 Agent。`Custom` 中可以选择已保存的
BYOK 模型、创建新的 DeepSeek/GLM/OpenAI-compatible 配置，或按 `d` 删除配置。自定义配置
保存在权限为 `0600` 的 `~/.paicli/models.json`；建议填写 API Key 环境变量名，只有显式输入
API Key 时才会把密钥写入该文件。

## 内置工具

PaiCLI 内置了一组 Agent 可以调用的本地工具和联网工具：

- `read_file`
- `write_file`
- `list_dir`
- `glob`
- `grep`
- `bash`
- `web_search`
- `web_fetch`
- `save_memory`
- `search_memory`
- `load_skill`
- `save_skill`
- `search_code`
- `revert_turn`

写文件、执行命令、远程 MCP 写操作、恢复快照等危险动作，会经过 policy、HITL 和 audit 处理。
默认策略下，`write_file` 和 `edit_file` 必须获得审批。Runtime 网页工作台可在异步回合内审批。
SDK、单次 Prompt 和无人值守的 Runtime 调用缺少审批回调时会拒绝这些操作。审计日志在写入和 `/audit` 展示时都进行脱敏，覆盖字段、
正文内的常见凭据及当前已知密钥。
`save_skill` 也必须经过 HITL；模型可以提议沉淀，但不会静默改变后续行为。
递归搜索、目录树、glob 和代码索引跳过符号链接及 Windows junction。同一用户的 PaiCLI 写入
和编辑调用发生冲突时，后到的调用会得到文件忙的结果，可以稍后重试。旧代码索引会自动失效，
首次使用 `/index` 重建后恢复搜索。

交互模式下按 `Shift+Tab` 可在两种会话权限模式间切换：

- `Default`：使用启动时的 HITL、工作区路径和命令安全策略。
- `Auto (full access)`：当前会话内不再请求审批，并关闭路径与命令守卫；再次按
  `Shift+Tab` 会恢复启动时的默认策略。

网页工作台在输入框下方提供“默认审批 / 完全访问”切换。提交消息时固定本轮的审批、路径和
命令策略。选择完全访问的回合会自动执行工具，并允许访问项目外路径。运行中切换从下一轮
生效，当前待处理审批仍由用户批准或拒绝。已处理的审批合并到折叠历史中。权限选择按会话
隔离，新会话和 Runtime 重启后使用默认策略。

## Skill 匹配与沉淀

Skill 按 `builtin -> user -> project` 加载，同名时后层覆盖前层：

- builtin：产品默认能力
- user：`~/.paicli/skills/*/SKILL.md`，跨项目复用
- project：`.paicli/skills/*/SKILL.md`，最贴近当前仓库并拥有最高优先级

每次用户输入先用 name、description、tags 做中英文词法/字符 n-gram Top-K 匹配，再把候选交给模型决定是否调用 `load_skill`。Skill 正文只在真正加载后进入当前 ReAct 的下一模型轮；每个并发子 Agent 都有独立 Skill 缓冲区，不会串线。

当一次成功流程具备稳定输入、明确步骤和可复用边界时，模型可以调用 `save_skill` 提议写入 project 或 user 层。该工具默认拒绝覆盖已有 Skill，并强制人工确认。

## 记忆、动态 Prompt 与上下文压缩

PaiCLI 把记忆分成三层：

- 短期记忆：当前 session 的原始消息、工具调用和工具结果
- 静态长期记忆：`AGENTS.md`、`PAI.md`、`.paicli/PAI.md` 及自定义 prompt 文件；人工维护、可版本控制
- 动态长期记忆：按项目 scope 隔离的 SQLite 记录；包含 kind、source、importance、confidence、TTL、访问次数和内容哈希

动态记忆不会再无条件取“最近 8 条”。每个请求会按当前问题自动召回 Top-K，并把结果放进明确标注为 untrusted data 的动态 Prompt；模型觉得候选不足时，还可以调用 `search_memory` 深搜。写入端会拒绝空值/超长值，通过规范化哈希去重，并按项目容量淘汰低价值记录。

Prompt 分为可缓存的静态前缀和逐请求重建的动态后缀。静态前缀承载身份、规则和项目指令；动态后缀承载当前时间、cwd、模型、工具以及与当前问题相关的记忆。

可用输入预算按 `context_window - max_output_tokens - reserve_tokens` 计算。默认在该预算的 80% 触发压缩，压到 55% 左右，为后续输出、工具结果和无 tokenizer 估算误差留出空间。压缩摘要只属于短期会话，不会自动晋升为长期记忆。

新模型请求对往轮较长的工具正文保留头尾预览，每项最多 2,000 字符。保留用户消息、回答和工具调用配对，当前回合的工具正文照常投影。完整正文仍可从会话事件历史读取，旧请求继续按原有预算参数重放。

## 模型、Token 与费用

默认 provider/model 是 `deepseek/deepseek-flash`。DeepSeek V4 Flash/Pro 的内置 profile 使用 1M 上下文，并带有截至 2026-07-17 的官方每百万 Token 价格；价格会变化，因此可以用 `llm.context_window` 和 `llm.prices` 覆盖，未知 OpenAI-compatible 模型应显式配置。

流式请求开启 `stream_options.include_usage`，并解析 `choices=[]` 的 usage-only 块、cache hit/miss 和 reasoning Token。REPL 用 `/usage` 查看最近一次普通 ReAct，单次 CLI 用 `--json` 获取完整 usage/cost。成本以供应商返回的实际 Token 为准，不能只用“代码行数”精确推算。
SSE 支持 LF、CRLF 和 CR 换行。空流、截断的流、流中错误帧及无效 JSON 会记录为协议错误。
模型服务快速返回 HTTP 500、502、503 或 504 时，最多自动重试一次。从第一次尝试开始计算的重试窗口最长 5 秒，涵盖退避和重试等待响应头。首次请求及已开始的流式响应继续使用配置的模型超时。

## 联网工具

`web_search` 使用 DuckDuckGo HTML 搜索，返回标题、URL 和摘要。

`web_fetch` 可以抓取公开 HTTP/HTTPS 页面，并做基础正文提取。它会拒绝 `file://`、loopback、私有网络和内网地址，降低 SSRF 风险。
每次重定向都会重新验证目标，连接固定使用验证通过的公网 IP，同时保留原域名的 Host 和
TLS 校验。该工具直接连接公网，不读取环境变量中的 HTTP 代理配置。

如果需要登录态、浏览器状态或 JS 渲染页面，建议使用 Chrome DevTools MCP。

## MCP

PaiCLI 可以连接 MCP server，并把远端工具动态注册为：

```text
mcp__<server-name>__<tool-name>
```

初始化项目级 Chrome DevTools MCP 配置：

```bash
uv run paicli mcp init-chrome --scope project
```

它会写入 `.paicli/mcp.json`，内容类似：

```json
{
  "mcpServers": {
    "chrome-devtools": {
      "type": "stdio",
      "command": "npx",
      "args": [
        "-y",
        "chrome-devtools-mcp@latest",
        "--no-usage-statistics"
      ]
    }
  }
}
```

项目级 MCP 配置默认停用。确认信任该工作区后，可在启动 PaiCLI 的进程环境设置
`PAICLI_TRUST_PROJECT_MCP=1`。用户级 MCP 配置仍可自动加载，`mcp.auto_start=false` 关闭自动发现。
stdio 服务只继承 MCP SDK 的基础环境及该服务 `env` 中显式配置的变量。

连接已有 remote-debugging Chrome：

```bash
uv run paicli mcp init-chrome \
  --scope project \
  --browser-url http://127.0.0.1:9222
```

查看已配置的 MCP server：

```bash
uv run paicli mcp list
```

把 PaiCLI 自身作为 MCP server 暴露：

```bash
uv run paicli mcp serve --transport stdio
PAICLI_MCP_API_KEY=your_local_key uv run paicli mcp serve --transport http --port 3000
```

HTTP 使用标准 Streamable HTTP，客户端连接 `http://127.0.0.1:3000/mcp`，
并发送 `Authorization: Bearer <PAICLI_MCP_API_KEY>`。服务校验回环 Host 和 Origin。
stdio 和 HTTP 都走统一工具策略，需要人工审批的调用会拒绝。

本地验证可使用 MCP SDK 客户端，按初始化、列工具、调用 `read_file` 的顺序执行。

Chrome DevTools MCP 会把浏览器页面和 DevTools 状态暴露给 Agent。不要随意把包含个人账号、敏感数据或生产后台的 Chrome 会话授权给 Agent。

## Web 工作台

从源码仓库启动时，先安装前端依赖，再运行统一入口。

```powershell
pnpm --dir frontend install
uv run paicli workbench --cwd .
```

命令会启动本机 Runtime 和前端，验证连接后打开浏览器。退出启动命令会停止两者。需要更换端口或手动打开浏览器时，参见 [工作台启动说明](frontend/README.md)。

## Runtime API

PaiCLI 内置轻量 Runtime API，适合外部系统接入线程、turn、事件和后台任务。

启动服务：

```bash
PAICLI_RUNTIME_API_KEY=dev-key \
uv run paicli serve --cwd .
```

Runtime 默认监听 `127.0.0.1:24158`。项目配置 `.paicli/config.json` 的 `runtime.port` 可指定固定端口，`--port` 仅用于显式覆盖。端口被占用时启动会报错并退出。前端默认使用 `127.0.0.1:24157`，由 `frontend/vite.config.ts` 配置并代理到 Runtime。

创建会话：

```bash
curl -sS -X POST http://127.0.0.1:24158/v1/sessions \
  -H 'x-api-key: dev-key'
```

发送 turn：

```bash
curl -sS -X POST http://127.0.0.1:24158/v1/sessions/<session_id>/turns \
  -H 'content-type: application/json' \
  -H 'x-api-key: dev-key' \
  -d '{"message":"总结这个项目"}'
```

读取事件：

```bash
curl -sS http://127.0.0.1:24158/v1/sessions/<session_id>/events.json \
  -H 'x-api-key: dev-key'
```

轮询接口 `GET /v1/sessions/<session_id>/events.json?cursor=<sequence>` 每页最多返回 200 条事件。
当 `has_more` 为 true 时使用 `next_cursor` 读取下一页。游标只推进到本次返回的最后一条事件。
Runtime JSON 请求体上限为 1 MiB。
通过 `POST /v1/sessions/<session_id>/turns/async` 启动回合后，轮询响应中的 `assistant_preview`
提供当前步骤已生成的文本，并附带 `turn_id` 和 `step_id`。预览独立于历史游标，步骤完成后由
历史中的 `assistant/message` 接管。

异步回合可通过 `POST /v1/sessions/<session_id>/runs/<run_id>/cancel` 请求停止。事件响应的
提交时先在 SQLite 同一个事务内接收回合并保存用户消息，再返回 `run_id`、与它相同的
`turn_id` 和提交前的 `after_sequence`。同一工作区的 CLI、SDK 和 Runtime 共享会话。
每个会话的执行者持有进程级文件锁，直到回合清理完成。Runtime 启动或继续会话时可收尾已退出
执行者留下的回合和待处理审批，把未返回的工具结果标为未知。升级前请停止旧版本的执行进程。
会话不在当前工作区时返回 `404`。事件响应中的 `active_run` 同步运行状态，支持刷新后继续管理。
已结束的异步运行只在内存中保留有限的短期状态，结果通过会话事件读取。

实时客户端可订阅 `GET /v1/sessions/<session_id>/events/stream?cursor=<sequence>`。
SSE 的 `page` 消息沿用 `events.json` 的数据结构，先重放历史，再推送新事件和文本预览。
断开后使用最后收到的 `next_cursor` 重连。
工作台使用 `view=conversation` 只接收会话界面所需的事件。游标覆盖已读取的历史，包括被筛除的诊断事件。
会话列表和事件状态包含 `title`。使用 `POST /v1/sessions/<session_id>/title` 和
`{"title":"新名称"}` 手动改名，事件流会同步 `session/title_updated`。
Runtime 接收新会话的第一条用户消息后，在后台只尝试生成一次标题。生成失败时会话仍可手动命名。
配置示例为 `{"session_title":{"model":"deepseek-flash"}}`。留空会沿用 `llm.model`，连接配置沿用
主模型。DeepSeek 标题请求固定关闭思考。可用 `session_title.enabled=false` 关闭自动生成。
该视图通过 `subagents` 同步委派任务的状态，通过 `agent/results_received` 标记内部结果投递。
已就绪的子任务结果会在工具批次结算后进入主 Agent 的下一次模型请求。
压缩失败或输出达到上限时，已有证据可作为部分结果交付。空闲子 Agent 到期会保留已完成的轮次。

`subagent` 接受单个任务，也可通过 `tasks` 一次提交 1–8 个独立任务。
同一批最多运行 4 个，剩余任务排队，到实际启动时才开始计时。
`subagents.max_active` 默认也为 4，可调整整个运行实例的并发上限。
子报告按主模型剩余的上下文预算分配空间，每份回传最多 50 KiB，完整报告可在子任务详情中查看。
自动压缩会预留模型输出空间，使用最近的模型输入用量和上下文增量估算剩余 token。

网页搜索每次默认返回 5 条、最多 20 条。会话中的 `web_fetch` 默认返回 4,000 字符以内的摘录和
`response_id`，单次内联上限为 30,000 字符。同一轮重复抓取同一 URL 会复用已保存的页面。
`web_read` 可用 `offset` / `limit` 分页，也可用 `query` 查找相关段落。每页保存最多
200,000 字符的可读文本，句柄限所属会话使用。配置子 Agent 工具白名单时，按需加入 `web_read`。

本地子 Agent 的网页证据预算按模型输入窗口和本轮剩余空间计算，默认留出四分之一输入预算作为
整轮证据上限。`subagents.research_timeout` 默认允许 240 秒内发起网络请求。
`subagents.research_max_searches`、`subagents.research_max_fetches` 和
`subagents.research_max_chars` 可选，用于设置额外上限。
工作台在主对话显示委派入口，在右侧显示子 agent 的状态和独立执行记录。打开子会话时订阅
`GET /v1/sessions/<parent_id>/subagents/<child_session_id>/events/stream?cursor=<sequence>`。
子会话流沿用独立游标，并校验子会话属于当前工作区与指定父会话。通用会话 API 以根会话为入口，
子会话通过父会话下的只读流访问，子 agent 的审批由主会话提交。
`subagent` 工具可通过可选的 `role` 为任务指定展示角色，工作台据此选用头像。
模型请求失败时，`turn/failed` 可带 `provider_error`，提供错误类型和可用的 HTTP 状态码。
`events.json` 中的 `request/attempt_failed` 按请求记录每次失败的尝试次数、阶段、耗时、
错误类型、是否重试和可用的 HTTP 状态码、上游请求 ID。重试成功时，之前的失败仍可追溯。
Plan 执行结束时提供 `plan_outcome`，包含整体状态、失败任务 ID 和因依赖跳过的任务 ID。
部分失败时会话记录 `turn/failed` 并保留已生成的结果。同步 HTTP 响应返回该结构，后台任务标为
`failed`，CLI 单次查询输出结果后以非零状态退出。

异步回合可传入 `"interactive_approval": true` 启用交互审批。通过事件取得 `approval/started`
中的 `approval_id` 后，向 `POST /v1/sessions/<session_id>/approvals/<approval_id>/decision`
提交 `{"decision":"approve"}` 或 `{"decision":"deny"}`。等待超过 5 分钟会取消请求。

通过 `GET /v1/sessions/<session_id>/permissions` 查询会话权限；向同一路径 POST
`{"mode":"auto"}` 启用完全访问，或 `{"mode":"default"}` 恢复启动策略。事件响应和 SSE
的 `permission_mode` 同步下一轮选择的模式。当前回合沿用提交时的权限。

创建并查看后台任务：

```bash
curl -sS -X POST http://127.0.0.1:24158/v1/tasks \
  -H 'content-type: application/json' \
  -H 'x-api-key: dev-key' \
  -d '{"message":"后台总结这个仓库","mode":"plan"}'

curl -sS http://127.0.0.1:24158/v1/tasks \
  -H 'x-api-key: dev-key'
```

也可以只启动队列消费者，不暴露 HTTP：

```bash
uv run paicli worker --workers 2 --cwd .
```

任务队列按项目目录隔离。Worker 使用 SQLite 原子事务领取任务，并按本次领取序号和租约有效期
续租、提交。取消已运行的任务会先显示 `canceling`，Worker 收到请求后取消协程，在清理完成后
更新为 `canceled`。已经产生的文件或外部副作用仍需按原工具语义处理。进程异常退出后，过期的
运行任务可重新领取，过期的取消请求会收尾为 `canceled`。

## 图片输入

PaiCLI 支持在 prompt 里引用图片：

```text
分析这张截图 @image:./screenshots/page.png
```

也支持绝对路径和远程图片：

```text
解释这张图 @image:/Users/me/Desktop/diagram.png
看看这个图片 @image:https://example.com/image.png
```

本地图片会自动压缩、缩放，并在需要时把透明底铺成白底，再转为 data URL。如果当前 provider/model 不支持多模态输入，PaiCLI 会自动降级为文本元信息，不会把不支持的图片 payload 发给模型。

## 快照

回合首次获准执行可能修改工作区的工具前，创建 `pre-turn` 快照。创建失败时，工具会返回错误并停止写入。已经创建前置快照的回合结束后会尽力创建 `post-turn` 快照。只读、拒绝审批和仅预演的回合无需复制工作区。恢复操作会先保存自己的 `pre-restore` 快照。

快照保存在 `~/.paicli/snapshots/v2/`，覆盖工作区中可恢复的普通文件。创建和恢复均跳过
`.env`、`.env.*`、`.git` 和 Git 指针引用的元数据目录。恢复时保留排除目录和现有链接。

创建、恢复和清理使用同一把工作区锁。恢复先检查路径并保存 `pre-restore`，随后按计划逐项
替换文件。遇到路径冲突或检测到并发更改时会报错。恢复失败时仅尝试撤销本次已应用的操作，
回滚失败时会显示 `pre-restore` 快照 ID。

REPL 中可以使用：

```text
/snapshot
/restore 1
/snapshot clean
```

## SDK

```python
from paicli.sdk import create_default_engine

engine = create_default_engine(cwd=".")
result = engine.ask_complete("解释这个项目")
print(result.text)

plan_result = engine.plan_complete("先读取 README，再总结项目结构")
```

## 开发

安装开发依赖：

```bash
uv sync --extra dev
```

运行检查：

```bash
uv run python -m ruff check .
uv run python -m ruff format --check .
uv run python -m pytest
uv build
```

测试环境隔离、链路覆盖说明和分组运行方式见 [tests/README.md](tests/README.md)。
日常快速回归可用 `uv run --extra dev python -m pytest -m "not integration and not slow"`；
默认 `pytest` 和 CI 仍运行全部测试，合并前需要完整回归。
完整覆盖率报告可用 `uv run --extra dev python -m pytest --cov --cov-report=html` 生成。

常用 smoke：

```bash
uv run paicli --version
uv run paicli --help
uv run paicli doctor --cwd .
uv run paicli --plain -p hello
```

## 和 Java / TypeScript 版本的关系

Python 版覆盖了 Java / TypeScript 版本里公开、开放协议相关的主要 Agent CLI 能力，包括 CLI、REPL、ReAct、Plan-and-Execute、Multi-Agent、Skill、SDK、工具调用、MCP、Runtime API、记忆、快照、联网工具和图片输入。

Java 版本里还有一个私有的微信 iLink 通道。Python 仓库没有内置这个私有通道，因为它依赖账号、扫码登录和协议凭证，不应该用假实现冒充。

更详细的实现对齐情况见 [docs/parity.md](docs/parity.md)。

## License

MIT. See [LICENSE](LICENSE).
