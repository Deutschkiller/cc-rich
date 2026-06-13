#!/usr/bin/env python3
"""Claude Code Web Proxy — Python server with SSE log streaming."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import html
import json
import os
import pty
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import uuid
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

try:
    import websockets
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets"])
    import websockets

ROOT = Path(__file__).resolve().parent
PUBLIC_DIR = ROOT / "public"
KNOWLEDGE_DIR = ROOT / "knowledge"
_DATA_HOME = Path(os.environ.get("CCRICH_DATA", Path.home() / ".cc-rich"))
GENERATED_DIR = _DATA_HOME / "generated"
STATE_DIR = _DATA_HOME / ".claude-web"
LOG_DIR = STATE_DIR / "logs"
SESSION_ID_FILE = STATE_DIR / "session_id"
HISTORY_FILE = STATE_DIR / "history.json"
OUTPUT_FILE = GENERATED_DIR / "claude-output.html"

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "3000"))
TERMINAL_PORT = int(os.environ.get("TERMINAL_PORT", str(PORT + 1)))
_TERMINAL_BASE = os.environ.get("TERMINAL_CMD", os.environ.get("CLAUDE_COMMAND", "ccr"))
_TERMINAL_ARGS = json.loads(os.environ.get("TERMINAL_ARGS", os.environ.get("CLAUDE_ARGS", '["code"]')))
TERMINAL_CMD_ARGS = [_TERMINAL_BASE, *_TERMINAL_ARGS, "--permission-mode", "bypassPermissions"]
STREAM_TIMEOUT_SECONDS = int(os.environ.get("STREAM_TIMEOUT_SECONDS", "1800"))

LOG_DIR.mkdir(parents=True, exist_ok=True)
PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
GENERATED_DIR.mkdir(parents=True, exist_ok=True)


def send_json(handler: SimpleHTTPRequestHandler, data: dict, code: int = 200) -> None:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_json_body(handler: SimpleHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(length).decode("utf-8")
    return json.loads(raw or "{}")


def get_or_create_session_id() -> str:
    if SESSION_ID_FILE.exists():
        session_id = SESSION_ID_FILE.read_text(encoding="utf-8").strip()
        if session_id:
            return session_id

    session_id = str(uuid.uuid4())
    SESSION_ID_FILE.write_text(session_id, encoding="utf-8")
    return session_id


def make_msgid(message: str) -> str:
    digest = hashlib.md5(message.encode("utf-8")).hexdigest()[:8]
    return f"{int(time.time() * 1_000_000)}_{digest}"


def read_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []

    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []

    return data if isinstance(data, list) else []


def append_history(msgid: str, message: str) -> None:
    items = read_history()
    items.insert(
        0,
        {
            "msgid": msgid,
            "title": message.replace("\n", " ")[:80],
            "time": time.time(),
        },
    )
    HISTORY_FILE.write_text(json.dumps(items[:80], ensure_ascii=False, indent=2), encoding="utf-8")


def claude_command(is_continue: bool) -> list[str]:
    prefix = os.environ.get("CLAUDE_COMMAND", "ccr")
    args = json.loads(os.environ.get("CLAUDE_ARGS", '["code"]'))

    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ValueError("CLAUDE_ARGS 必须是 JSON 字符串数组，例如 '[\"code\"]'")

    cmd = [
        prefix,
        *args,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
    ]

    if is_continue:
        cmd.append("--continue")

    return cmd


def newest_generated_html(started_at: float) -> Path | None:
    candidates = []

    for path in GENERATED_DIR.glob("*.html"):
        if path.name == OUTPUT_FILE.name:
            continue

        try:
            modified_at = path.stat().st_mtime
        except OSError:
            continue

        if modified_at >= started_at - 1:
            candidates.append((modified_at, path))

    if not candidates:
        return None

    return max(candidates, key=lambda item: item[0])[1]


def build_prompt(message: str) -> str:
    output_path = str(OUTPUT_FILE)
    kb_path = str(KNOWLEDGE_DIR)
    return f"""你正在通过一个本地网页代理回答用户。

你必须第一步就把最终结果写入文件 `{output_path}`。

生成 HTML 之前，先读取 `{kb_path}/` 目录下的所有文件作为知识库上下文。
这些文件包含项目背景、需求文档、设计规范等，生成的页面内容必须基于这些知识。

要求：
- 写入完整 HTML 文档，从 <!doctype html> 到 </html>
- HTML 内必须包含 CSS；如需交互可以包含少量浏览器端 JavaScript
- 页面会被放入 iframe 预览，所以必须能独立渲染
- 不要输出 Markdown 代码块
- 不要使用 WebSearch、WebFetch、MCP、Task 或联网工具，除非用户明确要求联网
- 不要写入其他 HTML 文件，只写 `{output_path}`
- 写完文件后，只用一句话确认已写入

