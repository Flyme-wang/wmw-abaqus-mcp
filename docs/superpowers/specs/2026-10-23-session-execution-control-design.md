# CAE 会话执行控制设计

用户已确认四项设计：单会话单执行、GUI 短回调、请求状态追踪、提交与监测分离。本次按该确认实施，不改模型、物理参数或正在运行的会话。

## 范围与隔离

在独立仓库副本及 codex/session-execution-control 分支开发。原在用工作目录与 Codex 插件缓存不改，不热加载、不重启 CAE，不终止任何求解、父进程或调度 allocation。版本按提交工具行为变化升级为本地 6.0.0 候选版；无真实 CAE 验证时不得宣称 GUI 卡顿已经实测消除。

## 设计选择

采用桥接进程统一维护的请求注册表。仅在 MCP 客户端加锁无法覆盖多个客户端；把 Abaqus API 移到任意 Python 后台线程缺少线程安全依据。网络线程负责准入、状态查询与等待响应，GUI 线程只派发一个内核请求及短轮询结果。

每个会话只准入一个 queued/running/timeout_pending 请求，其他不同请求立即 busy。同一请求编号及相同内容复用原请求，不重复执行；同编号不同内容拒绝。queued 超过截止时间变 expired，禁止派发；已派发请求客户端超时后保持 timeout_pending 和独占占位，直到真实完成回执。通信超时、读取失败或派发异常不能自动释放执行槽，也不能自动重发。状态查询是旁路操作，不需要进入内核队列。

GUI 定时器使用一个 dispatcher 单例和已调度标记。一次回调只做一次派发或一次响应检查，不循环 sleep，不连续执行多条。派发前建立 active 标记，抵御原生 sendCommand 内部事件重入。保持 API 的 GUI/kernel 线程归属；sendCommand 自身的原生耗时不能凭离线测试保证消除。

结果先写同目录临时文件，再 os.replace 发布最终 JSON。暂时读取失败保留执行状态及诊断，不释放槽。请求历史限制结果数量/内存，活动请求不被回收；若查询历史已回收明确返回 unknown，客户端不能据此自动重放。桥接 stop/start 不丢失正在执行请求；未派发的旧请求作废，不在重启后执行。

## 协议与客户端

原 execute 请求保留 id，增加可选稳定 request_id（默认 UUID）。协议增加 request_status 请求，返回 request_id/state/started/completed/outcome_pending 及完成结果。ping/桥接健康信息不因繁忙再注入内核请求，明确区分 transport connected 与 kernel ready。

MCP run_python、execute_script、submit_job 暴露可选 request_id；新增 get_request_status 工具。等待超时时只查原请求状态，若已完成返回原结果，否则报告带编号的待核实状态，不发送第二次 execute。busy 明确给出活动请求编号。端点在请求发起时固定，恢复查询使用同一端点。

submit_job 仅提交，立即报告 submitted 和实际当前 status，不调用 waitForCompletion，不将 submitted 解释为科学完成。monitor_job_status 继续独立查询状态，具名监测返回真实 job.status，读取错误明确列出。记录提交时的绝对工作目录供后续监测使用，不自动终止失败作业。

## 验证与交付

先写能在旧代码失败的回归测试。使用真实注册表、伪 Abaqus 边界及仅监听系统分配临时端口的离线集成测试；禁止连接 48152–48164 等现有桥接端点。覆盖并发准入、重入、过期、超时仍忙、同编号去重、完成后释放、旁路状态、单次 GUI 回调、提交返回及监控。保留原路由测试。复核打包、stdio 工具发现及中英文文档。先生成隔离源码和可审查补丁/安装包；用户追加授权后同步 GitHub 源码，未创建正式 Release，不改变现有会话。
