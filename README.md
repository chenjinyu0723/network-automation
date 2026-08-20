# AI Agent 工业交换机自动配置

本项目是一个本地单用户 Web/Windows 桌面应用：用户导入交换机手册，绘制拓扑并填写需求，系统使用 LLM + 手册混合检索生成每台设备的可编辑 CLI 草案，用户逐台确认后用 Netmiko 下发并查看完整回显。

## 当前能力

- React 18 + Ant Design + React Flow 前端，FastAPI + SQLite 后端。
- CHM（7-Zip 解包后 HTML）、PDF、HTML、TXT/Markdown 手册导入和异步断点解析。
- 命令名精确匹配 + SQLite FTS5/BM25 + 可选 Embedding 的本地混合检索。
- 最多两轮、每轮最多五条查询的 LLM 引导检索；页面级去重，检索不到时保留可编辑最佳努力草案。
- 先生成可编辑配置思路，再逐设备生成命令；思路为空不能进入命令阶段。
- Huawei VRP、H3C Comware、Cisco IOS、Arista EOS 和通用手册方言；Huawei VLAN 有专用编译路径，其它能力走通用手册证据路径。
- 拓扑、手册、模板单项保存、打开、导入、导出和删除；模板包含可拖动拓扑、需求、思路和最终命令，不包含密码或内部模型状态。
- Netmiko 单设备一次性 `send_config_set`，实时回显、验证、验证成功自动 `save`；PC SSH ping 验收已移除。
- LLM 支持 `adaptive/always/off` thinking，并同时发送两种兼容字段；API Key 使用系统 keyring。

当前输出是“可审阅草案”，不是任意厂商/版本下的正确性保证。真实设备、版本和硬件特性必须由用户审阅后再下发。

## 快速启动

后端：

```powershell
uv sync --extra dev
uv run uvicorn app.main:app --reload --app-dir apps/api
```

前端：

```powershell
npx --yes pnpm@11.9.0 install --frozen-lockfile
npx --yes pnpm@11.9.0 --filter network-automation-web dev
```

打开 `http://127.0.0.1:5173`。后端 API 默认是 `http://127.0.0.1:8000`。命令从仓库根目录执行；不建议使用 `pnpm --dir apps/web install`。

## Windows EXE

首次在新电脑构建前，需要安装 Node.js LTS（建议 20 或更高版本，包含 npm/npx）和 `uv`。不需要运行 `corepack enable`，也不需要全局安装 pnpm。

随后在仓库根目录执行：

```powershell
.\scripts\build_desktop.ps1
```

该脚本会跳过 Corepack 生成的 `pnpm.cmd` shim；如果检测到真实的 pnpm 就直接使用，否则通过 `npm exec` 临时下载并运行锁定的 `pnpm@11.9.0`，不依赖 Corepack 或管理员权限。Node 支持时，脚本会自动使用 Windows 系统根证书。如果公司 HTTPS 代理替换了证书且仍出现 `UNABLE_TO_VERIFY_LEAF_SIGNATURE`，应让 IT 将公司根证书安装到 Windows“受信任的根证书颁发机构”，或在当前 PowerShell 设置 `NODE_EXTRA_CA_CERTS` 为公司 CA PEM 文件路径。仅在公司安全规定允许时再设置 `npm config set strict-ssl false`。脚本会自动运行依赖安装、前端构建、`uv sync --extra dev --extra desktop` 和 PyInstaller。它不再直接调用 `apps/web/node_modules/.bin`，因此新环境不会因 pnpm 工作区的链接布局不同而找不到 `tsc` 或 `vite`。

