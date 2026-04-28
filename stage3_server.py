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
PORT = 8002
BASE_DIRECTORY = os.getcwd()
DEFAULT_SHELL = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
OUTPUT_READ_SIZE = 4096
DEFAULT_OWNER_ID = "demo-user"


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
class ShellRuntime:
    process: subprocess.Popen[bytes]
    master_fd: int
    loop: asyncio.AbstractEventLoop
    output_queue: asyncio.Queue[dict] = field(default_factory=asyncio.Queue)
    closed: bool = False
    websocket_attached: bool = False

    def publish_output(self, text: str) -> None:
        if not text:
            return
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
        if self.closed:
            raise RuntimeError("workspace runtime is no longer running")
        os.write(self.master_fd, data.encode("utf-8"))

    def terminate(self) -> int:
        if self.closed:
            return self.process.poll() or 0
        self.process.terminate()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=1)
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        self.closed = True
        exit_code = self.process.poll()
        if exit_code is None:
            exit_code = -signal.SIGTERM
        self.publish_exit(exit_code)
        return exit_code


@dataclass
class Workspace:
    workspace_id: str
    owner_id: str
    name: str
    working_directory: str
    shell_path: str
    created_at: str
    loop: asyncio.AbstractEventLoop
    status: str = "starting"
    last_activity_at: str = field(default_factory=utc_now_iso)
    exit_code: int | None = None
    last_error: str | None = None
    runtime: ShellRuntime | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def mark_ready(self, runtime: ShellRuntime) -> None:
        self.runtime = runtime
        self.status = "ready"
        self.last_activity_at = utc_now_iso()

    def mark_activity(self) -> None:
        with self.lock:
            self.last_activity_at = utc_now_iso()

    def refresh_status(self) -> None:
        if self.runtime is None or self.runtime.closed:
            return
        exit_code = self.runtime.process.poll()
        if exit_code is not None:
            self.exit_code = exit_code
            self.runtime.closed = True
            self.status = "error" if exit_code != 0 else "stopped"
            self.last_error = None if exit_code == 0 else f"Shell exited with code {exit_code}"

    def to_dict(self) -> dict:
        self.refresh_status()
        return {
            "workspace_id": self.workspace_id,
            "owner_id": self.owner_id,
            "name": self.name,
            "status": self.status,
            "working_directory": self.working_directory,
            "shell": self.shell_path,
            "created_at": self.created_at,
            "last_activity_at": self.last_activity_at,
            "exit_code": self.exit_code,
            "last_error": self.last_error,
        }


WORKSPACES: dict[str, Workspace] = {}


def workspace_reader(workspace: Workspace) -> None:
    runtime = workspace.runtime
    if runtime is None:
        return

    while True:
        try:
            raw = os.read(runtime.master_fd, OUTPUT_READ_SIZE)
        except OSError:
            break
        if not raw:
            break
        workspace.mark_activity()
        runtime.publish_output(raw.decode("utf-8", errors="replace"))

    exit_code = runtime.process.wait()
    workspace.exit_code = exit_code
    workspace.status = "error" if exit_code != 0 else "stopped"
    workspace.last_error = None if exit_code == 0 else f"Shell exited with code {exit_code}"
    runtime.closed = True
    try:
        os.close(runtime.master_fd)
    except OSError:
        pass
    runtime.publish_exit(exit_code)


def create_shell_runtime(loop: asyncio.AbstractEventLoop, working_directory: str) -> ShellRuntime:
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
        cwd=working_directory,
        env=env,
        start_new_session=True,
        close_fds=True,
    )
    os.close(slave_fd)
    return ShellRuntime(process=process, master_fd=master_fd, loop=loop)


def create_workspace(
    loop: asyncio.AbstractEventLoop,
    owner_id: str,
    name: str,
    requested_cwd: str,
) -> Workspace:
    safe_cwd = validate_working_directory(requested_cwd)
    workspace = Workspace(
        workspace_id=str(uuid.uuid4()),
        owner_id=owner_id,
        name=name,
        working_directory=safe_cwd,
        shell_path=DEFAULT_SHELL,
        created_at=utc_now_iso(),
        loop=loop,
    )

    runtime = create_shell_runtime(loop, safe_cwd)
    workspace.mark_ready(runtime)

    reader = threading.Thread(
        target=workspace_reader,
        args=(workspace,),
        name=f"workspace-reader-{workspace.workspace_id}",
        daemon=True,
    )
    reader.start()
    return workspace


