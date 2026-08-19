# AI Agent 工业交换机自动配置

本项目是本地单用户 Web 应用。它将用户注入的交换机手册转为命令知识库，支持手绘拓扑、按任务选择手册、配置规划、逐台审批下发和验证。

## 开发启动

后端：

```powershell
uv sync --extra dev
uv run uvicorn app.main:app --reload --app-dir apps/api
```

前端：

```powershell
pnpm install --frozen-lockfile
pnpm --filter network-automation-web dev
```

生产构建验证：

```powershell
pnpm --filter network-automation-web run build
```

请从工作区根目录执行这些命令。不要使用 `pnpm --dir apps/web install`：在 pnpm 11.9.0 中它会把工作区包当作独立项目安装，并可能错误报出已批准的 `esbuild` 构建脚本被忽略。

默认后端为 `http://127.0.0.1:8000`，前端开发服务器为 `http://127.0.0.1:5173`。

`data/` 是本机运行数据：SQLite 数据库、原始手册、解包内容、导出文件和日志。目录已被 Git 忽略，禁止将真实 API Key、SSH 密码或设备回显提交到仓库。

## 最新回归

2026-08-18 使用已注入的华为 S1700/S5700/S6700 V600R025C00 CHM、已配置 LLM/Embedding 端点完成 8 个基础场景及 8 个模板参考变体，最终为 **16/16 通过**，总耗时 1687.060s。覆盖 OSPF、静态路由、LACP/MSTP、Access VLAN、双 VLAN 三层互通、VRRP、三层 Eth-Trunk 和 iStack；未连接 eNSP、未调用 Netmiko、未下发或保存配置。完整命令审阅、逐组耗时和两项修复记录见 [常用组网场景验证报告](docs/常用组网场景验证报告.md)。

## Windows 桌面应用

桌面版会在本机启动 FastAPI，并在原生 WebView 窗口中打开界面；不需要启动两个开发终端，也不会监听局域网地址。

重新构建发布包：

```powershell
uv sync --extra desktop
& .\scripts\build_desktop.ps1
```

