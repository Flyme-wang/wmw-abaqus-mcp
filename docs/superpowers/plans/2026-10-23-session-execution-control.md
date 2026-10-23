# CAE 会话执行控制实施计划

> **For agentic workers:** 使用 subagent-driven-development 按任务实施，并分别核对需求与代码质量。

**Goal:** 实现用户已确认的四项执行控制改进，保持现有计算不受干预。
**Architecture:** 标准库注册表统一准入；GUI 单请求派发与结果短轮询；网络旁路状态查询；MCP 在原端点恢复查询；作业提交和监测分离。
**Tech Stack:** Python 3.10+、Abaqus/CAE GUI、socketserver、MCP SDK、unittest。
**Baseline:** 52c3ad5，原有 8 项测试通过。隔离仓库 D:/Program Files/codex/try1/wmw-abaqus-mcp。

## Task 1：核心执行控制与回归测试

文件：新增 bridge_runtime.py、tests/test_execution_control.py、tests/test_gui_dispatch.py、tests/test_request_recovery.py；修改 abaqus_mcp_plugin.py、mcp_server.py、tests/test_multi_session.py（仅兼容新协议所需）。

- [x] 先写注册表行为测试：第二请求 busy、同编号复用、未派发超时不执行、已派发超时保持占位、完成才释放。
  测试示例：`first = registry.admit("one", "execute", {"code": "pass"}, 1)`；`registry.admit("two", "execute", {"code": "pass"}, 1)` 必须返回 busy。
- [x] 运行 `python -B -m unittest discover -s tests -v`，记录旧实现的预期失败。
- [x] 实现可独立导入的标准库注册表与结构化状态；活动请求不被历史回收，重复请求内容冲突拒绝。
- [x] GUI callback 使用 `dispatch_once()` / `collect_once()` 的单次逻辑，不包含等待循环；同一 tick 和事件重入不派发第二个请求。原子写响应、单个定时器、保留正在执行的状态。
- [x] 网络 handler 在 GUI 外完成准入、busy、request_status 和心跳；接收与发送期限有界，繁忙时不累积执行队列。
- [x] MCP 提供 `get_request_status(request_id, session_id=None)`；超时只查询，绝不重发 execute；run_python/execute_script/submit_job 可指定稳定 request_id，查询固定端点。
- [x] submit_job 移除 waitForCompletion，报告 submitted 和当前 status；监控独立返回 job.status 与目录/读取错误。
- [x] 原有和新增隔离测试全部通过；真实 CAE API 仅在 GUI 原生派发路径调用。

## Task 2：版本、文档与可迁移包

文件：VERSION、pyproject.toml、CHANGELOG.md、README.md、README.en.md、docs/validation.md、docs/source-manifest.json、docs/site/*；补充离线验收/安装说明。

- [x] 按接口行为变化更新本地版本 6.0.0，明确尚未发行与真实 CAE 未验收。
- [x] 记录请求状态、busy、超时查询、提交语义变化、历史保留边界及原生 sendCommand 限制。
- [x] setuptools py-modules 包含新运行时模块；保留原始来源指纹，新增本次变更指纹。
- [x] 重建既有 HTML，中文 Microsoft YaHei，英文/数字 Times New Roman；预览并检查链接与裁切。
- [x] 验证 stdio initialize/tools/list/resources/list，不调用现有 CAE；构建 wheel/源码包并记录 SHA256。
- [x] 先独立需求复核，再代码质量复核；解决发现的问题。
- [x] 汇报源码路径、测试结果、包路径与尚未部署的事实。安装不得热改当前会话或自动重启任何进程。
