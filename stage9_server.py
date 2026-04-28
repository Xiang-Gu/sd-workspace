#!/usr/bin/env python3
import asyncio
import json
import os
import pty
import re
import shutil
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
PORT = 8008
BASE_DIRECTORY = os.getcwd()
DATA_ROOT = os.path.join(BASE_DIRECTORY, ".stage9_data")
USERS_ROOT = os.path.join(DATA_ROOT, "users")
EPHEMERAL_ROOT = os.path.join(DATA_ROOT, "ephemeral", "workspaces")
DOCKER_BIN = "docker"
DEFAULT_CONTAINER_IMAGE = "alpine:3.20"
CONTAINER_HOME = "/home/workspace-user"
CONTAINER_WORKSPACE = "/workspace"
DEFAULT_SHELL = "/bin/sh"
OUTPUT_READ_SIZE = 4096
DEFAULT_OWNER_ID = "demo-user"
DEFAULT_WORKSPACE_NAME = "demo-workspace"
DEFAULT_IDLE_TIMEOUT_SECONDS = 30
IDLE_SWEEP_INTERVAL_SECONDS = 5


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def parse_utc_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


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


def get_home_root(owner_id: str) -> str:
    owner_slug = slugify(owner_id)
    return os.path.join(USERS_ROOT, owner_slug, "home")


def get_project_root(workspace_id: str) -> str:
    return os.path.join(EPHEMERAL_ROOT, workspace_id, "project")


def get_container_name(workspace_id: str) -> str:
    return f"stage9-workspace-{workspace_id}"


@dataclass
class ShellRuntime:
    process: subprocess.Popen[bytes]
    master_fd: int
    loop: asyncio.AbstractEventLoop
    container_name: str
    container_image: str
    output_queue: asyncio.Queue[dict] = field(default_factory=asyncio.Queue)
    closed: bool = False
    websocket_attached: bool = False
    shutdown_reason: str | None = None

    def publish_event(self, event: dict) -> None:
        self.loop.call_soon_threadsafe(self.output_queue.put_nowait, event)

    def publish_output(self, text: str) -> None:
        if text:
            self.publish_event({"type": "output", "data": text})

    def publish_exit(self, exit_code: int) -> None:
        self.publish_event({"type": "exit", "exit_code": exit_code})

    def publish_suspended(self, reason: str) -> None:
        self.publish_event({"type": "suspended", "reason": reason})

    def write_input(self, data: str) -> None:
        if self.closed:
            raise RuntimeError("workspace runtime is no longer running")
        os.write(self.master_fd, data.encode("utf-8"))

    def terminate(self, reason: str) -> int:
        if self.closed:
            return self.process.poll() or 0

        self.shutdown_reason = reason
        if reason == "suspend":
            self.publish_suspended("manual_or_idle")

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
        run_command([DOCKER_BIN, "rm", "-f", self.container_name])
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
    idle_timeout_seconds: int
    container_image: str = DEFAULT_CONTAINER_IMAGE
    repo_url: str | None = None
    branch: str | None = None
    commit: str | None = None
    bootstrap_status: str = "not_requested"
    status: str = "starting"
    last_activity_at: str = field(default_factory=utc_now_iso)
    suspended_at: str | None = None
    exit_code: int | None = None
    last_error: str | None = None
    runtime: ShellRuntime | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def shell_working_directory(self) -> str:
        return CONTAINER_WORKSPACE

    def mark_ready(self, runtime: ShellRuntime) -> None:
        self.runtime = runtime
        self.status = "ready"
        self.suspended_at = None
        self.last_error = None
        self.last_activity_at = utc_now_iso()

    def mark_activity(self) -> None:
        with self.lock:
            self.last_activity_at = utc_now_iso()

    def mark_error(self, message: str) -> None:
        self.status = "error"
        self.last_error = message
        if self.repo_url:
            self.bootstrap_status = "failed"

    def ready_for_interaction(self) -> bool:
        return self.status == "ready" and self.runtime is not None and not self.runtime.closed

    def websocket_attached(self) -> bool:
        return self.runtime is not None and self.runtime.websocket_attached

    def should_suspend_for_idle(self, now: datetime) -> bool:
        if self.status != "ready" or self.runtime is None or self.runtime.closed:
            return False
        if self.runtime.websocket_attached:
            return False
        last_activity = parse_utc_timestamp(self.last_activity_at)
        if last_activity is None:
            return False
        idle_seconds = (now - last_activity).total_seconds()
        return idle_seconds >= self.idle_timeout_seconds

    def refresh_status(self) -> None:
        runtime = self.runtime
        if runtime is None or runtime.closed:
            return
        exit_code = runtime.process.poll()
        if exit_code is None:
            return
        self.exit_code = exit_code
        runtime.closed = True
        if runtime.shutdown_reason in {"suspend", "delete", "shutdown"}:
            return
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
            "runtime_kind": "container",
            "container_image": self.container_image,
            "container_name": get_container_name(self.workspace_id),
            "created_at": self.created_at,
            "last_activity_at": self.last_activity_at,
            "suspended_at": self.suspended_at,
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "exit_code": self.exit_code,
            "last_error": self.last_error,
            "repo_url": self.repo_url,
            "branch": self.branch,
            "commit": self.commit,
            "bootstrap_status": self.bootstrap_status,
            "home_persistent": True,
            "project_persistent": False,
            "runtime_attached": self.runtime is not None and not self.runtime.closed,
        }


