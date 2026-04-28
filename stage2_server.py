#!/usr/bin/env python3
import asyncio
import json
import os
import pty
import signal
import subprocess
import termios
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from aiohttp import WSMsgType, web


HOST = "127.0.0.1"
PORT = 8001
BASE_DIRECTORY = os.getcwd()
DEFAULT_SHELL = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
OUTPUT_READ_SIZE = 4096


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_working_directory(requested_cwd: str) -> str:
    safe_cwd = os.path.abspath(requested_cwd)
    if not safe_cwd.startswith(BASE_DIRECTORY):
        raise ValueError(
            "working_directory must stay inside the demo base directory: "
            f"{BASE_DIRECTORY}"
        )
    if not os.path.isdir(safe_cwd):
        raise ValueError(f"working_directory does not exist: {safe_cwd}")
    return safe_cwd


@dataclass
class ShellSession:
    session_id: str
    shell_path: str
    working_directory: str
    process: subprocess.Popen[bytes]
    master_fd: int
    created_at: str
    loop: asyncio.AbstractEventLoop
    status: str = "ready"
    exit_code: int | None = None
    last_activity_at: str = field(default_factory=utc_now_iso)
    output_queue: asyncio.Queue[dict] = field(default_factory=asyncio.Queue)
    lock: threading.Lock = field(default_factory=threading.Lock)
    closed: bool = False
    websocket_attached: bool = False

    def refresh_status(self) -> None:
        if self.closed:
            return
        exit_code = self.process.poll()
        if exit_code is not None:
            self.exit_code = exit_code
            self.status = "exited"
            self.closed = True

    def publish_output(self, text: str) -> None:
        if not text:
            return
        with self.lock:
            self.last_activity_at = utc_now_iso()
        self.loop.call_soon_threadsafe(
            self.output_queue.put_nowait,
            {"type": "output", "data": text},
        )

    def publish_exit(self, exit_code: int) -> None:
        self.loop.call_soon_threadsafe(
            self.output_queue.put_nowait,
            {"type": "exit", "exit_code": exit_code},
        )

    def write_input(self, data: str) -> None:
        self.refresh_status()
        if self.closed:
            raise RuntimeError("session is no longer running")
        os.write(self.master_fd, data.encode("utf-8"))
        with self.lock:
            self.last_activity_at = utc_now_iso()

    def terminate(self) -> None:
        if self.closed:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=1)
        self.refresh_status()
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        self.closed = True
        if self.exit_code is None:
            self.exit_code = -signal.SIGTERM
        self.status = "exited"
        self.publish_exit(self.exit_code)


SESSIONS: dict[str, ShellSession] = {}


def session_reader(session: ShellSession) -> None:
    while True:
        try:
            raw = os.read(session.master_fd, OUTPUT_READ_SIZE)
        except OSError:
            break
        if not raw:
            break
        session.publish_output(raw.decode("utf-8", errors="replace"))

    session.refresh_status()
    if session.exit_code is None:
        session.exit_code = session.process.wait()
    session.status = "exited"
    session.closed = True
    try:
        os.close(session.master_fd)
    except OSError:
        pass
    session.publish_exit(session.exit_code)


def create_session(requested_cwd: str, loop: asyncio.AbstractEventLoop) -> ShellSession:
    safe_cwd = validate_working_directory(requested_cwd)

    master_fd, slave_fd = pty.openpty()
    tty_attrs = termios.tcgetattr(slave_fd)
    tty_attrs[3] &= ~termios.ECHO
    termios.tcsetattr(slave_fd, termios.TCSANOW, tty_attrs)
    env = os.environ.copy()
    env["TERM"] = "dumb"
    env["PS1"] = ""
    env["BASH_SILENCE_DEPRECATION_WARNING"] = "1"
    shell_command = [DEFAULT_SHELL, "--noprofile", "--norc", "-i"]
    if DEFAULT_SHELL.endswith("/sh") and not DEFAULT_SHELL.endswith("/bash"):
        shell_command = [DEFAULT_SHELL, "-i"]

    process = subprocess.Popen(
        shell_command,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=safe_cwd,
        env=env,
        start_new_session=True,
        close_fds=True,
    )
    os.close(slave_fd)

    session = ShellSession(
        session_id=str(uuid.uuid4()),
        shell_path=DEFAULT_SHELL,
        working_directory=safe_cwd,
        process=process,
        master_fd=master_fd,
        created_at=utc_now_iso(),
        loop=loop,
    )

    reader = threading.Thread(
        target=session_reader,
        args=(session,),
        name=f"session-reader-{session.session_id}",
        daemon=True,
    )
    reader.start()
    return session


