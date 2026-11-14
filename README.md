<p align="center">
  <img src="frontend/public/libi-icons/primary.png" width="96" height="96" alt="LiBi 的紫色猫咪图标">
</p>

<h1 align="center">LiBi</h1>

<p align="center"><strong>在本地工作区运行的 AI Agent</strong></p>

<p align="center">
  在终端或网页工作台和 LiBi 对话，让它读写代码、运行命令、联网查资料。<br>
  也可以通过 Python SDK 或 Runtime HTTP API 发起任务。
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#终端与工作台">终端与工作台</a> ·
  <a href="#程序接入">程序接入</a> ·
  <a href="#开发">开发</a>
</p>

<p align="center"><code>Python 3.11+</code> · <a href="LICENSE">MIT</a></p>

---

## 快速开始

准备好 Python 3.11+ 和 `uv` 后，在仓库根目录安装依赖并检查环境。

```powershell
uv sync --locked
uv run libi doctor --cwd .
```

用内置的 DeepSeek 连接发起第一条请求。

```powershell
$env:DEEPSEEK_API_KEY = '<你的 DeepSeek API Key>'
uv run libi -p "读一下这个项目，说明怎么运行测试"
```

### 模型与项目配置

也可以在项目的 `.env` 中设置 `DEEPSEEK_API_KEY`。连接其他模型时，可通过 `LIBI_PROVIDER`、`LIBI_MODEL`、`LIBI_BASE_URL` 和 `LIBI_API_KEY` 指定连接。

用户级配置位于 `~/.libi/config.json`，项目级配置位于 `.libi/config.json`。要加载项目配置和 `.env` 中受保护的连接、权限等设置，在启动环境中显式设置 `LIBI_TRUST_PROJECT_CONFIG=1`。项目级 MCP 另用 `LIBI_TRUST_PROJECT_MCP=1` 授权。

## 终端与工作台

### 终端

| 操作 | 命令 |
| --- | --- |
| 打开交互终端 | `uv run libi` |
| 执行一次请求 | `uv run libi -p "检查测试失败的原因"` |
| 先规划再执行 | `uv run libi --mode plan -p "梳理并验证模块依赖"` |
| 交互选择最近会话 | `uv run libi --resume` |
| 继续指定会话 | `uv run libi --session-id <会话 ID>` |

终端输入 `/help` 查看交互命令，`/usage` 查看最近一轮用量，`/context` 查看会话 ID 和上下文状态。

### 网页工作台

工作台需要 Node.js 22.12+ 和 pnpm 10。首次启动前，在仓库根目录安装前端依赖，再启动工作台。

```powershell
pnpm --dir frontend install --frozen-lockfile
uv run libi workbench --cwd .
```

启动器会同时运行前端和 Runtime，生成两者共用的临时凭据。工作台的 `/v1/health` 返回 HTTP 200 后，浏览器会打开默认地址 [http://127.0.0.1:24157](http://127.0.0.1:24157)。

在左下角打开「设置 → 模型」，填写 OpenAI 兼容服务的 Base URL 和 API Key，测试连接并选择服务返回的模型 ID。保存后从下一轮对话生效。

对话输入框支持粘贴截图、拖放和选择图片或 UTF-8 文本附件。点击输入框或消息里的图片附件，可在当前会话中预览大图。带附件提问时，LiBi 会优先把「这里」「这个」理解为同一条消息中的附件。发送图片需要启用支持视觉的模型，可在「设置 → 模型」调整图片输入能力。

重启时先停止原进程，再按原工作目录和端口启动。可加 `--no-browser` 跳过自动打开浏览器。更多设置见[工作台说明](frontend/README.md)。

## 任务如何运行

- **执行模式** 默认使用 ReAct，按需调用工具并处理结果。Plan 模式先生成计划，再按任务依赖执行。
- **会话与进度** 在工作台切换会话，查看子 Agent 进度和独立执行记录，以及每轮 Token 用量和缓存命中率。桌面布局支持拖动分隔线，栏宽保存在当前浏览器中。
- **权限与审批** 默认按策略限制路径、命令和写入。工作台可按回合处理工具审批，会话中的完全访问模式需要显式选择。
- **记忆与恢复** 会话历史、长期记忆、审计与快照分开保存。长会话可以压缩上下文，文件快照可用于恢复工作区。
- **MCP 连接器** 工作台启动时检测已启用的连接器，可在卡片上启停自定义 MCP，查看连接结果和工具列表。运行 `uv run libi mcp serve` 可向其他 MCP 客户端提供工具。
- **联网调研** 联网工具可保存网页证据，子 Agent 调研结果中的引用可在工作台查看。

## 程序接入

### Python SDK

用 Python SDK 发起请求，结果包含模型回复及用量。

```python
from libi.sdk import create_default_engine

engine = create_default_engine(".")
result = engine.ask_complete("列出项目的主要入口")
print(result.text)
```

### Runtime HTTP API

需要固定 HTTP 地址时，单独启动 Runtime。

```powershell
$env:LIBI_RUNTIME_API_KEY = '<本机 Runtime 凭据>'
uv run libi serve --cwd .
```

独立 Runtime 默认监听 `127.0.0.1:24158`，客户端通过 `x-api-key` 鉴权。

| 操作 | HTTP 入口 |
| --- | --- |
| 检查健康状态 | `GET /v1/health` |
| 创建会话 | `POST /v1/sessions` |
| 发送一轮请求 | `POST /v1/sessions/{id}/turns` |
| 异步发送一轮请求 | `POST /v1/sessions/{id}/turns/async` |
| 订阅会话事件 | `GET /v1/sessions/{id}/events/stream` |
| 提交队列任务 | `POST /v1/tasks` |

只处理队列任务时，可运行 `uv run libi worker --cwd .`。

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
