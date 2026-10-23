# wmw-abaqus-mcp

**v6.0.0 source candidate · Per-session Abaqus/CAE execution control**

[中文](README.md) · [Changelog](CHANGELOG.md) · [Validation](docs/validation.md) · [Local HTML](docs/site/english.html)

Connect an MCP client to one or more live **Abaqus/CAE** windows. Each window binds its own TCP port, normally **48152–48162**. One external MCP server discovers these bridges and routes tools to an active session or an explicit **`session_id`**.

This source candidate changes request lifecycle handling and job-submission semantics. **Only the source is being synchronized; no formal Release has been published, and live CAE integration validation remains outstanding.** Upgrade both the bridge and external MCP server together. Existing CAE sessions are not hot-reloaded or restarted. `submit_job` now returns after submission; use separate monitoring to establish completion.

## Execution control

Each CAE bridge admits one queued/running/outcome-pending operation. Other operation IDs receive `BUSY`. Reusing an ID with identical content refers to the original operation; conflicting content is rejected. GUI ticks dispatch once or check a result once, without a sleep loop. Abaqus APIs remain on their native GUI/kernel execution path.

`run_python`, `execute_script`, and `submit_job` accept an optional stable `request_id`. Use **`get_request_status(request_id, session_id=...)`** after a timeout. States are `queued`, `running`, `timeout_pending`, `completed`, `failed`, `expired`, and `unknown`. An expired queued operation is never dispatched. A dispatched operation remains reserved until its actual outcome is known; a timeout does not stop it or authorize replay. Unknown/evicted history is not proof that execution did not occur.

`submit_job` reports submission and current status, without `waitForCompletion`. Query `monitor_job_status` separately. A completed request is a Python-operation receipt, not a completed solver analysis. Bridge status is process-local; closing CAE loses it. Terminal results are limited to 128 entries and 16 MiB; de-duplication retains 65536 ID hashes by default, configurable at initialization through `ABAQUS_MCP_ID_HISTORY_LIMIT`. Evicted IDs return `HISTORY_EVICTED` and cannot be replayed. Capacity exhaustion returns `HISTORY_CAPACITY`, without resetting history or restarting CAE. `ping` reports transport and busy state, while model metadata is obtained with `get_model_info`.

**Native boundary:** this removes plugin-owned polling waits. The native `sendCommand`, model regeneration, and display operations may still occupy CAE. No supported asynchronous native dispatch or arbitrary worker-thread Abaqus API safety is assumed.

Submission directories retain up to 128 weak-reference identity records. Monitoring reuses one only when both MDB and job objects match. Unsupported weak references, replaced objects, or missing records produce an explicit `current_directory_fallback` and reason. Actual Abaqus wrapper identity/weak-reference behavior has not been validated; verify the returned absolute directory whenever a fallback is reported.

## Install

Use an external **Python 3.10+** interpreter and a licensed Abaqus/CAE installation. Do not install the MCP SDK into Abaqus Python. See the [validation scope](docs/validation.md) before assuming compatibility with another CAE version.

Clone the repository into a separate directory and install from its root. This is a source candidate; see the [upgrade and acceptance steps](docs/upgrade.md).