WORKSPACES: dict[str, Workspace] = {}


def workspace_reader(workspace: Workspace, runtime: ShellRuntime) -> None:
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
    runtime.closed = True
    try:
        os.close(runtime.master_fd)
    except OSError:
        pass

    if runtime.shutdown_reason == "suspend":
        runtime.publish_exit(exit_code)
        return
    if runtime.shutdown_reason in {"delete", "shutdown"}:
        runtime.publish_exit(exit_code)
        return

    if workspace.runtime is runtime:
        workspace.status = "error" if exit_code != 0 else "stopped"
        workspace.last_error = None if exit_code == 0 else f"Shell exited with code {exit_code}"
        workspace.runtime = None
    runtime.publish_exit(exit_code)


def ensure_container_image(image: str) -> None:
    inspect_result = run_command([DOCKER_BIN, "image", "inspect", image])
    if inspect_result.returncode == 0:
        return

    pull_result = run_command([DOCKER_BIN, "pull", image])
    if pull_result.returncode != 0:
        raise RuntimeError(
            pull_result.stderr.strip()
            or pull_result.stdout.strip()
            or f"Unable to pull container image {image}"
        )


def create_shell_runtime(
    loop: asyncio.AbstractEventLoop,
    workspace: Workspace,
) -> ShellRuntime:
    ensure_container_image(workspace.container_image)
    master_fd, slave_fd = pty.openpty()
    tty_attrs = termios.tcgetattr(slave_fd)
    tty_attrs[3] &= ~termios.ECHO
    termios.tcsetattr(slave_fd, termios.TCSANOW, tty_attrs)

    env = os.environ.copy()
    env["TERM"] = "dumb"
    env["DOCKER_CLI_HINTS"] = "false"
    container_name = get_container_name(workspace.workspace_id)
    run_command([DOCKER_BIN, "rm", "-f", container_name])
    process = subprocess.Popen(
        [
            DOCKER_BIN,
            "run",
            "--rm",
            "--name",
            container_name,
            "-i",
            "-v",
            f"{workspace.home_root}:{CONTAINER_HOME}",
            "-v",
            f"{workspace.project_root}:{CONTAINER_WORKSPACE}",
            "-w",
            CONTAINER_WORKSPACE,
            "-e",
            f"HOME={CONTAINER_HOME}",
            "-e",
            "TERM=dumb",
            workspace.container_image,
            workspace.shell_path,
        ],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=BASE_DIRECTORY,
        env=env,
        start_new_session=True,
        close_fds=True,
    )
    os.close(slave_fd)
    return ShellRuntime(
        process=process,
        master_fd=master_fd,
        loop=loop,
        container_name=container_name,
        container_image=workspace.container_image,
    )


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


def cleanup_project_root(project_root: str) -> None:
    if os.path.isdir(project_root):
        shutil.rmtree(os.path.dirname(project_root), ignore_errors=True)


