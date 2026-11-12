# ARC Web Explorer（Agent Registration Center）

流程：**连接 Nacos → 发现 Agent → 查看 Card 摘要 → 选择健康端点 → 使用 A2A 交互**。
所有应用文件都在本目录，使用仓库已有 Python 依赖，不需要前端构建。
目录来自 Nacos AI Agent registry，显示当前 namespace 中账号可见的全部 Agent，自动遍历分页。

## 启动与调试

在仓库根目录执行：

```bash
uv sync --all-extras --dev
uv run python scripts/agent-registration-center/app.py
```

打开 **http://127.0.0.1:8099**，`--port 8100` 可更换端口。
也可在 VS Code“运行和调试”中选择 **ARC: Web Client Explorer**，按 F5；服务就绪后自动打开浏览器。
先关闭已占用同一端口的进程，再使用 F5 启动。

默认 Nacos 地址为 `https://arc.meerkatai.cn`，namespace 为 `public`。
可填写其它实例的 origin（不附加 `/nacos`），然后用有目录读取权限的应用账号登录。
本地 Python 进程必须能访问 Nacos 与发现的 Agent 地址；服务只监听本机。
未使用本地凭证文件时，需要在页面填写用户名和密码。登录被拒绝与目录读取权限不足会分别显示明确错误；
修改代码后，VS Code 调试进程需要停止并重新 F5 启动，再刷新页面。

## Agent token 的获取与错误提示

本仓库 Agent 的 A2A 默认匿名（`DESIGN_AGENT_A2A_ALLOW_ANONYMOUS=true`），可留空 Agent token。
切换后需重启目标 Agent，再在本应用重新发现服务以更新 Card。
设为 `false` 后才需要下述凭证；Nacos 目录登录始终独立。

“调用身份 caller”是 token 映射的身份名，不是浏览器、IP 或 Nacos 账号。
认证模式下不同 caller 的任务和文件隔离，共用 caller 则共享；匿名模式所有客户端共享
匿名任务和文件。`contextId` 区分会话，不提供访问权限隔离。
详情页在鉴权说明旁显示这些规则；其它 harness 的权限模型以其部署者说明为准。

Agent token 由目标 Agent 的部署者提供；Nacos 账号只用于目录发现，AgentCard 不包含或签发 token。
如果你负责部署本仓库的 Agent，查看目标服务启动时使用的 `DESIGN_AGENT_ACCESS_TOKEN_CALLERS`，
格式为 `<token>:<caller-id>,...`；客户端只填 token 部分，不带 `Bearer ` 或 caller-id。
其它 harness 按其服务或鉴权网关的签发方式获取。

页面的“Agent token 如何获取”展开说明完整步骤；聊天输入框旁的“填写 / 获取 Agent token”可定位到凭证区。
凭证状态区分未填写、已填写但未验证、目标服务已接受本次操作，以及远端拒绝。

- 未填写所需 token 或格式错误：输入框旁显示“未发送”，不提交任务。
- HTTP 401：目标未接受凭证，向部署者确认 token 是否正确、有效且属于当前服务。
- HTTP 403：目标拒绝访问，部署者需检查身份、任务和资源权限。

操作错误会同时显示在当前面板中，聊天错误会出现在输入框旁和失败消息气泡中；
不会要求用户滚回页面顶部寻找提示。浏览器表单验证失败也会显示明确的未发送状态。

## 浏览与聊天

选择 Agent 和版本后，默认展示 Card 摘要：描述、提供方、输入/输出媒体类型、鉴权声明、可选能力、技能与示例。
“完整服务详情 / AgentCard”折叠项保留原始 JSON；“发现的端点”展示各实例的健康状态。

填写目标 Agent 的 Bearer token（公开服务可留空），再进行聊天。Nacos accessToken 与 Agent 调用凭证独立。
每次协议操作都会重新发现并验证目标端点，只使用健康、enabled 的 A2A 1.0 JSONRPC 端点，覆盖 Card 中可能过时的地址。

- **对话**：普通发送、SSE 流式响应、后台发送（`returnImmediately`）、同 Context 多轮对话，以及继续非终态任务。
  Ctrl / ⌘ + Enter 发送，“新会话”清空 Context；终态任务应在原 Context 下创建新消息，不复用 Task ID。
