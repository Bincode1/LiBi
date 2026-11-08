# LiBi 工作台

工作台使用 React、TypeScript 和 Vite，通过同源代理访问本机 Runtime。运行需要 Node.js 22.12+ 和 pnpm 10。

## 启动和重启

在仓库根目录运行以下命令。

```powershell
pnpm --dir frontend install --frozen-lockfile
uv run libi workbench --cwd .
```

默认地址为 [http://127.0.0.1:24157](http://127.0.0.1:24157)。启动器为 Runtime 分配本机端口，自动给 Vite 配置代理与临时凭据。看到 `LiBi workbench ready` 或通过工作台访问 `/v1/health` 得到 HTTP 200，表示代理已连通。

在启动终端按 Ctrl+C 停止前端和 Runtime。重启时按原工作目录和端口再运行同一命令。可用 `--port` 更换工作台端口，用 `--no-browser` 跳过打开浏览器。

## 配置对话模型

打开「设置 → 模型」，填写 OpenAI 兼容服务的 API 根地址（例如 `https://api.example.com/v1`）和 API Key，然后点击「测试并获取模型」。测试只请求该地址下的 `/models`。如果服务未提供列表，仍可手动填写模型 ID。

点击「保存模型配置」后，工作台的下一轮对话会使用该设置。当前用户的工作台配置保存在 `~/.libi/workbench-model.json`，其中包括 API Key。页面只显示是否已配置，地址相同时留空可保留原密钥。首次保存前，工作台沿用启动时加载的配置。终端和独立 Runtime 使用各自原有的配置来源。

## 发送对话附件

在对话输入框点击「＋」、粘贴图片或拖放文件。发送前可以移除附件，也可以只发附件。支持 PNG、JPEG、WebP 及 UTF-8 文本，例如日志、Markdown、CSV、JSON 和常见代码文件。每条消息最多 5 个附件，单个最多 10 MiB，合计最多 20 MiB。

图片作为视觉输入随请求发送，文本以带行号的片段提供，较长文件可由 Agent 分段读取。ReAct 和 Plan 都支持这一流程。自定义模型可以在「设置 → 模型」明确启用图片输入。已发送附件保存在本机用户目录的 `.libi/runtime/attachments/`，可从会话中重新打开。

## 管理 MCP 服务

工作台启动时会在后台检测已启用的 MCP 服务，包括受信任的项目连接器。MCP 服务页会自动更新检测状态。用自定义 MCP 卡片右侧的开关可以启动或关闭服务，启停从下一轮对话生效。启动后会自动检测连接，卡片的更多菜单也提供手动测试。

## 调整分栏

在桌面布局中拖动会话栏或工作详情旁的分隔线可调整宽度。也可以按 Tab 聚焦分隔线，再用左右方向键调整。刷新页面或重新打开工作详情时会恢复上次保存的栏宽。窗口变窄时，侧栏按可用空间切换为抽屉。

## 手动联调

需要固定 Runtime 端口或单独调试前端代理时，可以分别启动。

1. 在项目根目录给启动进程设置 `LIBI_RUNTIME_API_KEY`，运行 `uv run libi serve --cwd .`。默认端口为 `24158`。
2. 将相同的值通过 `LIBI_WEB_RUNTIME_KEY` 提供给 Vite。可以用前端进程环境，也可以用 `frontend/.env.local`。
3. 运行 `pnpm --dir frontend dev`。如需指定其他本机 Runtime 地址，设置 `LIBI_WEB_RUNTIME_URL`。

凭据由 Vite 服务端添加到代理请求。代理限定本机 HTTP 地址，并拒绝跨源浏览器请求。`pnpm preview` 用于预览构建产物，代理只在开发服务中可用。

## 验证

```powershell
pnpm --dir frontend test
pnpm --dir frontend build
```

浏览器中的连接状态由 Runtime 健康检查决定。会话和子 Agent 的执行记录通过 SSE 同步，断开后可从最后一个游标继续。
