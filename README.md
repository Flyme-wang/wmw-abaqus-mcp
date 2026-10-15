# wmw-abaqus-mcp

**v5.2.0 · Abaqus/CAE 多端口、多窗口 MCP 插件**

[English](README.en.md) · [版本记录](CHANGELOG.md) · [验证范围](docs/validation.md) · [本地排版版](docs/site/index.html)

把 MCP 客户端连接到正在运行的 **Abaqus/CAE**。每个 CAE 窗口使用独立 TCP 端口，默认范围为 **48152–48162**；一个 MCP server 可发现多个窗口、切换活动会话，并通过 **`session_id`** 定向执行。

> [!IMPORTANT]
> 本仓库来自当前使用的 **v5.2.0** 源码，名称统一为 **wmw-abaqus-mcp**。保留原工具名、`ABAQUS_MCP_*` 环境变量和通信协议。本次同步不改变建模或求解逻辑。

## 功能

- **自动端口**：每个窗口选择范围内首个可用端口。
- **指定端口**：菜单中直接选择端口；被占用时报告错误。
- **多会话路由**：发现、列出、切换会话，模型操作支持 `session_id`。
- **CAE 交互**：执行 Python、查看模型、管理已有作业、读取 ODB 元数据、抓取视口图像。
- **本地桥接**：外部 MCP server 通过 stdio 提供工具；GUI 线程调度，再经 `sendCommand` 进入 Abaqus kernel。

每个窗口内的请求受该 CAE GUI/kernel 状态约束；多端口不代表同一窗口可并行执行多个内核脚本。

## 安装

需要可用的 **Abaqus/CAE 许可证**和外部 **Python 3.10+**。无需在 CAE Python 中安装 MCP。兼容性范围见[验证记录](docs/validation.md)。