def bootstrap_project(workspace: Workspace) -> None:
    os.makedirs(workspace.home_root, exist_ok=True)
    cleanup_project_root(workspace.project_root)
    os.makedirs(workspace.project_root, exist_ok=True)

    if not workspace.repo_url:
        workspace.bootstrap_status = "not_requested"
        return

    workspace.status = "cloning"
    workspace.bootstrap_status = "cloning"
    clone_result = run_command(["git", "clone", workspace.repo_url, workspace.project_root])
    if clone_result.returncode != 0:
        cleanup_project_root(workspace.project_root)
        raise RuntimeError(
            clone_result.stderr.strip()
            or clone_result.stdout.strip()
            or "git clone failed"
        )

    if workspace.commit:
        checkout_result = run_command(["git", "-C", workspace.project_root, "checkout", workspace.commit])
        if checkout_result.returncode != 0:
            cleanup_project_root(workspace.project_root)
            raise RuntimeError(
                checkout_result.stderr.strip()
                or checkout_result.stdout.strip()
                or "git checkout commit failed"
            )
    elif workspace.branch:
        checkout_result = run_command(["git", "-C", workspace.project_root, "checkout", workspace.branch])
        if checkout_result.returncode != 0:
            cleanup_project_root(workspace.project_root)
            raise RuntimeError(
                checkout_result.stderr.strip()
                or checkout_result.stdout.strip()
                or "git checkout branch failed"
            )

    workspace.commit = get_current_commit(workspace.project_root)
    if not workspace.branch:
        workspace.branch = get_current_branch(workspace.project_root)
    workspace.bootstrap_status = "checked_out"


def attach_runtime(workspace: Workspace) -> None:
    runtime = create_shell_runtime(workspace.loop, workspace)
    workspace.mark_ready(runtime)
    reader = threading.Thread(
        target=workspace_reader,
        args=(workspace, runtime),
        name=f"workspace-reader-{workspace.workspace_id}",
        daemon=True,
    )
    reader.start()


def provision_workspace(workspace: Workspace, lifecycle_status: str) -> None:
    workspace.status = lifecycle_status
    workspace.last_error = None
    workspace.exit_code = None
    bootstrap_project(workspace)
    attach_runtime(workspace)


def suspend_workspace(workspace: Workspace) -> None:
    if workspace.status != "ready":
        raise ValueError(f"Workspace must be ready to suspend, current status is {workspace.status}.")

    workspace.status = "suspending"
    runtime = workspace.runtime
    if runtime is not None:
        workspace.exit_code = runtime.terminate("suspend")
    workspace.runtime = None
    cleanup_project_root(workspace.project_root)
    workspace.suspended_at = utc_now_iso()
    workspace.last_activity_at = workspace.suspended_at
    workspace.status = "suspended"


def resume_workspace(workspace: Workspace) -> None:
    if workspace.status != "suspended":
        raise ValueError(f"Workspace must be suspended to resume, current status is {workspace.status}.")

    workspace.status = "resuming"
    workspace.suspended_at = None
    provision_workspace(workspace, "resuming")


def delete_workspace(workspace: Workspace) -> None:
    workspace.status = "stopping"
    runtime = workspace.runtime
    if runtime is not None:
        workspace.exit_code = runtime.terminate("delete")
    workspace.runtime = None
    cleanup_project_root(workspace.project_root)
    workspace.status = "deleted"
    workspace.last_activity_at = utc_now_iso()


def create_workspace_record(
    loop: asyncio.AbstractEventLoop,
    owner_id: str,
    name: str,
    requested_cwd: str,
    repo_url: str | None,
    branch: str | None,
    commit: str | None,
    idle_timeout_seconds: int,
) -> Workspace:
    base_directory = validate_base_directory(requested_cwd)
    workspace_id = str(uuid.uuid4())
    return Workspace(
        workspace_id=workspace_id,
        owner_id=owner_id,
        name=name,
        base_directory=base_directory,
        shell_path=DEFAULT_SHELL,
        created_at=utc_now_iso(),
        loop=loop,
        home_root=get_home_root(owner_id),
        project_root=get_project_root(workspace_id),
        repo_url=repo_url,
        branch=branch,
        commit=commit,
        idle_timeout_seconds=idle_timeout_seconds,
    )


def get_workspace_or_404(workspace_id: str) -> Workspace:
    workspace = WORKSPACES.get(workspace_id)
    if workspace is None:
        raise web.HTTPNotFound(
            text=json.dumps({"error": f"Unknown workspace: {workspace_id}"}),
            content_type="application/json",
        )
    return workspace