双击 `release\NetworkAutomation\NetworkAutomation.exe`。发布时必须保留整个 `release\NetworkAutomation\` 目录和 `_internal`。桌面版启动本地 FastAPI，只监听 `127.0.0.1`，需要 WebView2 Runtime。

开发版数据默认在 `data/`；EXE 数据在 `%LOCALAPPDATA%\NetworkAutomation\data`。可用 `APP_DATA_DIR` 覆盖开发数据目录。原始手册、解包页面、SQLite、日志和导出文件均在该目录。

## 一次完整体验

1. 在“设置”填写 LLM/Embedding 的 base URL、模型、温度、维度和批量大小，并测试 LLM 连通性。
2. 在“手册管理”导入手册，等待解析完成；需要语义检索时点击“构建 Embedding”。
3. 在“拓扑编辑”拖入交换机/PC，填写每台设备自己的 IP/前缀/网关和 SSH 信息，用连线工具连接设备，再填写两端真实接口并保存拓扑。
4. 在“配置规划”选择已保存拓扑和已完成手册，输入需求并生成配置思路。审阅、修改并保存思路。
5. 点击“确认思路并生成命令”，等待各设备依次完成；点击设备查看和编辑命令。右侧只显示当前阶段状态，任务切换或断线后可恢复。
6. 在“下发与结果”选择一台设备，确认 SSH 信息和命令后提交。查看 `display version`、完整配置回显、验证和 `save`；每台设备单独确认。
7. 在规划页把已完成任务保存为模板，随后在“模板管理”查看、编辑标题简介、导入导出或删除。

## 设置与接口

LLM 和 Embedding 均使用 OpenAI 兼容接口。base URL 可填写 `/v1/` 或完整 endpoint，客户端会规范化。Embedding 支持 `dimensions` 和批量 `input: [...]`。传输使用固定的 `httpx.AsyncClient(verify=False, transport=httpx.AsyncHTTPTransport(verify=False))`；规划没有应用层超时，用户可以停止任务。

LLM 首次请求同时携带：

```python
extra_body={
    "chat_template_kwargs": {"enable_thinking": True},
    "thinking": {"type": "enabled"},
}
```

不兼容的供应商会自动按兼容形状重试。Embedding 向量保存在 SQLite，不依赖外部向量库；未配置或索引失败时降级为精确 + FTS5。

## 代码入口

| 任务 | 文件 |
|---|---|
| FastAPI/桌面启动 | `apps/api/app/main.py`、`apps/api/app/desktop.py` |
| API 和 SSE | `apps/api/app/api/routes.py` |
| 数据库/模型 | `apps/api/app/db.py`、`apps/api/app/models.py` |
| 手册解析 | `apps/api/app/ingestion/pipeline.py`、`chm.py` |
| 检索/Embedding | `apps/api/app/retrieval/hybrid.py`、`active.py`、`embeddings.py` |
| LangGraph/规划 | `apps/api/app/agents/graph.py`、`planning/service.py` |
| LLM 客户端 | `apps/api/app/llm/client.py` |
| Netmiko 执行 | `apps/api/app/execution/service.py` |
| React 导航和页面 | `apps/web/src/app/App.tsx`、`features/` |
| 桌面构建 | `scripts/build_desktop.ps1` |

完整的模块职责、数据模型、状态流转、检索算法、执行细节和接手清单见 [项目实现报告](docs/项目实现报告.md)。用户操作说明见 [工业交换机自动配置用户手册](docs/工业交换机自动配置用户手册.docx)，历史场景验证见 [常用组网场景验证报告](docs/常用组网场景验证报告.md)。

## 测试与质量门

```powershell
uv run pytest -q
uv run ruff check apps/api/app
npx --yes pnpm@11.9.0 --filter network-automation-web run build
```

发布 EXE 前还要执行 `scripts/build_desktop.ps1`，并检查 `release\NetworkAutomation\NetworkAutomation.exe` 与 `_internal\web_dist\index.html` 均存在。当前历史基线为后端 `122 passed`；以后以本机实际测试输出为准。

## 安全与仓库约定

- 不提交真实 API Key、SSH 密码、设备回显、SQLite 和用户导入手册。
- `.gitignore` 已忽略 `data/`、`*.db`、密钥文件、手册、构建产物、日志和 QA 数据。
- 仅在获得授权的实验设备上执行命令；保护端口和上联端口不要误填为可配置端口。
- 代码和项目文档采用 [MIT License](LICENSE)。厂商手册、设备配置和用户导入数据不因本项目许可证自动获得再分发授权。