- **文件与数据**：选择多个本地文件作为 `raw` Parts，或附加文件 URL、结构化 JSON Part。单条消息文件总量上限 8 MiB。
  浏览器检查 Card 的输入媒体类型；匹配媒体类型不代表目标一定接受 raw / URL Part，最终以服务实现为准。
- **流式输出**：聊天气泡展示服务实际发送的消息、任务状态与 artifact 增量，支持 `append` 更新，保留协议事件记录。
  服务没有输出 token 增量时，客户端不会模拟逐字生成。
- **结果**：文本、结构化 JSON、inline 文件与文件 URL。inline 文件可直接下载；URL 文件通过当前身份重新读取任务校验后下载，
  仅支持目标 Agent 同 origin，最大 20 MiB；跨 origin 文件需自行授权访问。
- **任务管理**：按 Context/状态查询与分页，查看历史消息和 artifacts，按 Task ID 查询、订阅/恢复流、请求取消，或带入对话。
- **Push 配置**：创建、查询、分页列出和删除远端回调配置，可填写 token/authentication。
  回调 URL 需能从远端服务访问；本应用不提供公网回调接收服务。
- **协议操作**：11 项核心操作的 JSON 参数编辑器，支持 `tenant`、`metadata`、消息 configuration、完整分页与筛选字段等参数。
  此面板也提供获取鉴权扩展 Card 的入口。

“停止接收”仅断开本地流，**不会取消远端任务**。取消需单独请求，是否成功取决于目标服务与任务状态。
单次连接最长等待 30 分钟，超时后可查询或重新订阅已有任务，避免重复提交。

## 协议操作、Card 声明与强制测试

A2A 1.0 定义标准操作集合；AgentCard 不逐项列出所有 RPC 方法。
页面的操作名、请求/响应类型和参数字段从官方 SDK 的 `A2AService` protobuf descriptor 读取；
可选能力状态则从 Nacos 发现的真实 Card 读取，区分 `true`、`false` 和字段缺失。

- `capabilities.streaming` 对应流式发送与任务订阅。
- `capabilities.pushNotifications` 对应 Push 配置的增删查与列表。
- `capabilities.extendedAgentCard` 对应鉴权扩展 Card。
- 普通发送、任务查询、列表和取消没有逐项 Card 开关；是否成功还取决于服务实现、身份与任务状态。
- `skills` 描述业务专长，通过标准消息接口使用，不会为每项技能产生一个新 RPC。
- 文件上传、URL 和结构化数据都属于 `message.parts`，不是额外上传 RPC；媒体类型依据 `defaultInputModes` 和技能的 `inputModes`。

对应表提供每项操作的 Card 字段、声明状态和测试入口。
默认遵循声明；勾选“仍然测试 Card 未声明支持的能力”后，可以真实发送相关操作或媒体类型，观察远端响应。
强制模式不修改 Card，不降级成其它方法，也不跳过 endpoint 校验或鉴权。
GetExtendedAgentCard 强制测试会发出真实请求，不使用 SDK 的缓存 Card 代替响应。

认证服务的消息调用需要目标 Agent 凭证，和 Nacos 用户名/密码不同。Card 要求 Bearer 但未填写 token 时，页面与后端都会明确提示；
错误 token 导致的远端 401/403 会显示具体 HTTP 状态。
成功发送后若后续 GetTask 查询失败，页面保留提交成功与 Task ID，提示查询，避免把它误判成发送失败后重复提交。

## A2A 1.0 操作覆盖

| 协议操作 | UI 入口 |
| --- | --- |
| `SendMessage` | 对话：普通 / 后台发送 |
| `SendStreamingMessage` | 对话：流式响应 |
| `GetTask` | 任务管理：查看任务 |
| `ListTasks` | 任务管理：查询与下一页 |
| `CancelTask` | 对话或任务管理：请求取消 |
| `SubscribeToTask` | 任务管理：订阅 / 恢复流 |
| `CreateTaskPushNotificationConfig` | Push：创建配置 |
| `GetTaskPushNotificationConfig` | Push：查询配置 |
| `ListTaskPushNotificationConfigs` | Push：列出配置与分页 token |
| `DeleteTaskPushNotificationConfig` | Push：删除配置 |
| `GetExtendedAgentCard` | 协议操作：获取扩展 AgentCard |