构建完成后双击 `release\NetworkAutomation\NetworkAutomation.exe` 即可启动。当前使用 PyInstaller 的 `onedir` 发布形式，分发时必须保留整个 `release\NetworkAutomation\` 目录，不能只单独复制 `.exe`；同目录的 `_internal` 是运行时和前端资源。

安装包运行数据固定写入 `%LOCALAPPDATA%\NetworkAutomation\data`，包含 SQLite、手册文件、导出结果和日志；卸载或替换发布目录不会删除这些本地数据。首次运行需要系统已安装 Microsoft Edge WebView2 Runtime（当前 Windows 10/11 通常已自带）。

## 当前实现范围

- 手册导入：CHM（7-Zip 解包）、HTML、文本、PDF；CHM/HTML 解析支持断点恢复的本地 Worker。
- CHM：解析 TOC、命令页、命令格式、视图、参数、约束、示例和命令证据。导入器仍会保留可能的型号信息作为内部辅助元数据，但不要求用户审核或选择型号。
- 手册选择策略：每个配置任务由用户选择一份已完成抽取的手册作为唯一命令上下文。拓扑不填写型号，现场 `display version` 只记录审计信息，不参与型号树或系列匹配门禁。
- 本地混合检索：命令名精确匹配 + SQLite FTS5 + 可选 OpenAI 兼容 Embedding。向量以 SQLite BLOB 保存，CPU 余弦检索融合；Embedding 未配置、不可用或尚未建索引时自动降级到前两者。
- Embedding 接口兼容：设置页支持填写 `/v1/` 基地址或完整的 `/v1/embeddings` 地址；配置“向量维度”时按 OpenAI 兼容格式发送 `dimensions`，批量索引发送 `input: [...]`。例如 Qwen3-Embedding-4B 可填写模型名和 `2560` 维度；留空维度则不发送该字段，兼容服务默认维度。
- 拓扑：React Flow 编辑器；交换机和 PC 不显示端点圆点，使用独立“连线（绳）”工具依次点击两个设备生成直线，再在右侧保存两端真实接口名。IP、掩码、网关和 SSH 信息均为当前设备独立的可选字段；拓扑通过 SQLite 保存为 revision，可从“已保存拓扑”重新打开。
- 规划：界面采用两阶段流程。第一步只生成可编辑的“配置思路”，用户确认非空思路后，第二步才运行 LangGraph 的“受约束 LLM 意图精炼 → 显式主动检索 → LLM CommandPlan → 本地证据/拓扑编译 → LLM 审阅提示”，生成逐设备命令草案。思路为空时不会进入命令生成阶段。能力标签不是硬编码白名单：未内置专用插件的需求也走通用手册路径；优先显示已绑定本轮 `command_id`、且通过手册命令前缀、拓扑物理端口、保护端口、维护命令与方言只读验证命令校验的 CLI。若模型 JSON、手册证据绑定或模型响应完全失败，仍会保留 LLM 纯 CLI 草案或手册示例/可编辑占位参考，并以黄色提示标记为“未验证”，不会伪装成已验证配置。模型不能原生 function call、不能持有密码或触发下发。
- CLI 方言：每本手册可选择“自动识别、Huawei VRP、H3C Comware、Cisco IOS、Arista EOS、通用手册”。方言只定义配置会话包装、受限退出命令和只读验证前缀，不决定可配置的网络能力。自动识别只按明确品牌映射；未知品牌默认“通用手册”，不会猜测或注入 `system-view`、`configure terminal` 等厂商命令。Huawei VRP 的 VLAN 专用编译器仅在明确选择/识别到 Huawei VRP 时启用，非华为手册的 VLAN 需求与 OSPF、静态路由、STP、ACL 等能力一样走证据绑定通用路径。
- 规划进度：配置规划页右侧通过本地 SSE 展示任务阶段、模型 thinking 和正式输出；阶段事件保存在 SQLite，页面重连可继续读取。点击“停止”会写入取消令牌和已取消状态；意图、主动检索、命令生成和审阅的流式调用都会在当前 chunk 或下一个 LangGraph 节点停止。
- 配置模板：可将已生成设备命令的任务保存为本地不可变快照，记录标题、简介、拓扑、配置要求、配置思路和逐设备命令；模板管理页支持查看、编辑标题/简介和删除。创建新任务时可选择模板供 LLM 参考角色划分、实施顺序与命令组织，但当前拓扑、需求、端口、VLAN、地址和手册证据优先，旧 CLI 参数不会直接复制。
- LLM 思考策略：设置页提供 `adaptive`（默认）、`always`、`off`。自适应只在需求理解、命令计划/修订、命令审查和结果诊断等推理型节点开启 `thinking`；检索判断、静态校验、保护端口检查、Netmiko 执行和 `save` 不启用 `thinking`。首次请求会同时携带 `chat_template_kwargs.enable_thinking` 与 `thinking.type`；若端点拒绝其中任一字段，按单字段、再无可选字段的兼容序列重试，并在计划审计中记录降级原因。HTTP 传输固定 `verify=False`，且不设置应用层超时。
- 端口：拓扑填写的端口字符串原样进入命令，例如填写 `GE0/0/0` 就下发 `interface GE0/0/0`。仅在端口去重、保护端口和验证回显比较时，将 `GE` 与 `GigabitEthernet` 归为同一接口。`multi_vlan_intervlan` 插件将直连 PC 的端口编译为 Access、交换机互联编译为 Trunk、指定/识别的三层核心编译为 VLANIF；用户明确标记的保护端口不参与自动生成或写入。
- 执行：用户可以在规划页直接编辑命令，再逐台确认下发。下发页通过本地 SSE 在右侧滚动显示 SSH 连接、每条命令、验证与保存的设备回显；密码只保留在活动连接内。手册证据、LLM 审查、拓扑推断和设备顺序都只作为审阅信息，不替代用户决定；写入前只要求当前设备的非空命令已被用户确认，并排除用户明确标记的保护端口。设备验证失败不执行 `save`，验证通过才自动保存。成功下发的 `vlan_access` 计划可执行受限 Undo，Undo 仅能进入当前拓扑中直连 PC 的端口，受保护端口及上联端口会被拒绝。PC SSH ping 验收为可选项，仅允许 Linux/Windows ping 命令白名单。

## LLM 全链路演进路线

最终目标是“拓扑 + 用户需求 → 每台设备的可审阅命令”。落地采用分阶段策略：

1. 已完成基础：LLM 受限理解需求并补充检索词，混合检索定位手册证据，确定性编译器生成 VLAN Access 命令。
2. 已完成 `multi_vlan_intervlan`：从需求解析 `PC → VLAN`，从拓扑区分 Access/Trunk，利用 PC 地址和网关生成核心交换机的 VLANIF；华为 CHM 实测覆盖 `vlan batch`、`port link-type`、`port default vlan`、`port trunk allow-pass vlan`、`interface`、VLANIF 视图 `ip address`。
3. 已完成通用证据编译路径：任意能力（如三层 OSPF、静态路由、链路聚合、STP、ACL）可由 LLM 标记、检索并生成草案，不需要先增加功能白名单。通用路径允许模型给出“一行 CLI + 手册 command_id”，本地只接受命令前缀与手册一致、物理接口在当前拓扑范围内、未触碰保护端口、未包含 save/reboot/reset/delete 等维护命令的草案；验证命令只允许该手册 CLI 方言的只读查询或连通性前缀（如 VRP `display`、IOS `show`、`ping`）。
4. 已完成独立 LLM 命令审查：它只能给出 approve/reject 提示，不能改写 CLI 或触发下发。审查以当前设备角色范围为上下文，避免把其它设备的配置误报为缺项；最终下发决定仍由用户逐台确认。
5. 扩展阶段：为高频能力增加专用插件，以获得更强的确定性参数编译、设备状态断言和可回滚性；写入、保护端口、验证、自动 `save` 始终由确定性节点控制。

命令草案以任务选择的手册、对应语法证据和拓扑端口为依据生成；缺证据、推断或 LLM 审查问题会显示为提示。用户可以编辑草案，并决定是否逐台下发；系统不会自动批量发送。

真实设备测试仅使用获授权设备及明确的只读命令。不要触碰未授权端口或设备。

## 已导入华为样例与当前行为

- `S1700, S5700, S6700 V600R025C00 命令参考.chm` 已入库：8,914 页、8,714 条命令、0 个失败页；规划时直接选择这份手册。
- 已授权 eNSP 设备只读检测为 `S5700-28C-HI / V200R001C00`，其回显会留存在执行审计中，但不会阻断已审批的手册驱动计划。系统仍不会接触受保护的 `GE0/0/2`。
- 真实试验前，仍建议从拓扑页明确标记可操作的 `GE0/0/1`，并逐台查看、确认具体命令；系统只生成草案，不替用户决定是否下发。

## 许可证

本项目代码采用 [MIT License](LICENSE)。该许可仅适用于本仓库中由项目贡献者编写的代码和文档；厂商手册、CHM/PDF、用户导入的数据、设备配置、导出结果及其他第三方材料不因此获得额外授权。使用或再分发此类材料前，请自行确认其版权、保密和安全要求。