def ensure_workspace_ready_for_io(workspace: Workspace) -> web.Response | None:
    workspace.refresh_status()
    if workspace.status == "suspended":
        return web.json_response({"error": "Workspace is suspended. Resume it first."}, status=409)
    if workspace.status != "ready" or workspace.runtime is None:
        return web.json_response(
            {"error": f"Workspace is not ready for interaction. Current status: {workspace.status}"},
            status=409,
        )
    return None


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
    idle_timeout_seconds = payload.get("idle_timeout_seconds", DEFAULT_IDLE_TIMEOUT_SECONDS)

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
    if not isinstance(idle_timeout_seconds, int) or idle_timeout_seconds <= 0:
        return web.json_response({"error": "Field 'idle_timeout_seconds' must be a positive integer."}, status=400)

    try:
        workspace = create_workspace_record(
            asyncio.get_running_loop(),
            owner_id=owner_id.strip(),
            name=name.strip(),
            requested_cwd=requested_cwd,
            repo_url=repo_url.strip() if isinstance(repo_url, str) else None,
            branch=branch.strip() if isinstance(branch, str) else None,
            commit=commit.strip() if isinstance(commit, str) else None,
            idle_timeout_seconds=idle_timeout_seconds,
        )
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    WORKSPACES[workspace.workspace_id] = workspace

    try:
        provision_workspace(workspace, "starting")
    except RuntimeError as exc:
        workspace.mark_error(str(exc))

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
    print(f"idle_timeout_seconds: {workspace.idle_timeout_seconds}")
    print("=" * 80)

    return web.json_response(workspace.to_dict(), status=201)


async def list_workspaces_handler(_: web.Request) -> web.Response:
    return web.json_response({"workspaces": [workspace.to_dict() for workspace in WORKSPACES.values()]})


async def get_workspace_handler(request: web.Request) -> web.Response:
    workspace = get_workspace_or_404(request.match_info["workspace_id"])
    return web.json_response(workspace.to_dict())


async def list_files_handler(request: web.Request) -> web.Response:
    workspace = get_workspace_or_404(request.match_info["workspace_id"])
    blocked = ensure_workspace_ready_for_io(workspace)
    if blocked is not None:
        return blocked

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
    blocked = ensure_workspace_ready_for_io(workspace)
    if blocked is not None:
        return blocked

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
    blocked = ensure_workspace_ready_for_io(workspace)
    if blocked is not None:
        return blocked

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
    blocked = ensure_workspace_ready_for_io(workspace)
    if blocked is not None:
        return blocked

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


async def suspend_workspace_handler(request: web.Request) -> web.Response:
    workspace = get_workspace_or_404(request.match_info["workspace_id"])
    try:
        suspend_workspace(workspace)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=409)

    print(f"[{utc_now_iso()}] suspended workspace {workspace.workspace_id}")
    return web.json_response(workspace.to_dict())


async def resume_workspace_handler(request: web.Request) -> web.Response:
    workspace = get_workspace_or_404(request.match_info["workspace_id"])
    try:
        resume_workspace(workspace)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=409)
    except RuntimeError as exc:
        workspace.mark_error(str(exc))
        return web.json_response(workspace.to_dict(), status=200)

    print(f"[{utc_now_iso()}] resumed workspace {workspace.workspace_id}")
    return web.json_response(workspace.to_dict())


async def workspace_websocket_handler(request: web.Request) -> web.StreamResponse:
    workspace = get_workspace_or_404(request.match_info["workspace_id"])
    blocked = ensure_workspace_ready_for_io(workspace)
    if blocked is not None:
        return blocked
    assert workspace.runtime is not None
    if workspace.runtime.websocket_attached:
        return web.json_response({"error": "A client is already attached to this workspace."}, status=409)

    ws = web.WebSocketResponse()
    await ws.prepare(request)
    workspace.runtime.websocket_attached = True
    workspace.mark_activity()
    print(f"[{utc_now_iso()}] websocket attached for workspace {workspace.workspace_id}")

    async def sender(runtime: ShellRuntime) -> None:
        while True:
            event = await runtime.output_queue.get()
            await ws.send_str(json.dumps(event))
            if event["type"] == "exit":
                break

    runtime = workspace.runtime
    sender_task = asyncio.create_task(sender(runtime))

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
                runtime.write_input(payload["data"])
                workspace.mark_activity()
            except RuntimeError as exc:
                await ws.send_str(json.dumps({"type": "error", "message": str(exc)}))
                break

            print(f"[{utc_now_iso()}] input for workspace {workspace.workspace_id}: {payload['data']!r}")
    finally:
        runtime.websocket_attached = False
        workspace.mark_activity()
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

    delete_workspace(workspace)
    print(f"[{utc_now_iso()}] deleted workspace {workspace_id} (project storage removed, home retained)")
    return web.json_response(workspace.to_dict())


