# Claude Code Web Proxy

一个本地网页代理：上半部分渲染 Claude Code 输出的 HTML，中间显示 Claude Code CLI 输出，下半部分是聊天输入框。

## 启动

```bash
python3 server.py
```

打开 http://localhost:3000。

## 后台常驻

推荐用脚本启动，避免终端会话断开后服务退出：

```bash
./scripts/start-server.sh
./scripts/status-server.sh
./scripts/stop-server.sh
```

日志写入 `.claude-web/server.log`，进程号写入 `.claude-web/server.pid`。

在 macOS 上，更稳定的方式是安装为 `launchd` 用户服务：

```bash
./scripts/install-launchd.sh
./scripts/launchd-status.sh
./scripts/uninstall-launchd.sh
```

`launchd` 会在登录后自动启动，并在进程异常退出后自动拉起。日志写入 `.claude-web/launchd.out.log` 和 `.claude-web/launchd.err.log`。

注意：如果项目位于 `~/Documents`，macOS 隐私权限可能阻止 `launchd` 启动的 `/usr/bin/python3` 读取项目文件，日志会出现 `Operation not permitted`。解决方式：

- 把项目移动到不受 Documents 隐私保护影响的目录，例如 `~/dev/cc-rich`
- 或在系统设置里给相关 Python/终端环境授予完整磁盘访问权限
- 或直接在普通终端里运行 `./scripts/start-server.sh`

## Claude CLI 配置

默认执行：

```bash
ccr code -p --output-format stream-json --verbose --permission-mode bypassPermissions
```

可以通过环境变量修改：

```bash
CLAUDE_COMMAND=ccr CLAUDE_ARGS='["code"]' python3 server.py
```

每次提交聊天内容时，Python 服务端会启动一次 Claude Code 子进程，将 stream-json 输出写入 `.claude-web/logs/*.jsonl`，前端通过 SSE 读取日志。Claude Code 会把最终 HTML 写入 `public/claude-output.html`，页面上半部分会读取并渲染这个文件。
