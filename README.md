# LiBi

LiBi 是一个在本地工作区运行的 AI Agent。可以在终端或网页工作台中对话，让它读写代码、运行命令、联网查资料，也可以通过 Python SDK 或 Runtime HTTP API 发起任务。

## 安装与配置

需要 Python 3.11+ 和 uv。在仓库根目录安装依赖。

```powershell
uv sync --locked
uv run libi doctor --cwd .
```

以下示例使用内置的 DeepSeek 连接。

```powershell
$env:DEEPSEEK_API_KEY = '<你的 DeepSeek API Key>'
uv run libi -p "读一下这个项目，说明怎么运行测试"
```

也可以在项目的 `.env` 中设置 `DEEPSEEK_API_KEY`，供内置 DeepSeek 端点使用。配置其他模型时，可通过 `LIBI_PROVIDER`、`LIBI_MODEL`、`LIBI_BASE_URL` 和 `LIBI_API_KEY` 指定连接。

用户级配置位于 `~/.libi/config.json`，项目级配置位于 `.libi/config.json`。要加载项目配置和 `.env` 中受保护的连接、权限等设置，在启动环境中显式设置 `LIBI_TRUST_PROJECT_CONFIG=1`。项目级 MCP 另用 `LIBI_TRUST_PROJECT_MCP=1` 授权。

## 终端与工作台

| 操作 | 命令 |
| --- | --- |
| 打开交互终端 | `uv run libi` |
| 执行一次请求 | `uv run libi -p "检查测试失败的原因"` |
| 先规划再执行 | `uv run libi --mode plan -p "梳理并验证模块依赖"` |
| 交互选择最近会话 | `uv run libi --resume` |
| 继续指定会话 | `uv run libi --session-id <会话 ID>` |

终端输入 `/help` 查看交互命令，`/usage` 查看最近一轮用量，`/context` 查看会话 ID 和上下文状态。

网页工作台需要 Node.js 22.12+ 和 pnpm 10。首次安装前端依赖后，从仓库根目录启动。

```powershell
pnpm --dir frontend install --frozen-lockfile
uv run libi workbench --cwd .
```

工作台默认打开 [http://127.0.0.1:24157](http://127.0.0.1:24157)。启动命令为前端和 Runtime 配置本机连接及临时凭据，并在代理健康检查通过后打开浏览器。重启时停止原命令，再按相同参数启动。后台启动可加 `--no-browser`。见[工作台说明](frontend/README.md)。

在工作台左下角打开「设置 → 模型」，可填写 OpenAI 兼容服务的 Base URL 和 API Key，测试连接并选择服务返回的模型 ID。保存后从下一轮对话生效。

对话输入框支持粘贴截图、拖放和选择图片或 UTF-8 文本附件。带附件提问时，LiBi 会优先把「这里」「这个」理解为同一条消息中的附件。图片需要启用支持视觉的模型，可在「设置 → 模型」调整图片输入能力。

## 任务如何运行

- 默认使用 ReAct，按需要调用工具并处理结果。Plan 模式先生成任务计划，再按依赖执行。
- 工作台支持会话切换、工具审批、子 Agent 进度和独立执行记录，以及每轮 Token 用量和缓存命中率。
- 桌面工作台可拖动分隔线调整会话栏和工作详情栏，栏宽保存在当前浏览器中。
- 默认权限模式按策略限制路径、命令和写入，工作台可按回合处理审批。会话中的完全访问模式需要显式选择。
- 会话历史、长期记忆、审计与快照分开保存。长会话可以压缩上下文，文件快照用于恢复工作区。
- 工作台启动后会自动检测已启用的 MCP 连接器。可在连接器卡片上启动或关闭自定义 MCP，查看启用状态、连接结果和工具列表。工具服务可通过 `uv run libi mcp serve` 向其他 MCP 客户端提供。
- 联网工具可保存网页证据，带来源的子 Agent 调研结果可在工作台查看引用。

## 程序接入

用 Python SDK 发起请求，结果包含模型回复及用量。

```python
from libi.sdk import create_default_engine

engine = create_default_engine(".")
result = engine.ask_complete("列出项目的主要入口")
print(result.text)
```

需要固定 HTTP 地址时，用独立 Runtime。

```powershell
$env:LIBI_RUNTIME_API_KEY = '<本机 Runtime 凭据>'
uv run libi serve --cwd .
```

独立 Runtime 默认监听 `127.0.0.1:24158`。客户端通过 `x-api-key` 鉴权。主要入口包括 `GET /v1/health`、`POST /v1/sessions`、`POST /v1/sessions/{id}/turns`、`POST /v1/sessions/{id}/turns/async`、`GET /v1/sessions/{id}/events/stream` 和 `POST /v1/tasks`。只处理队列任务时可运行 `uv run libi worker --cwd .`。

## 数据与升级

用户级数据位于 `~/.libi/`，包括会话数据库、MCP 连接、记忆与快照。项目级配置和技能位于各工作区的 `.libi/`。从旧版迁移时，先停止所有旧进程，再将用户级、项目级数据目录分别迁至这两个位置，并把应用专用环境变量的前缀改为 `LIBI_`。保留工作区的原绝对路径，已有会话就能按原工作区继续访问。

## 开发

Python 包在 `src/libi/`，网页工作台在 `frontend/`，测试在 `tests/`。工程约定见 [LIBI.md](LIBI.md)，测试分层见[测试说明](tests/README.md)。

```powershell
uv sync --locked --extra dev
uv run --extra dev ruff check .
uv run --extra dev ruff format --check .
uv run --extra dev python -m pytest
uv build
pnpm --dir frontend test
pnpm --dir frontend build
```

[MIT 许可证](LICENSE)
