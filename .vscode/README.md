# 在 VS Code 中调试聊天客户端

Slack 和 Feishu 都可以用 VS Code 的 F5 前台调试；不需要 systemd。

1. 打开仓库根目录，安装依赖及 Microsoft 的 **Python**、**Python Debugger** 扩展。
   Remote SSH 开发时，在远端安装扩展。
2. 配好根目录 `.env`，确认 Docker 正在运行。
3. 在运行与调试的配置菜单中选择 **Design Agent: Slack** 或 **Design Agent: Feishu**，按
   **F5**。Feishu 配置会使用独立的 `workspace/feishu` 状态目录。
4. 客户端连接就绪后，在对应的聊天中发送请求。**Shift+F5** 停止当前调试进程；选好
   配置后再按 F5，才会以新代码重新启动。

配置使用 `.venv`、`.env` 和现有 `workspace/`。不要把密钥或代理凭据写入调试配置。
断点可放在 `src/design_agent/surfaces/slack/bolt_app.py` 的 `SlackAdapter.handle`、
`src/design_agent/surfaces/feishu/app.py` 的 `FeishuAdapter.handle`，或
`src/design_agent/cli.py` 的相应命令函数。断点暂停时，消息处理也会等待。
这是宿主 Python 调试；容器里的渲染进程需要另行调试。

不用 VS Code 时，同样可以在终端前台运行：

```bash
uv run design-agent slack
```

```bash
DESIGN_AGENT_WORKSPACE_ROOT=workspace/feishu uv run design-agent feishu
```

## 进程生命周期

F5 启动当前选中的客户端，Shift+F5 停止它。不需要创建后台 service，
也不会通过 VS Code 自动配置开机启动。没有运行 Agent 进程时，它不会实时处理 Slack 消息；
之后启动时仍可能收到 Slack 重试投递的事件。

同一个 Slack App 不应同时运行多个独立开发进程。避免在 F5 调试之外再运行
`uv run design-agent slack`；持续运行的团队 Agent 推荐使用独立 Slack App 和独立工作目录。