async def create_session_handler(request: web.Request) -> web.Response:
    payload = await request.json()
    requested_cwd = payload.get("working_directory", BASE_DIRECTORY)
    if not isinstance(requested_cwd, str) or not requested_cwd.strip():
        return web.json_response(
            {"error": "Field 'working_directory' must be a non-empty string."},
            status=400,
        )

    try:
        session = create_session(requested_cwd, asyncio.get_running_loop())
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    SESSIONS[session.session_id] = session

    print("")
    print("=" * 80)
    print(f"[{session.created_at}] created session {session.session_id}")
    print(f"cwd: {session.working_directory}")
    print(f"shell: {session.shell_path}")
    print("=" * 80)

    return web.json_response(
        {
            "session_id": session.session_id,
            "status": session.status,
            "working_directory": session.working_directory,
            "shell": session.shell_path,
            "created_at": session.created_at,
        },
        status=201,
    )


async def websocket_handler(request: web.Request) -> web.StreamResponse:
    session_id = request.match_info["session_id"]
    session = SESSIONS.get(session_id)
    if session is None:
        return web.json_response({"error": f"Unknown session: {session_id}"}, status=404)
    if session.websocket_attached:
        return web.json_response(
            {"error": "A client is already attached to this session."},
            status=409,
        )

    ws = web.WebSocketResponse()
    await ws.prepare(request)
    session.websocket_attached = True
    print(f"[{utc_now_iso()}] websocket attached for session {session_id}")

    async def sender() -> None:
        while True:
            event = await session.output_queue.get()
            await ws.send_str(json.dumps(event))
            if event["type"] == "exit":
                break

    sender_task = asyncio.create_task(sender())

    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue

            try:
                payload = json.loads(msg.data)
            except json.JSONDecodeError:
                await ws.send_str(
                    json.dumps({"type": "error", "message": "Invalid JSON message."})
                )
                continue

            if payload.get("type") != "input" or not isinstance(payload.get("data"), str):
                await ws.send_str(
                    json.dumps(
                        {
                            "type": "error",
                            "message": "Expected {'type': 'input', 'data': string}.",
                        }
                    )
                )
                continue

            try:
                session.write_input(payload["data"])
            except RuntimeError as exc:
                await ws.send_str(json.dumps({"type": "error", "message": str(exc)}))
                break

            print(f"[{utc_now_iso()}] input for session {session_id}: {payload['data']!r}")
    finally:
        session.websocket_attached = False
        sender_task.cancel()
        try:
            await sender_task
        except asyncio.CancelledError:
            pass
        print(f"[{utc_now_iso()}] websocket detached for session {session_id}")

    return ws


async def delete_session_handler(request: web.Request) -> web.Response:
    session_id = request.match_info["session_id"]
    session = SESSIONS.pop(session_id, None)
    if session is None:
        return web.json_response({"error": f"Unknown session: {session_id}"}, status=404)

    session.terminate()
    print(f"[{utc_now_iso()}] deleted session {session_id}")
    return web.json_response(
        {
            "session_id": session_id,
            "status": session.status,
            "exit_code": session.exit_code,
        }
    )


async def shutdown_all_sessions(_: web.Application) -> None:
    sessions = list(SESSIONS.values())
    SESSIONS.clear()
    for session in sessions:
        session.terminate()


def main() -> None:
    app = web.Application()
    app.router.add_post("/sessions", create_session_handler)
    app.router.add_get("/sessions/{session_id}/ws", websocket_handler)
    app.router.add_delete("/sessions/{session_id}", delete_session_handler)
    app.on_shutdown.append(shutdown_all_sessions)

    print("Persistent remote shell demo server")
    print(f"Listening on http://{HOST}:{PORT}")
    print(f"Base directory: {BASE_DIRECTORY}")
    print("Routes: POST /sessions, GET /sessions/{id}/ws, DELETE /sessions/{id}")
    print("Press Ctrl+C to stop.")
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
