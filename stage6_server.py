#!/usr/bin/env python3
import asyncio
import json
import os
import pty
import re
import signal
import subprocess
import termios
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import WSMsgType, web


HOST = "127.0.0.1"
PORT = 8005
BASE_DIRECTORY = os.getcwd()
DATA_ROOT = os.path.join(BASE_DIRECTORY, ".stage6_data")
USERS_ROOT = os.path.join(DATA_ROOT, "users")
DEFAULT_SHELL = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
OUTPUT_READ_SIZE = 4096
DEFAULT_OWNER_ID = "demo-user"
DEFAULT_WORKSPACE_NAME = "demo-workspace"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip("-")
    return slug or "default"


def validate_base_directory(requested_cwd: str) -> str:
    safe_cwd = os.path.abspath(requested_cwd)
    if not safe_cwd.startswith(BASE_DIRECTORY):
        raise ValueError(
            "working_directory must stay inside the demo base directory: "
            f"{BASE_DIRECTORY}"
        )
    if not os.path.isdir(safe_cwd):
        raise ValueError(f"working_directory does not exist: {safe_cwd}")
    return safe_cwd


def resolve_project_path(project_root: str, requested_path: str | None) -> tuple[str, str]:
    relative_path = requested_path or "."
    target_path = os.path.abspath(os.path.join(project_root, relative_path))
    if target_path != project_root and not target_path.startswith(f"{project_root}{os.sep}"):
        raise ValueError("path must stay inside the project root")
    normalized_relative = os.path.relpath(target_path, project_root)
    return target_path, normalized_relative


