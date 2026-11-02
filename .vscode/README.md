# 在 VS Code 中调试 Slack Agent

1. 在这台 Linux 机器上打开仓库根目录。如果通过 Remote SSH 开发，在远端安装
   Microsoft 的 **Python** 和 **Python Debugger** 扩展。
2. 打开“运行和调试”，选择 **Design Agent: Slack**，按 **F5**。
3. 启动前的任务会停止 `design-agent-slack.service`，避免后台服务和调试进程同时处理消息。
   切换前让正在进行的任务完成；停止服务会中断尚未结束的工作。
4. 终端出现 `Bolt app is running!` 后，在 Slack 的 Agent 私聊中发送需求，或在已邀请
   Agent 的频道中 @ 它。继续修改时回复原来的 Slack 线程。

配置使用仓库的 `.venv`、根目录 `.env` 和原有 `workspace/`，不需要复制密钥。
Docker 需要保持运行；网络代理继承 VS Code 的环境。如果调试时出现网络连接错误，
检查启动 VS Code 的环境是否包含后台服务使用的代理配置，不要把代理凭据写入 `launch.json`。

建议在 `src/design_agent/surfaces/slack/bolt_app.py` 的 `SlackAdapter.handle` 内设置断点，
或在 `src/design_agent/cli.py` 的 `slack_command` 内观察启动过程。断点暂停期间，
Slack 消息的处理也会等待。这个配置调试宿主 Python 服务，容器内渲染程序需另行调试。

按 **Shift+F5** 停止调试后，Slack Agent 不会自动切回后台运行。需要恢复时，在终端执行：

```bash
systemctl --user start design-agent-slack.service
systemctl --user is-active design-agent-slack.service
```

F5 的前置任务只停止服务，不会取消已有的开机自启设置。手动修改任务或启动方式时，
保持同一套 Slack 配置只有一个 Agent 进程运行。