async def create_workspace_handler(request: web.Request) -> web.Response:
    payload = await request.json()
    requested_cwd = payload.get("working_directory", BASE_DIRECTORY)
    owner_id = payload.get("owner_id", DEFAULT_OWNER_ID)
    name = payload.get("name", "demo-workspace")

    if not isinstance(requested_cwd, str) or not requested_cwd.strip():
        return web.json_response(
            {"error": "Field 'working_directory' must be a non-empty string."},
            status=400,
        )
    if not isinstance(owner_id, str) or not owner_id.strip():
        return web.json_response(
            {"error": "Field 'owner_id' must be a non-empty string."},
            status=400,
        )
    if not isinstance(name, str) or not name.strip():
        return web.json_response(
            {"error": "Field 'name' must be a non-empty string."},
            status=400,
        )

    try:
        workspace = create_workspace(
            asyncio.get_running_loop(),
            owner_id=owner_id.strip(),
            name=name.strip(),
            requested_cwd=requested_cwd,
        )
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    WORKSPACES[workspace.workspace_id] = workspace

    print("")
    print("=" * 80)
    print(f"[{workspace.created_at}] created workspace {workspace.workspace_id}")
    print(f"owner: {workspace.owner_id}")
    print(f"name: {workspace.name}")
    print(f"cwd: {workspace.working_directory}")
    print(f"shell: {workspace.shell_path}")
    print("=" * 80)

    return web.json_response(workspace.to_dict(), status=201)


async def list_workspaces_handler(_: web.Request) -> web.Response:
    return web.json_response(
        {"workspaces": [workspace.to_dict() for workspace in WORKSPACES.values()]}
    )


async def get_workspace_handler(request: web.Request) -> web.Response:
    workspace_id = request.match_info["workspace_id"]
    workspace = WORKSPACES.get(workspace_id)
    if workspace is None:
        return web.json_response(
            {"error": f"Unknown workspace: {workspace_id}"},
            status=404,
        )
    return web.json_response(workspace.to_dict())


async def workspace_websocket_handler(request: web.Request) -> web.StreamResponse:
    workspace_id = request.match_info["workspace_id"]
    workspace = WORKSPACES.get(workspace_id)
    if workspace is None:
        return web.json_response(
            {"error": f"Unknown workspace: {workspace_id}"},
            status=404,
        )
    workspace.refresh_status()
    if workspace.runtime is None:
        return web.json_response(
            {"error": "Workspace has no runtime attached."},
            status=409,
        )
    if workspace.runtime.websocket_attached:
        return web.json_response(
            {"error": "A client is already attached to this workspace."},
            status=409,
        )

    ws = web.WebSocketResponse()
    await ws.prepare(request)
    workspace.runtime.websocket_attached = True
    print(f"[{utc_now_iso()}] websocket attached for workspace {workspace_id}")

    async def sender() -> None:
        assert workspace.runtime is not None
        while True:
            event = await workspace.runtime.output_queue.get()
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
                workspace.runtime.write_input(payload["data"])
                workspace.mark_activity()
            except RuntimeError as exc:
                await ws.send_str(json.dumps({"type": "error", "message": str(exc)}))
                break

            print(f"[{utc_now_iso()}] input for workspace {workspace_id}: {payload['data']!r}")
    finally:
        workspace.runtime.websocket_attached = False
        sender_task.cancel()
        try:
            await sender_task
        except asyncio.CancelledError:
            pass
        print(f"[{utc_now_iso()}] websocket detached for workspace {workspace_id}")

    return ws


async def delete_workspace_handler(request: web.Request) -> web.Response:
    workspace_id = request.match_info["workspace_id"]
    workspace = WORKSPACES.pop(workspace_id, None)
    if workspace is None:
        return web.json_response(
            {"error": f"Unknown workspace: {workspace_id}"},
            status=404,
        )

    workspace.status = "stopping"
    if workspace.runtime is not None:
        workspace.exit_code = workspace.runtime.terminate()
    workspace.status = "deleted"
    workspace.last_activity_at = utc_now_iso()
    print(f"[{utc_now_iso()}] deleted workspace {workspace_id}")
    return web.json_response(workspace.to_dict())


async def shutdown_all_workspaces(_: web.Application) -> None:
    workspaces = list(WORKSPACES.values())
    WORKSPACES.clear()
    for workspace in workspaces:
        if workspace.runtime is not None:
            workspace.runtime.terminate()
        workspace.status = "deleted"


def main() -> None:
    app = web.Application()
    app.router.add_post("/workspaces", create_workspace_handler)
    app.router.add_get("/workspaces", list_workspaces_handler)
    app.router.add_get("/workspaces/{workspace_id}", get_workspace_handler)
    app.router.add_get("/workspaces/{workspace_id}/ws", workspace_websocket_handler)
    app.router.add_delete("/workspaces/{workspace_id}", delete_workspace_handler)
    app.on_shutdown.append(shutdown_all_workspaces)

    print("Workspace-based remote shell demo server")
    print(f"Listening on http://{HOST}:{PORT}")
    print(f"Base directory: {BASE_DIRECTORY}")
    print("Routes: POST /workspaces, GET /workspaces, GET /workspaces/{id}, GET /workspaces/{id}/ws, DELETE /workspaces/{id}")
    print("Press Ctrl+C to stop.")
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