async def idle_sweeper(_: web.Application) -> None:
    while True:
        await asyncio.sleep(IDLE_SWEEP_INTERVAL_SECONDS)
        now = utc_now()
        for workspace in list(WORKSPACES.values()):
            if workspace.should_suspend_for_idle(now):
                print(f"[{utc_now_iso()}] auto-suspending idle workspace {workspace.workspace_id}")
                try:
                    suspend_workspace(workspace)
                except ValueError:
                    continue


async def start_background_tasks(app: web.Application) -> None:
    app["idle_sweeper_task"] = asyncio.create_task(idle_sweeper(app))


async def stop_background_tasks(app: web.Application) -> None:
    idle_task = app.get("idle_sweeper_task")
    if idle_task is not None:
        idle_task.cancel()
        try:
            await idle_task
        except asyncio.CancelledError:
            pass


async def shutdown_all_workspaces(_: web.Application) -> None:
    workspaces = list(WORKSPACES.values())
    WORKSPACES.clear()
    for workspace in workspaces:
        runtime = workspace.runtime
        if runtime is not None:
            runtime.terminate("shutdown")
        cleanup_project_root(workspace.project_root)
        workspace.runtime = None
        workspace.status = "deleted"


def main() -> None:
    os.makedirs(USERS_ROOT, exist_ok=True)
    os.makedirs(EPHEMERAL_ROOT, exist_ok=True)

    app = web.Application()
    app.router.add_post("/workspaces", create_workspace_handler)
    app.router.add_get("/workspaces", list_workspaces_handler)
    app.router.add_get("/workspaces/{workspace_id}", get_workspace_handler)
    app.router.add_get("/workspaces/{workspace_id}/files", list_files_handler)
    app.router.add_get("/workspaces/{workspace_id}/file", read_file_handler)
    app.router.add_put("/workspaces/{workspace_id}/file", write_file_handler)
    app.router.add_post("/workspaces/{workspace_id}/directories", create_directory_handler)
    app.router.add_post("/workspaces/{workspace_id}/suspend", suspend_workspace_handler)
    app.router.add_post("/workspaces/{workspace_id}/resume", resume_workspace_handler)
    app.router.add_get("/workspaces/{workspace_id}/ws", workspace_websocket_handler)
    app.router.add_delete("/workspaces/{workspace_id}", delete_workspace_handler)
    app.on_startup.append(start_background_tasks)
    app.on_shutdown.append(stop_background_tasks)
    app.on_shutdown.append(shutdown_all_workspaces)

    print("Containerized workspace demo server")
    print(f"Listening on http://{HOST}:{PORT}")
    print(f"Base directory: {BASE_DIRECTORY}")
    print(f"Persistent home root: {USERS_ROOT}")
    print(f"Ephemeral project root base: {EPHEMERAL_ROOT}")
    print(f"Container runtime: {DOCKER_BIN}")
    print(f"Default container image: {DEFAULT_CONTAINER_IMAGE}")
    print(
        "Routes: POST /workspaces, GET /workspaces, GET /workspaces/{id}, "
        "GET /workspaces/{id}/files, GET /workspaces/{id}/file, "
        "PUT /workspaces/{id}/file, POST /workspaces/{id}/directories, "
        "POST /workspaces/{id}/suspend, POST /workspaces/{id}/resume, "
        "GET /workspaces/{id}/ws, DELETE /workspaces/{id}"
    )
    print(f"Idle sweeper interval: {IDLE_SWEEP_INTERVAL_SECONDS}s")
    print(f"Default idle timeout: {DEFAULT_IDLE_TIMEOUT_SECONDS}s")
    print("Press Ctrl+C to stop.")
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