```powershell
git clone https://github.com/Flyme-wang/wmw-abaqus-mcp.git
cd wmw-abaqus-mcp
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

在仓库根目录设置用户环境和菜单加载器。**若已有同名目录，先备份原目录，再合并 loader 文件。** 保留已有 `abaqus_v6.env`。

```powershell
$repoPath = (Get-Location).Path
[Environment]::SetEnvironmentVariable('ABAQUS_MCP_HOME', $repoPath, 'User')
$env:ABAQUS_MCP_HOME = $repoPath
$loaderPath = Join-Path $env:USERPROFILE 'abaqus_plugins\mcp_control'
New-Item -ItemType Directory -Path $loaderPath -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $repoPath 'abaqus_plugins\mcp_control\__init__.py') -Destination $loaderPath
Copy-Item -LiteralPath (Join-Path $repoPath 'abaqus_plugins\mcp_control\mcp_control_plugin.py') -Destination $loaderPath
```

从继承该环境变量的新终端启动 CAE；正常使用请重启 CAE。菜单为：

```text
Plug-ins > wmw-abaqus-mcp > Start Socket Bridge > Auto Port
Plug-ins > wmw-abaqus-mcp > Start Socket Bridge > Port 48152
...
Plug-ins > wmw-abaqus-mcp > Start Socket Bridge > Port 48162
Plug-ins > wmw-abaqus-mcp > Bridge Status
Plug-ins > wmw-abaqus-mcp > Stop Socket Bridge
```

也可参考 [`abaqus_v6.env.example`](abaqus_v6.env.example) 合并启动钩子；**加载器和启动钩子二选一即可**，保留已有设置。

## MCP 客户端

参考 [`examples/mcp_config.example.json`](examples/mcp_config.example.json)，替换 `C:\path\to\wmw-abaqus-mcp` 为仓库绝对路径。`ABAQUS_MCP_HOME` 必须在 **CAE 进程**启动前设置；只写在 MCP 客户端环境中不会传入已打开的 CAE。

[`.env.example`](.env.example) 仅是变量说明，程序不会自动加载 `.env`。两个进程使用相同 host 和端口范围。修改 `ABAQUS_MCP_PORT_RANGE_START` / `ABAQUS_MCP_PORT_RANGE_END` 后重启对应进程。可选 `ABAQUS_MCP_AUTOSTART=1` 按 `ABAQUS_MCP_PORT` 固定端口启动，**不会自动选择空闲端口**。

## 两个窗口的连接流程

1. 窗口 A 选择 **Port 48152**，窗口 B 选择 **Port 48153**；或分别使用 **Auto Port**。
2. 客户端调用 **`discover_sessions()`**，逐端口 TCP ping 并登记可达会话。
3. 用 **`list_sessions()`** 获取 `session_id`、端口及活动标记，默认示例为 `session_48152`、`session_48153`。
4. 用 **`set_active_session(session_id="session_48153")`** 切换后续操作，或在工具中直接传入 `session_id`。
5. 建模前核对目标会话、模型和工作目录。

下面是客户端发送的 **MCP 工具名与参数**，不是 Abaqus 命令区脚本：

```json
{"name":"discover_sessions","arguments":{}}
{"name":"list_sessions","arguments":{}}
{"name":"ping","arguments":{"session_id":"session_48153"}}
{"name":"run_python","arguments":{"code":"list(mdb.models.keys())","session_id":"session_48153"}}
{"name":"set_active_session","arguments":{"session_id":"session_48153"}}
```

**未登记活动会话时，工具回退到 `ABAQUS_MCP_PORT`，默认 48152。** 选择 Port 48154 后直接调用无参数 `ping`，不保证连接该窗口。`list_sessions` 只列登记缓存，不扫描或自动清理失效项。

## 工具与资源

**会话工具**：`discover_sessions`、`list_sessions`、`set_active_session`。

**CAE 工具**：`ping`、`check_abaqus_connection`、`run_python`、`execute_script`、`set_workdir`、`get_model_info`、`list_jobs`、`submit_job`、`monitor_job_status`、`inspect_odb`、`get_odb_info`、`capture_viewport`、`get_viewport_image`。以上工具均支持可选 `session_id`。

| 资源 | 当前行为 |
|---|---|
| `abaqus://sessions` | 返回已登记会话 |
| `abaqus://status` | 查询默认 host/port |
| `abaqus://session-telemetry` | 查询默认 host/port，不跟随活动会话 |
| `abaqus://agent-instructions` | 返回操作指南 |

查询特定窗口请用 **`ping(session_id=...)`**。插件在系统临时目录写发现文件；当前 server 通过端口扫描发现窗口，不读取这些文件。

停止脚本按环境变量指定端口连接，**不会停止所有会话**：

```powershell
$env:ABAQUS_MCP_PORT = '48153'
.\.venv\Scripts\python.exe .\stop_mcp.py
```

> [!NOTE]
> 桥接允许执行本机 Abaqus Python，请保留默认 **127.0.0.1**，供可信本地客户端使用。协议没有网络认证或 TLS。发布包不包含许可证、模型、ODB、日志、虚拟环境及本机配置。

## 验证与排版

隔离的多端口路由检查：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

范围与局限见 [docs/validation.md](docs/validation.md)。源码指纹及品牌调整见 [docs/source-manifest.json](docs/source-manifest.json)。

GitHub Markdown 字体由平台控制。本地 HTML 使用 **中文 Microsoft YaHei、英文/数字 Times New Roman**；重点用粗体与蓝色，适用范围用棕色。下载仓库后打开 **[docs/site/index.html](docs/site/index.html)**。重建见[排版规则](docs/publication_style.md)。

## 许可与来源

[MIT License](LICENSE)。独立仓库提取自 [CAE-Agent-Hub](https://github.com/Cai-aa/CAE-Agent-Hub) 的 `MCP/Abaqus`，保留上游归属。GUI socket 架构参考 [Whfkl/Abaqus-Control-MCP](https://github.com/Whfkl/Abaqus-Control-MCP)，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。仓库不包含 Abaqus 二进制或许可证。