def run_command(command: list[str], cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def get_storage_paths(owner_id: str, workspace_name: str) -> tuple[str, str]:
    owner_slug = slugify(owner_id)
    workspace_slug = slugify(workspace_name)
    user_root = os.path.join(USERS_ROOT, owner_slug)
    home_root = os.path.join(user_root, "home")
    project_root = os.path.join(user_root, "workspaces", workspace_slug, "project")
    return home_root, project_root


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
    base_directory: str
    shell_path: str
    created_at: str
    loop: asyncio.AbstractEventLoop
    home_root: str
    project_root: str
    repo_url: str | None = None
    branch: str | None = None
    commit: str | None = None
    bootstrap_status: str = "not_requested"
    status: str = "starting"
    last_activity_at: str = field(default_factory=utc_now_iso)
    exit_code: int | None = None
    last_error: str | None = None
    runtime: ShellRuntime | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def shell_working_directory(self) -> str:
        return self.project_root

    def mark_ready(self, runtime: ShellRuntime) -> None:
        self.runtime = runtime
        self.status = "ready"
        self.last_activity_at = utc_now_iso()

    def mark_activity(self) -> None:
        with self.lock:
            self.last_activity_at = utc_now_iso()

    def mark_error(self, message: str) -> None:
        self.status = "error"
        self.last_error = message
        if self.repo_url:
            self.bootstrap_status = "failed"

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
            "base_directory": self.base_directory,
            "home_root": self.home_root,
            "project_root": self.project_root,
            "shell_working_directory": self.shell_working_directory(),
            "shell": self.shell_path,
            "created_at": self.created_at,
            "last_activity_at": self.last_activity_at,
            "exit_code": self.exit_code,
            "last_error": self.last_error,
            "repo_url": self.repo_url,
            "branch": self.branch,
            "commit": self.commit,
            "bootstrap_status": self.bootstrap_status,
            "storage_persistent": True,
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


def create_shell_runtime(
    loop: asyncio.AbstractEventLoop,
    project_root: str,
    home_root: str,
) -> ShellRuntime:
    master_fd, slave_fd = pty.openpty()
    tty_attrs = termios.tcgetattr(slave_fd)
    tty_attrs[3] &= ~termios.ECHO
    termios.tcsetattr(slave_fd, termios.TCSANOW, tty_attrs)

    env = os.environ.copy()
    env["TERM"] = "dumb"
    env["PS1"] = ""
    env["HOME"] = home_root
    env["BASH_SILENCE_DEPRECATION_WARNING"] = "1"

    shell_command = [DEFAULT_SHELL, "--noprofile", "--norc", "-i"]
    if DEFAULT_SHELL.endswith("/sh") and not DEFAULT_SHELL.endswith("/bash"):
        shell_command = [DEFAULT_SHELL, "-i"]

    process = subprocess.Popen(
        shell_command,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=project_root,
        env=env,
        start_new_session=True,
        close_fds=True,
    )
    os.close(slave_fd)
    return ShellRuntime(process=process, master_fd=master_fd, loop=loop)


def detect_existing_repo(project_root: str) -> bool:
    return os.path.isdir(os.path.join(project_root, ".git"))


def get_existing_repo_url(project_root: str) -> str | None:
    result = run_command(["git", "-C", project_root, "config", "--get", "remote.origin.url"])
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def get_current_commit(project_root: str) -> str | None:
    result = run_command(["git", "-C", project_root, "rev-parse", "HEAD"])
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def get_current_branch(project_root: str) -> str | None:
    result = run_command(["git", "-C", project_root, "rev-parse", "--abbrev-ref", "HEAD"])
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    if branch == "HEAD":
        return None
    return branch or None


def bootstrap_or_reuse_project(workspace: Workspace) -> None:
    os.makedirs(workspace.home_root, exist_ok=True)
    os.makedirs(os.path.dirname(workspace.project_root), exist_ok=True)

    existing_repo = detect_existing_repo(workspace.project_root)

    if existing_repo:
        actual_repo_url = get_existing_repo_url(workspace.project_root)
        if workspace.repo_url and actual_repo_url and workspace.repo_url != actual_repo_url:
            raise RuntimeError(
                "Persistent project root already contains a different repo: "
                f"{actual_repo_url}"
            )

        workspace.repo_url = workspace.repo_url or actual_repo_url
        workspace.bootstrap_status = "reused"

        if workspace.commit:
            checkout_result = run_command(["git", "-C", workspace.project_root, "checkout", workspace.commit])
            if checkout_result.returncode != 0:
                raise RuntimeError(
                    checkout_result.stderr.strip()
                    or checkout_result.stdout.strip()
                    or "git checkout commit failed"
                )
        elif workspace.branch:
            checkout_result = run_command(["git", "-C", workspace.project_root, "checkout", workspace.branch])
            if checkout_result.returncode != 0:
                raise RuntimeError(
                    checkout_result.stderr.strip()
                    or checkout_result.stdout.strip()
                    or "git checkout branch failed"
                )
        else:
            workspace.branch = get_current_branch(workspace.project_root)

        workspace.commit = get_current_commit(workspace.project_root)
        if not workspace.branch:
            workspace.branch = get_current_branch(workspace.project_root)
        return

    if workspace.repo_url:
        if os.path.exists(workspace.project_root):
            existing_entries = [entry for entry in os.scandir(workspace.project_root)]
            if existing_entries:
                raise RuntimeError(
                    "Persistent project root already has files and cannot be used for initial clone."
                )
        workspace.status = "cloning"
        workspace.bootstrap_status = "cloning"
        clone_result = run_command(["git", "clone", workspace.repo_url, workspace.project_root])
        if clone_result.returncode != 0:
            raise RuntimeError(
                clone_result.stderr.strip()
                or clone_result.stdout.strip()
                or "git clone failed"
            )

        if workspace.commit:
            checkout_result = run_command(["git", "-C", workspace.project_root, "checkout", workspace.commit])
            if checkout_result.returncode != 0:
                raise RuntimeError(
                    checkout_result.stderr.strip()
                    or checkout_result.stdout.strip()
                    or "git checkout commit failed"
                )
        elif workspace.branch:
            checkout_result = run_command(["git", "-C", workspace.project_root, "checkout", workspace.branch])
            if checkout_result.returncode != 0:
                raise RuntimeError(
                    checkout_result.stderr.strip()
                    or checkout_result.stdout.strip()
                    or "git checkout branch failed"
                )

        workspace.commit = get_current_commit(workspace.project_root)
        if not workspace.branch:
            workspace.branch = get_current_branch(workspace.project_root)
        workspace.bootstrap_status = "checked_out"
        return

    os.makedirs(workspace.project_root, exist_ok=True)
    workspace.bootstrap_status = "not_requested"


def create_workspace(
    loop: asyncio.AbstractEventLoop,
    owner_id: str,
    name: str,
    requested_cwd: str,
    repo_url: str | None,
    branch: str | None,
    commit: str | None,
) -> Workspace:
    base_directory = validate_base_directory(requested_cwd)
    home_root, project_root = get_storage_paths(owner_id, name)
    workspace = Workspace(
        workspace_id=str(uuid.uuid4()),
        owner_id=owner_id,
        name=name,
        base_directory=base_directory,
        shell_path=DEFAULT_SHELL,
        created_at=utc_now_iso(),
        loop=loop,
        home_root=home_root,
        project_root=project_root,
        repo_url=repo_url,
        branch=branch,
        commit=commit,
    )

    bootstrap_or_reuse_project(workspace)

    runtime = create_shell_runtime(loop, workspace.project_root, workspace.home_root)
    workspace.mark_ready(runtime)

    reader = threading.Thread(
        target=workspace_reader,
        args=(workspace,),
        name=f"workspace-reader-{workspace.workspace_id}",
        daemon=True,
    )
    reader.start()
    return workspace


def get_workspace_or_404(workspace_id: str) -> Workspace:
    workspace = WORKSPACES.get(workspace_id)
    if workspace is None:
        raise web.HTTPNotFound(
            text=json.dumps({"error": f"Unknown workspace: {workspace_id}"}),
            content_type="application/json",
        )
    return workspace


def list_directory_entries(target_path: str, project_root: str) -> list[dict]:
    entries = []
    for entry in sorted(
        os.scandir(target_path),
        key=lambda item: (not item.is_dir(), item.name.lower()),
    ):
        entry_path = os.path.abspath(entry.path)
        relative_path = os.path.relpath(entry_path, project_root)
        stat_result = entry.stat()
        entries.append(
            {
                "name": entry.name,
                "path": "." if relative_path == "." else relative_path,
                "type": "directory" if entry.is_dir() else "file",
                "size_bytes": stat_result.st_size,
                "modified_at": datetime.fromtimestamp(
                    stat_result.st_mtime,
                    tz=timezone.utc,
                ).isoformat(),
            }
        )
    return entries


async def create_workspace_handler(request: web.Request) -> web.Response:
    payload = await request.json()
    requested_cwd = payload.get("working_directory", BASE_DIRECTORY)
    owner_id = payload.get("owner_id", DEFAULT_OWNER_ID)
    name = payload.get("name", DEFAULT_WORKSPACE_NAME)
    repo_url = payload.get("repo_url")
    branch = payload.get("branch")
    commit = payload.get("commit")

    if not isinstance(requested_cwd, str) or not requested_cwd.strip():
        return web.json_response({"error": "Field 'working_directory' must be a non-empty string."}, status=400)
    if not isinstance(owner_id, str) or not owner_id.strip():
        return web.json_response({"error": "Field 'owner_id' must be a non-empty string."}, status=400)
    if not isinstance(name, str) or not name.strip():
        return web.json_response({"error": "Field 'name' must be a non-empty string."}, status=400)
    if repo_url is not None and (not isinstance(repo_url, str) or not repo_url.strip()):
        return web.json_response({"error": "Field 'repo_url' must be a non-empty string when provided."}, status=400)
    if branch is not None and (not isinstance(branch, str) or not branch.strip()):
        return web.json_response({"error": "Field 'branch' must be a non-empty string when provided."}, status=400)
    if commit is not None and (not isinstance(commit, str) or not commit.strip()):
        return web.json_response({"error": "Field 'commit' must be a non-empty string when provided."}, status=400)

    try:
        workspace = create_workspace(
            asyncio.get_running_loop(),
            owner_id=owner_id.strip(),
            name=name.strip(),
            requested_cwd=requested_cwd,
            repo_url=repo_url.strip() if isinstance(repo_url, str) else None,
            branch=branch.strip() if isinstance(branch, str) else None,
            commit=commit.strip() if isinstance(commit, str) else None,
        )
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except RuntimeError as exc:
        home_root, project_root = get_storage_paths(owner_id.strip(), name.strip())
        failed_workspace = Workspace(
            workspace_id=str(uuid.uuid4()),
            owner_id=owner_id.strip(),
            name=name.strip(),
            base_directory=os.path.abspath(requested_cwd),
            shell_path=DEFAULT_SHELL,
            created_at=utc_now_iso(),
            loop=asyncio.get_running_loop(),
            home_root=home_root,
            project_root=project_root,
            repo_url=repo_url.strip() if isinstance(repo_url, str) else None,
            branch=branch.strip() if isinstance(branch, str) else None,
            commit=commit.strip() if isinstance(commit, str) else None,
        )
        failed_workspace.mark_error(str(exc))
        WORKSPACES[failed_workspace.workspace_id] = failed_workspace
        return web.json_response(failed_workspace.to_dict(), status=201)

    WORKSPACES[workspace.workspace_id] = workspace

    print("")
    print("=" * 80)
    print(f"[{workspace.created_at}] created workspace {workspace.workspace_id}")
    print(f"owner: {workspace.owner_id}")
    print(f"name: {workspace.name}")
    print(f"base_directory: {workspace.base_directory}")
    print(f"home_root: {workspace.home_root}")
    print(f"project_root: {workspace.project_root}")
    print(f"repo_url: {workspace.repo_url}")
    print(f"branch: {workspace.branch}")
    print(f"commit: {workspace.commit}")
    print(f"bootstrap_status: {workspace.bootstrap_status}")
    print("=" * 80)

    return web.json_response(workspace.to_dict(), status=201)


async def list_workspaces_handler(_: web.Request) -> web.Response:
    return web.json_response({"workspaces": [workspace.to_dict() for workspace in WORKSPACES.values()]})


async def get_workspace_handler(request: web.Request) -> web.Response:
    workspace = get_workspace_or_404(request.match_info["workspace_id"])
    return web.json_response(workspace.to_dict())


async def list_files_handler(request: web.Request) -> web.Response:
    workspace = get_workspace_or_404(request.match_info["workspace_id"])
    requested_path = request.query.get("path", ".")

    try:
        target_path, normalized_relative = resolve_project_path(workspace.project_root, requested_path)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    if not os.path.exists(target_path):
        return web.json_response({"error": f"path does not exist: {requested_path}"}, status=404)
    if not os.path.isdir(target_path):
        return web.json_response({"error": f"path is not a directory: {requested_path}"}, status=400)

    workspace.mark_activity()
    return web.json_response(
        {
            "workspace_id": workspace.workspace_id,
            "path": normalized_relative,
            "entries": list_directory_entries(target_path, workspace.project_root),
        }
    )


async def read_file_handler(request: web.Request) -> web.Response:
    workspace = get_workspace_or_404(request.match_info["workspace_id"])
    requested_path = request.query.get("path")
    if not requested_path:
        return web.json_response({"error": "Query parameter 'path' is required."}, status=400)

    try:
        target_path, normalized_relative = resolve_project_path(workspace.project_root, requested_path)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    if not os.path.exists(target_path):
        return web.json_response({"error": f"file does not exist: {requested_path}"}, status=404)
    if not os.path.isfile(target_path):
        return web.json_response({"error": f"path is not a file: {requested_path}"}, status=400)

    try:
        content = Path(target_path).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return web.json_response({"error": "file is not valid UTF-8 text"}, status=400)

    workspace.mark_activity()
    return web.json_response(
        {
            "workspace_id": workspace.workspace_id,
            "path": normalized_relative,
            "content": content,
        }
    )


async def write_file_handler(request: web.Request) -> web.Response:
    workspace = get_workspace_or_404(request.match_info["workspace_id"])
    payload = await request.json()
    requested_path = payload.get("path")
    content = payload.get("content", "")

    if not isinstance(requested_path, str) or not requested_path.strip():
        return web.json_response({"error": "Field 'path' must be a non-empty string."}, status=400)
    if not isinstance(content, str):
        return web.json_response({"error": "Field 'content' must be a string."}, status=400)

    try:
        target_path, normalized_relative = resolve_project_path(workspace.project_root, requested_path)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    Path(target_path).write_text(content, encoding="utf-8")
    workspace.mark_activity()
    return web.json_response(
        {
            "workspace_id": workspace.workspace_id,
            "path": normalized_relative,
            "bytes_written": len(content.encode("utf-8")),
        }
    )


async def create_directory_handler(request: web.Request) -> web.Response:
    workspace = get_workspace_or_404(request.match_info["workspace_id"])
    payload = await request.json()
    requested_path = payload.get("path")

    if not isinstance(requested_path, str) or not requested_path.strip():
        return web.json_response({"error": "Field 'path' must be a non-empty string."}, status=400)

    try:
        target_path, normalized_relative = resolve_project_path(workspace.project_root, requested_path)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    os.makedirs(target_path, exist_ok=True)
    workspace.mark_activity()
    return web.json_response(
        {
            "workspace_id": workspace.workspace_id,
            "path": normalized_relative,
            "status": "created",
        },
        status=201,
    )


async def workspace_websocket_handler(request: web.Request) -> web.StreamResponse:
    workspace = get_workspace_or_404(request.match_info["workspace_id"])
    workspace.refresh_status()
    if workspace.runtime is None:
        return web.json_response({"error": "Workspace has no runtime attached."}, status=409)
    if workspace.runtime.websocket_attached:
        return web.json_response({"error": "A client is already attached to this workspace."}, status=409)

    ws = web.WebSocketResponse()
    await ws.prepare(request)
    workspace.runtime.websocket_attached = True
    print(f"[{utc_now_iso()}] websocket attached for workspace {workspace.workspace_id}")

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
                await ws.send_str(json.dumps({"type": "error", "message": "Invalid JSON message."}))
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

            print(f"[{utc_now_iso()}] input for workspace {workspace.workspace_id}: {payload['data']!r}")
    finally:
        workspace.runtime.websocket_attached = False
        sender_task.cancel()
        try:
            await sender_task
        except asyncio.CancelledError:
            pass
        print(f"[{utc_now_iso()}] websocket detached for workspace {workspace.workspace_id}")

    return ws


async def delete_workspace_handler(request: web.Request) -> web.Response:
    workspace_id = request.match_info["workspace_id"]
    workspace = WORKSPACES.pop(workspace_id, None)
    if workspace is None:
        return web.json_response({"error": f"Unknown workspace: {workspace_id}"}, status=404)

    workspace.status = "stopping"
    if workspace.runtime is not None:
        workspace.exit_code = workspace.runtime.terminate()
    workspace.status = "deleted"
    workspace.last_activity_at = utc_now_iso()
    print(f"[{utc_now_iso()}] deleted workspace {workspace_id} (storage retained)")
    return web.json_response(workspace.to_dict())


async def shutdown_all_workspaces(_: web.Application) -> None:
    workspaces = list(WORKSPACES.values())
    WORKSPACES.clear()
    for workspace in workspaces:
        if workspace.runtime is not None:
            workspace.runtime.terminate()
        workspace.status = "deleted"


def main() -> None:
    os.makedirs(USERS_ROOT, exist_ok=True)

    app = web.Application()
    app.router.add_post("/workspaces", create_workspace_handler)
    app.router.add_get("/workspaces", list_workspaces_handler)
    app.router.add_get("/workspaces/{workspace_id}", get_workspace_handler)
    app.router.add_get("/workspaces/{workspace_id}/files", list_files_handler)
    app.router.add_get("/workspaces/{workspace_id}/file", read_file_handler)
    app.router.add_put("/workspaces/{workspace_id}/file", write_file_handler)
    app.router.add_post("/workspaces/{workspace_id}/directories", create_directory_handler)
    app.router.add_get("/workspaces/{workspace_id}/ws", workspace_websocket_handler)
    app.router.add_delete("/workspaces/{workspace_id}", delete_workspace_handler)
    app.on_shutdown.append(shutdown_all_workspaces)

    print("Persistent home + persistent project demo server")
    print(f"Listening on http://{HOST}:{PORT}")
    print(f"Base directory: {BASE_DIRECTORY}")
    print(f"Persistent data root: {DATA_ROOT}")
    print(
        "Routes: POST /workspaces, GET /workspaces, GET /workspaces/{id}, "
        "GET /workspaces/{id}/files, GET /workspaces/{id}/file, "
        "PUT /workspaces/{id}/file, POST /workspaces/{id}/directories, "
        "GET /workspaces/{id}/ws, DELETE /workspaces/{id}"
    )
    print("Press Ctrl+C to stop.")
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