```powershell
git clone https://github.com/Flyme-wang/wmw-abaqus-mcp.git
cd wmw-abaqus-mcp
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

From the repository root, set the GUI bridge location and install the loader. Back up an existing loader before merging these files.

```powershell
$repoPath = (Get-Location).Path
[Environment]::SetEnvironmentVariable('ABAQUS_MCP_HOME', $repoPath, 'User')
$env:ABAQUS_MCP_HOME = $repoPath
$loaderPath = Join-Path $env:USERPROFILE 'abaqus_plugins\mcp_control'
New-Item -ItemType Directory -Path $loaderPath -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $repoPath 'abaqus_plugins\mcp_control\__init__.py') -Destination $loaderPath
Copy-Item -LiteralPath (Join-Path $repoPath 'abaqus_plugins\mcp_control\mcp_control_plugin.py') -Destination $loaderPath
```

Start a new CAE process from a terminal that inherits this variable. The menu is **Plug-ins > wmw-abaqus-mcp**. Choose **Start Socket Bridge > Auto Port** or **Port 48152** through **Port 48162**. An occupied explicit port reports an error instead of falling back to another port.

As an alternative to the loader, merge the hook from [`abaqus_v6.env.example`](abaqus_v6.env.example) into your existing environment file; preserve existing startup configuration. One loading method is sufficient.

## Configure the MCP client

Use [`examples/mcp_config.example.json`](examples/mcp_config.example.json) and replace `C:\path\to\wmw-abaqus-mcp` with your absolute repository path. `ABAQUS_MCP_HOME` must reach the **CAE process**; configuring it only for the external client cannot update an already running CAE process.

[`.env.example`](.env.example) documents the variables; the programs do **not** load `.env` automatically. Set the host and port range consistently for both processes. Restart them after changing the range. Optional `ABAQUS_MCP_AUTOSTART=1` uses the fixed `ABAQUS_MCP_PORT`, rather than selecting a free port.

## Connect two windows

1. Start window A on **Port 48152** and B on **Port 48153**, or use Auto Port in each.
2. Call **`discover_sessions()`** from your MCP client. It probes the configured port range with TCP ping.
3. Call **`list_sessions()`** to inspect registered IDs, endpoints and the active flag.
4. Use **`set_active_session(session_id="session_48153")`**, or pass the ID directly to a tool.
5. Check the target model and working directory before modifying a model or submitting a job.

Example **MCP tool requests**, not Abaqus console scripts:

```json
{"name":"discover_sessions","arguments":{}}
{"name":"list_sessions","arguments":{}}
{"name":"ping","arguments":{"session_id":"session_48153"}}
{"name":"run_python","arguments":{"code":"list(mdb.models.keys())","session_id":"session_48153"}}
```

Without a registered active session, tools fall back to `ABAQUS_MCP_PORT`, normally **48152**. Selecting a different GUI port alone does not change that fallback. `list_sessions` returns cached registrations; it neither scans nor removes stale entries. The bridge writes temporary discovery files, but the current server discovers windows by port probing instead of reading those files.

## Tools and resources

Session tools: **`discover_sessions`**, **`list_sessions`**, **`set_active_session`**.

CAE tools, each accepting an optional `session_id`: `ping`, `check_abaqus_connection`, `run_python`, `execute_script`, `set_workdir`, `get_model_info`, `list_jobs`, `submit_job`, `monitor_job_status`, `inspect_odb`, `get_odb_info`, `capture_viewport`, `get_viewport_image`.

| Resource | Behavior |
|---|---|
| `abaqus://sessions` | Registered sessions |
| `abaqus://status` | Default host/port |
| `abaqus://session-telemetry` | Default host/port; does not follow active session |
| `abaqus://agent-instructions` | Operation guidance |

Use **`ping(session_id=...)`** for a specific window's telemetry. Requests within a CAE window remain subject to its GUI/kernel execution state.

To stop one bridge, set its port explicitly:

```powershell
$env:ABAQUS_MCP_PORT = '48153'
.\.venv\Scripts\python.exe .\stop_mcp.py
```

The stop script does not stop all sessions. Keep the default **127.0.0.1** for trusted local use: the bridge executes Python and provides no authentication or TLS.

## Validation, source and presentation

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests use isolated fake endpoints. They do not establish CAE integration or solver correctness. See [validation](docs/validation.md) and [source fingerprints](docs/source-manifest.json).

GitHub controls Markdown fonts. The downloadable local [HTML edition](docs/site/english.html) uses **Microsoft YaHei for Chinese** and **Times New Roman for English/numbers**. See the [publication policy](docs/publication_style.md) to rebuild it.

## License

[MIT](LICENSE). Extracted from the local `MCP/Abaqus` implementation of [CAE-Agent-Hub](https://github.com/Cai-aa/CAE-Agent-Hub). The GUI socket architecture references [Whfkl/Abaqus-Control-MCP](https://github.com/Whfkl/Abaqus-Control-MCP); attribution is preserved in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). No Abaqus binaries, license files, models, ODBs, runtime logs, virtual environments or private configuration are included.