实现使用官方 `a2a-sdk 1.3.0`，协议为 A2A 1.0，binding 为 JSONRPC。
客户端覆盖全部核心操作；目标 Agent 是否支持可选能力由 Card 与实际响应决定。
未声明的 streaming、Push、扩展 Card 操作默认禁用；开启强制测试后可尝试，由远端决定结果。
当前本仓库的 Agent 接口仅接受文本 Parts，不支持取消与 Push；上传文件不能使目标服务自动具备文件处理能力。
通用 Web 客户端没有改动这些服务端能力。

## 可选：受保护凭证文件

可以显式指定仓库外的 JSON，避免手动粘贴凭证：

```bash
uv run python scripts/agent-registration-center/app.py --credentials-file /absolute/path/to/private-access.json
```

也可设置 `ARC_CREDENTIALS_FILE` 环境变量，命令行与 VS Code 启动均会继承。
文件结构如下，文件权限设为 `600`，不要提交到公共仓库：

```json
{
  "nacos_username": "<registry-reader>",
  "nacos_password": "<registry-password>",
  "origin": "https://agent.example.com",
  "token": "<agent-caller-token>"
}
```

页面出现“使用本地凭证”选项，取消勾选可手动填写。
文件中的 Nacos 凭证仅用于默认实例，Agent token 仅用于与 `origin` 匹配的发现端点。
配置接口只返回是否提供了凭证，不向浏览器返回文件中的密码或 token。

手动输入的 Nacos 密码仅用于登录；Nacos accessToken 按浏览器会话隔离保存在本地内存，过期后重新登录。
手动 Agent token 不写磁盘或浏览器持久化存储；指定凭证文件时，其内容保留在进程内存直到退出。
聊天与事件记录保存在当前页面内存；刷新页面不保留，已提交的服务端任务仍可通过 Task ID / 任务列表访问。

## 验证

```bash
uv run pytest -q tests/unit/test_arc_web.py tests/unit/test_docs_links.py
uv run ruff check scripts/agent-registration-center tests/unit/test_arc_web.py
node --check scripts/agent-registration-center/app.js
node --test scripts/agent-registration-center/ui.test.cjs
```

离线测试通过真实 SDK 编解码覆盖全部 11 项操作、SSE、文件与结构化消息、分页，以及会话隔离、能力检查和文件下载授权。
真实环境应分别验证 Nacos 发现、目标鉴权与服务端支持的操作；协议 fixture 通过不能替代真实 Agent 功能验证。

实现：[后端](app.py)、[页面](index.html)、[浏览器逻辑](app.js)、[样式](app.css)。
协议与注册指南：[A2A / Nacos](../../docs/a2a-nacos.md)。
规范：[A2A 1.0 核心操作](https://a2a-protocol.org/v1.0.0/specification/#31-core-operations)。

## 长任务和执行进度

多媒体任务建议选择“后台任务”，默认自动查询并订阅。流中断后先 `GetTask`，再恢复订阅；
连续三次订阅失败改为每 3 秒查询。不支持流式的服务直接轮询。认证失败停止，
未取得 Task ID 的不确定提交不会自动重发，避免重复生成。取消勾选自动接收后，后台提交立即结束等待。

单次接收上限 30 分钟；停止接收不取消任务，可点击“查询 / 恢复接收”。刷新页面后，
到任务管理查询任务，再恢复接收。当前页面保留的进度历史不写入浏览器存储。

聊天显示 Task ID、等待时间、最后更新时间和折叠的执行过程。支持标准 A2A 状态文字，
并识别本项目可选的工具进度元数据；不模拟 token、不猜测百分比。断线期间事件不保证回放，
任务查询以远端当前状态为准。Agent 重启后的执行恢复能力以其部署说明为准。

Card 的框架说明与 `documentationUrl` 在摘要中展示；其它渠道是否启用以该实例文档为准。
Nacos 普通服务名 `rad-<Agent名>-a2a` 是 Agent 运行端点的内部映射，目录使用逻辑 Agent 名称。
