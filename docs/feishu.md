# 飞书企业自建应用接入

飞书客户端接入自托管 Design Agent，复用与 Slack 相同的 agent、模型、品牌、工具和
Docker 沙箱。使用飞书官方 Python SDK 的长连接，无需公网域名、回调服务器或内网穿透。
此接入适用于国内飞书企业自建应用；MDA 的托管 Slack 渠道配置不适用于飞书。

## 准备信息

| 配置 | 来源与用途 |
| --- | --- |
| `FEISHU_APP_ID` | 开发者后台 → 企业自建应用 → 凭证与基础信息 → App ID |
| `FEISHU_APP_SECRET` | 同一页面的 App Secret；只写入本机 `.env`，不要发到聊天、日志或 Git |
| `FEISHU_TENANT_KEY` | 本企业的租户标识；严格限制可触发 agent 的企业，不能用企业名称代替 |
| 应用可用范围 | 管理员将测试成员纳入范围，发布应用，并将机器人加入测试群 |

Tenant Key 可从开发者后台的接收消息事件调试记录中读取 `header.tenant_key`，或通过
[获取企业信息](https://open.feishu.cn/document/server-docs/tenant-v2/tenant/query) API
的 `data.tenant.tenant_key` 获取；后一种方式需要该接口的企业信息读取权限。
不要复制包含 token 和消息正文的整份事件给别人。

现有模型、品牌和沙箱配置可以继续使用。App Secret 留在宿主进程，文件通过宿主下载后
放入沙箱，沙箱不接收应用密钥或飞书访问凭证。

## 飞书后台配置

1. 在[飞书开发者后台](https://open.feishu.cn/app)创建**企业自建应用**，开启机器人能力。
2. 在权限管理中申请下列**应用身份**权限，并完成管理员审批。

   | 权限 | 用途 |
   | --- | --- |
   | `im:message.p2p_msg:readonly` | 接收用户发给机器人的私聊 |
   | `im:message.group_at_msg:readonly` | 接收群内 @机器人的消息 |
   | `im:message:send_as_bot` | 发送、回复机器人消息 |
   | `im:message:readonly` | 读取消息及消息中的资源 |
   | `im:resource` | 上传交付文件 |
   | `im:message:update` | 更新处理状态消息；缺少时仍可交互，但状态不能刷新 |
   | `im:message.reactions:write_only` | 在收到的用户原消息上添加“Get”表情作为确认 |

   支持批量导入权限的后台可直接使用
   [权限 JSON](../config/feishu-permissions.example.json)。为方便初次取得 Tenant Key，
   可另行开通 `tenant:tenant:readonly`，然后在获取企业信息接口的 API 调试台查询本企业标识。

3. 将以下三个配置写入本机 `.env`，保留已有模型、品牌和沙箱设置：

   ```dotenv
   FEISHU_APP_ID=cli_your_app_id
   FEISHU_APP_SECRET=your_app_secret
   FEISHU_TENANT_KEY=your_tenant_key
   ```

4. 按下一节启动本地客户端并保持运行，再到 **事件与回调 → 事件配置**，选择
   **使用长连接接收事件**，添加 `im.message.receive_v1`（接收消息 v2.0）。
   飞书保存长连接配置时要求已有客户端在线。
5. 在版本管理与发布中发布应用、设置可用范围；私聊机器人，或把机器人加入测试群后 @它。

仅有群内 @权限时，平台不一定推送话题里后续不带 @的消息，尤其是单独发送的文件。
需要群话题内直接连续发文件时，可由管理员额外审批 `im:message.group_msg`（群消息敏感
权限）。应用仍只处理 @机器人启动的已知话题，忽略其他普通群聊。若不开通，群内每次
请求带 @，参考图可放在含 @的富文本消息中；单独文件建议先在私聊使用。

## 在当前机器启动

```bash
uv sync --extra feishu
uv run design-agent test feishu
uv run design-agent feishu
```

若当前机器已运行 Slack，建议使用独立的状态目录启动飞书，避免两个进程维护同一份任务记录：

```bash
DESIGN_AGENT_WORKSPACE_ROOT=workspace/feishu uv run design-agent feishu
```

看到 `long connection established` 才表示已建立连接；`starting long connection` 仅表示
开始尝试连接。之后在后台的 **事件配置** 页签保存「使用长连接接收事件」。另一个
**回调配置** 页签用于卡片交互等回调，本接入当前无需配置。

`test feishu` 调用凭证和机器人信息接口，不发送消息、不调用模型；它不能证明事件权限、
附件权限和回复交付正常。启动主服务后需要用真实会话验证。
只为同一个飞书应用运行一个消费者进程：官方长连接在多客户端间分发事件，本实现的
会话串行化由单个进程管理。Slack 服务可以继续独立运行。

Docker Compose 也提供独立的飞书 profile：

```bash
docker compose -f deploy/self-host/compose.yaml --profile feishu up -d --build
```

需要同时运行 Slack 时同时指定 `--profile slack --profile feishu`。使用 Compose 前按
[自托管指南](self-hosting.md)准备 Postgres、品牌目录、模型密钥和 Docker 权限。

## 使用与验证

- **私聊**：直接发送文字、图片或文件，再说“按照刚才的参考图做一张横版海报”。同一个
  私聊会话保留上下文，附件名加上消息标识，避免后发文件覆盖前面的同名参考。
- **群聊**：发送“@机器人 根据这张参考图制作海报”，结果发回该话题。不同话题的对话
  与沙箱隔离；同一话题中的请求顺序执行。
- **附件**：接收普通文件、图片、音频、视频，以及富文本里的图片；它们进入本话题的
  `/workspace/attachments/`。支持在一个富文本消息气泡中同时发送说明文字和 PDF 等文件
  附件（消息的 `files` 列表），下载完成后再启动该轮任务；文件夹附件暂不支持，请先打包
  成普通文件。传输音视频文件不代表模型能直接理解音视频内容。
  自托管附件会在沙箱中自动预处理 PDF，提供正文、最长边 1600 像素的页面预览、内嵌图片
  和 `pdf-manifest.json`，在调用 agent 前把清单路径交给它。agent 复用视频抽帧时的 `look`
  工具查看页面和图片；内嵌图片可作为制作素材，矢量图和版式保留在页面预览中。
  默认处理前 20 页、最多 64 个内嵌图片组件（含蒙版），单图最多 2000 万像素、输出合计
  最多 64 MiB，超出范围明确记录在清单或使预处理失败，不宣称全量解析。扫描件不保证有
  可提取的文字，视觉理解依赖可用的视觉模型。Word/PPT 等格式尚无自动预处理。
  更新 PDF 工具后需重建沙箱镜像，新会话使用新镜像。
- **交付**：agent 使用原有 `upload_files`，文件作为可下载附件发送到原私聊或话题，
  并保留本机 `workspace/out/` 副本。图片和视频目前也按文件交付，不提供原生媒体播放器。
- **状态与回复**：每轮更新一条状态消息，展示主要工具步骤；最终回复使用飞书 `post` 富文本
  消息中的 Markdown 标签渲染标题、列表、引用、代码块、表格和链接。长回复优先按段落分段，
  并在跨消息的代码块边界补齐围栏。后台任务完成后的 agent 回复也发回原会话。状态发送失败
  会记日志，仍继续生成并尝试交付结果。

建议验收：私聊问答 → 发一张参考图并生成文件 → 要求修改刚才结果 → 群内 @并确认结果
回到原话题 → 两个话题并行确认上下文隔离 → 长任务等待后台完成回复。
实际生成会使用已配置的模型和媒体服务并产生相应费用。

## 长连接在线，但私聊没有回复

长连接验证只证明客户端在线，不证明消息接收权限已生效。`im.message.receive_v1` 的
“所需权限（开通任一即可）”列表中，群内 @权限只能让平台推送相应群消息。私聊还必须
让 `im:message.p2p_msg:readonly`（读取用户发给机器人的单聊消息）显示“已开通”，并完成
权限变更需要的发布或管理员审批。`im:message:readonly` 不能代替这项事件接收权限。

进程日志用固定字段区分阶段，不包含消息正文、用户标识或密钥：

- `long connection established`：已建立连接。
- `message event accepted`：接收到了消息事件并通过入口校验。
- `message event ignored: ...`：事件已到达，但 App、企业或发送者等校验未通过。
- `progress card stopped: ...`：进度卡片接口拒绝请求；后缀是安全错误码，任务仍继续执行。

没有消息事件日志时，可到飞书后台的事件日志检索中检查平台是否产生并推送了
`im.message.receive_v1`。确认权限生效后重新建立连接，再发送一条新消息验证。

## 当前边界

- 展开的失败步骤附带简短原因（超时、权限、资源缺失、连接失败等）；仅展示固定分类文案，不回显原始错误、凭证或工具参数。无法识别时明确说明未提供可安全展示的具体原因。
- 处理中标题显示“已完成 n 步 · 继续处理中…”，失败步骤单独计数；不展示动态总步数或“已记录”计数。
- 步骤描述复用 Slack 的描述器，根据工具名称、命令和文件类型展示具体操作，例如打开技能手册、运行渲染或读取脚本；不直接展示原始工具参数。
- 每轮使用默认收起的进度卡片，点击“处理步骤”展开查看每次工具执行的记录、状态和
  工具耗时（以 `t` 标注）。标题中的“本轮总耗时”包含模型调用、等待与工具执行；单步仅统计工具调用，
  不足一秒显示“<1秒”，并行步骤的耗时不能相加作为总耗时。卡片更新合并限频，但不会跳过记录；并行步骤分别保留，成功或失败取自实际
  工具返回。每 25 步组成一个默认收起的组；按实际序列化消息大小分页，并预留步骤状态变化空间。
  只有接近大小预算时才续发卡片，续页只显示步骤范围；约 100 步的常见任务优先保留在一张卡片内。最终回复及文件独立发送。
  卡片整体更新可能重置用户临时展开的状态；不依赖卡片按钮回调。进度卡接口失败不会
  中止任务，最终回复仍正常尝试发送。此列表记录当前轮主 agent 的工具调用，不包含
  后台 helper 的内部步骤；后台任务完成通知仍通过原有回复通道送达。

- 单个输入资源最多 100 MiB、每条消息最多 10 个资源；单个输出文件非空且不超过
  30 MiB。输出上限也写在 `upload_files` 的工具描述中，超限时应压缩或拆分交付。
- 同一会话的后续消息排队执行，不会像 Slack 的 steering 中间件那样在当前模型调用间
  注入；“停止”消息也会排队。没有 Slack 原生流式任务组或持续费用 footer。
- 对每个会话最近 256 条已接收消息持久化去重。进程在已确认事件、尚未处理完时退出，
  不会自动重放该轮输入；用户应重新发送新消息。会话检查点和沙箱文件仍按现有机制保存。
  不保证跨平台网络故障下严格恰好一次交付；单文件回复使用稳定 UUID 降低部分批次重试
  造成的重复，上传成功记录仍由共享交付账本管理。
- 云文档链接只是文字，不自动授予文档访问权限；尚未实现飞书文档、表格、知识库或
  卡片按钮回调。机器人消息和其他 App / 企业的事件不会触发 agent。

## 官方接口依据

- [长连接接收事件](https://open.feishu.cn/document/ukTMukTMukTM/uYDNxYjL2QTM24iN0EjN/event-subscription-configure-)
- [官方 Python SDK](https://github.com/larksuite/oapi-sdk-python)
- [接收消息](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive)
- [回复消息](https://open.feishu.cn/document/server-docs/im-v1/message/reply)
- [发送消息内容结构（Markdown 富文本）](https://open.feishu.cn/document/server-docs/im-v1/message-content-description/create_json?lang=zh-CN)
- [折叠面板](https://open.feishu.cn/document/feishu-cards/card-json-v2-components/containers/collapsible-panel)
- [更新消息卡片](https://open.feishu.cn/document/server-docs/im-v1/message-card/patch)
- [获取消息中的资源文件](https://open.feishu.cn/document/server-docs/im-v1/message/get-2)
- [上传文件](https://open.feishu.cn/document/server-docs/im-v1/file/create)

## 单聊开启新会话

在与机器人的单聊中，单独发送 `/new`。机器人确认后，后续消息使用新的上下文和独立
sandbox；容器按需创建。请重新发送新任务需要的图片或文件。旧会话与文件保留，已在执行
的旧任务继续完成并在原单聊中交付。新会话选择会持久保存，重启不会切回旧会话。
该命令仅用于单聊；群聊继续按话题区分会话。目前不提供切回旧单聊会话的命令。
