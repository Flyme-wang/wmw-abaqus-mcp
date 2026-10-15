# v5.2.0 验证记录

**日期：2026-10-15；范围：独立公开发布副本。**

原在用桥接的版本常量为 **5.2.0**。本次同步保留通信及求解逻辑，只统一项目标识、GUI 菜单和包版本，更新可移植配置、文档及验证材料。

## 检查项目

| 项目 | 实际结果 |
|---|---|
| Python 语法、JSON、TOML | 通过；版本元数据为 5.2.0，TOML 无 BOM |
| 多端口与会话路由 | **8 项 unittest 通过**；两个 OS 分配端口的 fake TCP bridge，禁止连接其他端点 |
| 端口选择 | 自动选择首个可用端口；显式端口成功、占用错误；范围耗尽错误 |
| MCP stdio | initialize、tools/list、resources/list 通过；名称 wmw-abaqus-mcp，**16 tools、4 resources**，未发出 CAE bridge 请求 |
| 工具参数 | 13 个 CAE 工具均包含可选 session_id |
| Python 打包 | wheel 构建、隔离 target 安装/import、editable target 安装通过 |
| 源码追溯 | 发布 Python 文件与 source-manifest.json 的 SHA-256 匹配；核心代码 diff 仅为声明的名称/菜单/说明字符串 |
| 公开文件 | allowlist 与敏感模式扫描通过，排除环境、缓存、模型、日志和结果 |
| 本地 HTML | 5 页；本地链接/片段有效，代码可见文本与源 Markdown 保持一致；安装/操作文档 12 个代码块实际复制通过（按 Windows 换行规则核对） |
| 字体与布局 | Chromium 在 1440 / 390 px 预览；CDP 实测中文 Microsoft YaHei、英文 Times New Roman，无页面横向溢出或标题裁切 |

测试环境：**Windows、CPython 3.12.10、MCP SDK 1.27.2**。打包时依赖按 pyproject.toml 解析；没有将 `mcp>=1.0` 的所有版本都表述为已验证。

复核命令（使用已安装依赖的外部 Python）：

```powershell
python -B -m unittest discover -s tests -v
python -B scripts/check_mcp_stdio.py
python -m pip wheel --no-deps --wheel-dir .qa/wheels .
python scripts/build_publication.py
```

## 适用范围

**隔离测试使用 fake TCP JSON endpoints，不连接正在运行的 CAE。** 本次发布不重新启动 CAE、不修改现有模型、不提交求解器作业。

没有将离线检查或模型作业完成视为科学模型验收。未验证其他 Abaqus 版本、Linux/macOS GUI、远程网络部署或所有 ODB/截图工作流。`abaqus://status` 与 `abaqus://session-telemetry` 的默认端点限制保留并在 README 明示。

## 源码追溯

[source-manifest.json](source-manifest.json) 保存原始主插件、server、停止脚本与 loader 的 SHA-256；发布副本仅做声明的品牌修改。发布 ZIP 另附 SHA256SUMS。