用户需求：
{message}"""


def log_event(log_file, event: dict) -> None:
    log_file.write(json.dumps(event, ensure_ascii=False) + "\n")
    log_file.flush()


def fallback_html_from_text(text: str) -> str:
    escaped = html.escape(text)
    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8">
    <style>
      body {{ margin: 0; padding: 24px; background: #0d1117; color: #c9d1d9; font-family: system-ui, sans-serif; }}
      pre {{ white-space: pre-wrap; line-height: 1.6; }}
    </style>
  </head>
  <body><pre>{escaped}</pre></body>
</html>"""


def error_html(title: str, detail: str) -> str:
    escaped_title = html.escape(title)
    escaped_detail = html.escape(detail)
    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8">
    <style>
      body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #0d1117; color: #c9d1d9; font-family: system-ui, sans-serif; }}
      .box {{ max-width: 760px; padding: 28px 32px; border: 1px solid #30363d; background: #161b22; border-radius: 8px; }}
      h1 {{ color: #f85149; font-size: 22px; margin: 0 0 12px; }}
      pre {{ white-space: pre-wrap; color: #8b949e; line-height: 1.6; }}
    </style>
  </head>
  <body><div class="box"><h1>{escaped_title}</h1><pre>{escaped_detail}</pre></div></body>
</html>"""


def spawn_claude(message: str, msgid: str, is_continue: bool) -> None:
    log_path = LOG_DIR / f"{msgid}.jsonl"
    stdout_text = []
    started_at = time.time()

    if OUTPUT_FILE.exists():
        OUTPUT_FILE.unlink()

    with log_path.open("w", encoding="utf-8") as log_file:
        try:
            prompt = build_prompt(message)
            cmd = claude_command(is_continue)
            mode = "连续对话" if is_continue else "新会话"
            log_event(log_file, {"type": "proxy_status", "message": f"{mode} · 启动 {' '.join(cmd)}"})

            proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env={**os.environ, "NO_COLOR": "1"},
            )
            assert proc.stdin is not None
            proc.stdin.write(prompt)
            proc.stdin.close()

        except Exception as exc:
            log_event(log_file, {"type": "proxy_error", "message": f"无法启动 Claude Code：{exc}"})
            log_event(log_file, {"type": "claude_done", "exit_code": 1})
            return

        def read_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                log_event(log_file, {"type": "stderr", "text": line.rstrip("\n")})

        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stderr_thread.start()

        assert proc.stdout is not None
        for line in proc.stdout:
            stripped = line.strip()
            if not stripped:
                continue

            stdout_text.append(line)
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                event = {"type": "stdout", "text": stripped}
            log_event(log_file, event)

        proc.wait()
        stderr_thread.join(timeout=1)

        generated = newest_generated_html(started_at)
        if not OUTPUT_FILE.exists() and generated:
            OUTPUT_FILE.write_text(generated.read_text(encoding="utf-8"), encoding="utf-8")

        if not OUTPUT_FILE.exists() and stdout_text:
            OUTPUT_FILE.write_text(fallback_html_from_text("".join(stdout_text)), encoding="utf-8")

        if not OUTPUT_FILE.exists():
            OUTPUT_FILE.write_text(
                error_html("本轮没有生成 HTML", f"Claude Code 本轮结束后仍未写入 {OUTPUT_FILE}。"),
                encoding="utf-8",
            )

        log_event(log_file, {"type": "claude_done", "exit_code": proc.returncode})


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC_DIR), **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/api/status":
            send_json(self, {"session_id": get_or_create_session_id()})
            return

        if parsed.path == "/api/stream":
            self.handle_stream(parsed.query)
            return

        if parsed.path == "/api/read-output":
            self.handle_read_output()
            return

        if parsed.path == "/api/history":
            send_json(self, {"items": read_history()})
            return

        if parsed.path == "/api/pages":
            pages = []
            for p in sorted(GENERATED_DIR.glob("*.html"), key=lambda x: x.stat().st_mtime, reverse=True):
                pages.append({
                    "name": p.name,
                    "mtime": p.stat().st_mtime,
                    "size": p.stat().st_size,
                })
            send_json(self, {"pages": pages})
            return

        if parsed.path == "/api/knowledge":
            self.handle_knowledge_list()
            return

        if parsed.path == "/api/knowledge/read":
            self.handle_knowledge_read(parsed.query)
            return

        if parsed.path.startswith("/g/"):
            self.handle_generated_file(parsed.path)
            return

        if parsed.path == "/":
            self.path = "/index.html"

        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/api/ask":
            self.handle_ask()
            return

        if parsed.path == "/api/reset":
            self.handle_reset()
            return

        send_json(self, {"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path.startswith("/api/pages/"):
            self.handle_delete_page(parsed.path)
            return

        send_json(self, {"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def handle_ask(self) -> None:
        try:
            body = read_json_body(self)
        except json.JSONDecodeError:
            send_json(self, {"error": "请求 JSON 无效"}, HTTPStatus.BAD_REQUEST)
            return

        message = str(body.get("message", "")).strip()
        is_continue = bool(body.get("continue", False))

        if not message:
            send_json(self, {"error": "请输入要发送给 Claude Code 的内容"}, HTTPStatus.BAD_REQUEST)
            return

        msgid = make_msgid(message)
        if OUTPUT_FILE.exists():
            OUTPUT_FILE.unlink()
        append_history(msgid, message)
        thread = threading.Thread(target=spawn_claude, args=(message, msgid, is_continue), daemon=True)
        thread.start()

        print(f"[claude] {msgid}: continue={is_continue} {message[:80]!r}")
        send_json(self, {"msgid": msgid, "session_id": get_or_create_session_id(), "status": "started"})

    def handle_stream(self, query: str) -> None:
        params = parse_qs(query)
        msgid = params.get("msgid", [""])[0]
        start_line = int(params.get("start", ["0"])[0])

        if not msgid:
            send_json(self, {"error": "missing msgid"}, HTTPStatus.BAD_REQUEST)
            return

        log_path = LOG_DIR / f"{msgid}.jsonl"

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        line_num = start_line
        deadline = time.time() + STREAM_TIMEOUT_SECONDS

        while time.time() < deadline:
            if log_path.exists():
                lines = log_path.read_text(encoding="utf-8").splitlines()
                while line_num < len(lines):
                    line = lines[line_num].strip()
                    line_num += 1

                    if not line:
                        continue

                    try:
                        self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        return

                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        event = {}

                    if event.get("type") == "claude_done":
                        try:
                            self.wfile.write(b"data: [DONE]\n\n")
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            pass
                        return

            time.sleep(0.25)

        try:
            self.wfile.write(b'data: {"type":"stream_timeout"}\n\n')
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def handle_generated_file(self, path: str) -> None:
        filename = unquote(path[len("/g/"):])
        if not filename or ".." in filename or "/" in filename:
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.end_headers()
            return

        file_path = GENERATED_DIR / filename
        if not file_path.is_file():
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(file_path.stat().st_size))
        self.end_headers()
        with file_path.open("rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def handle_knowledge_list(self) -> None:
        def walk(dir_path: Path, base: Path) -> list[dict]:
            items = []
            try:
                entries = sorted(dir_path.iterdir(), key=lambda x: (not (x.is_dir() or x.is_symlink()), x.name.lower()))
            except PermissionError:
                return items
            for p in entries:
                if p.name.startswith(".") or p.name == "__pycache__":
                    continue
                name = p.name
                rel = str(p.relative_to(base))
                try:
                    is_dir = p.is_dir() or (p.is_symlink() and p.resolve().is_dir())
                except OSError:
                    is_dir = False
                if is_dir:
                    children = walk(p, base) if p.is_dir() else walk(p.resolve(), base)
                    items.append({"name": name, "path": rel, "type": "dir", "children": children})
                else:
                    items.append({"name": name, "path": rel, "type": "file", "size": p.stat().st_size})
            return items

        tree = walk(KNOWLEDGE_DIR, KNOWLEDGE_DIR)
        send_json(self, {"tree": tree})

    def handle_knowledge_read(self, query: str) -> None:
        params = parse_qs(query)
        rel = params.get("path", [""])[0]
        if not rel or ".." in rel or rel.startswith("/"):
            send_json(self, {"error": "非法路径"}, HTTPStatus.BAD_REQUEST)
            return

        file_path = (KNOWLEDGE_DIR / rel).resolve()
        if not file_path.is_file():
            send_json(self, {"error": "不是文件"}, HTTPStatus.BAD_REQUEST)
            return

        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = "[二进制文件，无法预览]"
        except Exception as exc:
            send_json(self, {"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        send_json(self, {"path": rel, "content": content, "size": file_path.stat().st_size})

    def handle_read_output(self) -> None:
        if OUTPUT_FILE.exists():
            send_json(self, {"found": True, "html": OUTPUT_FILE.read_text(encoding="utf-8")})
        else:
            send_json(self, {"found": False})

    def handle_delete_page(self, path: str) -> None:
        prefix = "/api/pages/"
        if not path.startswith(prefix):
            send_json(self, {"error": "非法路径"}, HTTPStatus.BAD_REQUEST)
            return

        filename = unquote(path[len(prefix):])
        if not filename or ".." in filename or "/" in filename:
            send_json(self, {"error": "非法文件名"}, HTTPStatus.BAD_REQUEST)
            return

        file_path = GENERATED_DIR / filename
        if not file_path.exists() or not file_path.is_file():
            send_json(self, {"error": "文件不存在"}, HTTPStatus.NOT_FOUND)
            return

        if file_path.suffix != ".html":
            send_json(self, {"error": "只能删除 HTML 文件"}, HTTPStatus.BAD_REQUEST)
            return

        file_path.unlink()
        send_json(self, {"status": "deleted", "name": filename})

    def handle_reset(self) -> None:
        if STATE_DIR.exists():
            shutil.rmtree(STATE_DIR)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if OUTPUT_FILE.exists():
            OUTPUT_FILE.unlink()
        send_json(self, {"status": "reset", "session_id": get_or_create_session_id()})

    def log_message(self, fmt: str, *args) -> None:
        if self.path.startswith("/api/stream") or self.path.startswith("/api/read-output"):
            return
        super().log_message(fmt, *args)


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, OSError)):
            return
        super().handle_error(request, client_address)


def _cleanup_terminal_process(pid_to_kill: int, fd_to_close: int) -> None:
    try:
        os.kill(pid_to_kill, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass

    for _ in range(30):
        try:
            wpid, _status = os.waitpid(pid_to_kill, os.WNOHANG)
            if wpid == pid_to_kill:
                break
        except ChildProcessError:
            break
        time.sleep(0.1)

    try:
        os.kill(pid_to_kill, signal.SIGKILL)
        os.waitpid(pid_to_kill, 0)
    except (ProcessLookupError, ChildProcessError, OSError):
        pass

    try:
        os.close(fd_to_close)
    except OSError:
        pass


def start_terminal_server() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def handle_terminal(websocket: "websockets.WebSocketServerProtocol") -> None:
        pid: int | None = None
        master_fd: int | None = None

        try:
            pid, master_fd = pty.fork()
        except OSError as exc:
            await websocket.send(f"\r\n终端启动失败: {exc}\r\n".encode("utf-8"))
            return

        if pid == 0:
            os.execvp(TERMINAL_CMD_ARGS[0], TERMINAL_CMD_ARGS)
            os._exit(1)

        async def pty_reader() -> None:
            try:
                while True:
                    data = await asyncio.get_event_loop().run_in_executor(
                        None, os.read, master_fd, 4096
                    )
                    if not data:
                        break
                    await websocket.send(data)
            except (OSError, websockets.exceptions.ConnectionClosed):
                pass

        reader_task = asyncio.ensure_future(pty_reader())

        try:
            async for message in websocket:
                if isinstance(message, bytes):
                    try:
                        os.write(master_fd, message)
                    except OSError:
                        break
                elif isinstance(message, str):
                    try:
                        ctrl = json.loads(message)
                        if ctrl.get("type") == "resize":
                            rows = int(ctrl.get("rows", 24))
                            cols = int(ctrl.get("cols", 80))
                            winsz = struct.pack("HHHH", rows, cols, 0, 0)
                            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsz)
                    except (json.JSONDecodeError, ValueError, OSError):
                        pass
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass
            _cleanup_terminal_process(pid, master_fd)

    async def serve() -> None:
        async with websockets.serve(handle_terminal, HOST, TERMINAL_PORT, ping_interval=30):
            await asyncio.Future()

    try:
        loop.run_until_complete(serve())
    except Exception as exc:
        print(f"[terminal] WebSocket 服务异常: {exc}")


def main() -> None:
    global PORT, TERMINAL_PORT

    session_id = get_or_create_session_id()
    base_port = PORT
    base_terminal = TERMINAL_PORT

    for offset in range(10):
        try:
            PORT = base_port + offset
            TERMINAL_PORT = PORT + 1
            httpd = QuietThreadingHTTPServer((HOST, PORT), Handler)
            break
        except OSError:
            if offset == 9:
                print(f"无法绑定端口 {base_port}–{base_port + offset}，全部被占用。")
                sys.exit(1)
            print(f"端口 {PORT} 被占用，尝试 {PORT + 1}...")
            continue

    print(
        f"""
Claude Code Web Proxy
  engine: ccr code -p
  stream: JSONL -> SSE -> browser
  output: {OUTPUT_FILE}
  knowledge: {KNOWLEDGE_DIR}
  terminal: ws://{HOST}:{TERMINAL_PORT}
  session: {session_id[:8]}...
  url: http://{HOST}:{PORT}
"""
    )

    terminal_thread = threading.Thread(target=start_terminal_server, daemon=True)
    terminal_thread.start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
