#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import pty
import shutil
import signal
import subprocess
import termios
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from aiohttp import WSMsgType, web


BASE_DIRECTORY = os.getcwd()
DATA_ROOT = os.path.join(BASE_DIRECTORY, ".stage13_data")
DOCKER_BIN = "docker"
DEFAULT_CONTAINER_IMAGE = "alpine:3.20"
CONTAINER_HOME = "/home/workspace-user"
CONTAINER_WORKSPACE = "/workspace"
DEFAULT_SHELL = "/bin/sh"
OUTPUT_READ_SIZE = 4096


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def parse_utc_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


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


def validate_runtime_path(path_value: str) -> str:
    abs_path = os.path.abspath(path_value)
    if not abs_path.startswith(DATA_ROOT):
        raise ValueError(f"path must stay inside {DATA_ROOT}")
    return abs_path


def get_container_name(worker_id: str, workspace_id: str) -> str:
    return f"stage13-{worker_id}-{workspace_id}"


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

    def publish_suspended(self) -> None:
        self.publish_event({"type": "suspended", "reason": "manual_or_idle"})

    def write_input(self, data: str) -> None:
        if self.closed:
            raise RuntimeError("runtime is no longer running")
        os.write(self.master_fd, data.encode("utf-8"))

    def terminate(self, reason: str) -> int:
        if self.closed:
            return self.process.poll() or 0

        self.shutdown_reason = reason
        if reason == "suspend":
            self.publish_suspended()

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
class RuntimeRecord:
    workspace_id: str
    owner_id: str
    name: str
    shell_path: str
    created_at: str
    loop: asyncio.AbstractEventLoop
    home_root: str
    project_root: str
    container_image: str
    container_name: str
    resource_profile: str
    cpu_limit: float
    memory_limit_mb: int
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

    def to_dict(self, worker_id: str) -> dict:
        self.refresh_status()
        return {
            "workspace_id": self.workspace_id,
            "owner_id": self.owner_id,
            "name": self.name,
            "worker_id": worker_id,
            "status": self.status,
            "home_root": self.home_root,
            "project_root": self.project_root,
            "shell_working_directory": self.shell_working_directory(),
            "shell": self.shell_path,
            "runtime_kind": "container",
            "container_image": self.container_image,
            "container_name": self.container_name,
            "resource_profile": self.resource_profile,
            "cpu_limit": self.cpu_limit,
            "memory_limit_mb": self.memory_limit_mb,
            "created_at": self.created_at,
            "last_activity_at": self.last_activity_at,
            "suspended_at": self.suspended_at,
            "exit_code": self.exit_code,
            "last_error": self.last_error,
            "repo_url": self.repo_url,
            "branch": self.branch,
            "commit": self.commit,
            "bootstrap_status": self.bootstrap_status,
            "runtime_attached": self.runtime is not None and not self.runtime.closed,
        }


RUNTIMES: dict[str, RuntimeRecord] = {}
WORKER_ID = "worker-a"
HOST = "127.0.0.1"
PORT = 8017
MAX_ACTIVE_WORKSPACES = 2
MAX_ACTIVE_CPUS = 2.0
MAX_ACTIVE_MEMORY_MB = 2048
CONTROL_PLANE_URL = "http://127.0.0.1:8016"
REGISTRATION_INTERVAL_SECONDS = 5


def workspace_reader(record: RuntimeRecord, runtime: ShellRuntime) -> None:
    while True:
        try:
            raw = os.read(runtime.master_fd, OUTPUT_READ_SIZE)
        except OSError:
            break
        if not raw:
            break
        record.mark_activity()
        runtime.publish_output(raw.decode("utf-8", errors="replace"))

    exit_code = runtime.process.wait()
    record.exit_code = exit_code
    runtime.closed = True
    try:
        os.close(runtime.master_fd)
    except OSError:
        pass

    if runtime.shutdown_reason in {"suspend", "delete", "shutdown"}:
        runtime.publish_exit(exit_code)
        return

    if record.runtime is runtime:
        record.status = "error" if exit_code != 0 else "stopped"
        record.last_error = None if exit_code == 0 else f"Shell exited with code {exit_code}"
        record.runtime = None
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


def active_runtimes() -> list[RuntimeRecord]:
    return [
        record
        for record in RUNTIMES.values()
        if record.status == "ready" and record.runtime is not None and not record.runtime.closed
    ]


def active_usage() -> dict:
    running = active_runtimes()
    return {
        "workspace_count": len(running),
        "cpus": sum(record.cpu_limit for record in running),
        "memory_mb": sum(record.memory_limit_mb for record in running),
    }


def log_worker_event(record: RuntimeRecord, message: str) -> None:
    print(f"[{utc_now_iso()}] {WORKER_ID} workspace {record.workspace_id}: {message}")


def ensure_capacity_for_record(candidate: RuntimeRecord) -> None:
    usage = active_usage()
    next_workspace_count = usage["workspace_count"] + 1
    next_cpus = usage["cpus"] + candidate.cpu_limit
    next_memory_mb = usage["memory_mb"] + candidate.memory_limit_mb
    if next_workspace_count > MAX_ACTIVE_WORKSPACES:
        raise ValueError(f"worker {WORKER_ID} is full by workspace count")
    if next_cpus > MAX_ACTIVE_CPUS:
        raise ValueError(f"worker {WORKER_ID} is full by CPU")
    if next_memory_mb > MAX_ACTIVE_MEMORY_MB:
        raise ValueError(f"worker {WORKER_ID} is full by memory")


def create_shell_runtime(record: RuntimeRecord) -> ShellRuntime:
    ensure_container_image(record.container_image)
    master_fd, slave_fd = pty.openpty()
    tty_attrs = termios.tcgetattr(slave_fd)
    tty_attrs[3] &= ~termios.ECHO
    termios.tcsetattr(slave_fd, termios.TCSANOW, tty_attrs)

    env = os.environ.copy()
    env["TERM"] = "dumb"
    env["DOCKER_CLI_HINTS"] = "false"
    run_command([DOCKER_BIN, "rm", "-f", record.container_name])
    process = subprocess.Popen(
        [
            DOCKER_BIN,
            "run",
            "--rm",
            "--name",
            record.container_name,
            "-i",
            "--cpus",
            str(record.cpu_limit),
            "--memory",
            f"{record.memory_limit_mb}m",
            "-v",
            f"{record.home_root}:{CONTAINER_HOME}",
            "-v",
            f"{record.project_root}:{CONTAINER_WORKSPACE}",
            "-w",
            CONTAINER_WORKSPACE,
            "-e",
            f"HOME={CONTAINER_HOME}",
            "-e",
            "TERM=dumb",
            record.container_image,
            record.shell_path,
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
        loop=record.loop,
        container_name=record.container_name,
        container_image=record.container_image,
    )


def cleanup_project_root(project_root: str) -> None:
    if os.path.isdir(project_root):
        shutil.rmtree(os.path.dirname(project_root), ignore_errors=True)


def get_current_commit(project_root: str) -> str | None:
    result = run_command(["git", "-C", project_root, "rev-parse", "HEAD"])
    return result.stdout.strip() if result.returncode == 0 else None


def get_current_branch(project_root: str) -> str | None:
    result = run_command(["git", "-C", project_root, "rev-parse", "--abbrev-ref", "HEAD"])
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return None if branch == "HEAD" else branch or None


def bootstrap_project(record: RuntimeRecord) -> None:
    os.makedirs(record.home_root, exist_ok=True)
    cleanup_project_root(record.project_root)
    os.makedirs(record.project_root, exist_ok=True)

    if not record.repo_url:
        record.bootstrap_status = "not_requested"
        return

    record.status = "cloning"
    record.bootstrap_status = "cloning"
    clone_result = run_command(["git", "clone", record.repo_url, record.project_root])
    if clone_result.returncode != 0:
        cleanup_project_root(record.project_root)
        raise RuntimeError(clone_result.stderr.strip() or clone_result.stdout.strip() or "git clone failed")

    if record.commit:
        checkout_result = run_command(["git", "-C", record.project_root, "checkout", record.commit])
        if checkout_result.returncode != 0:
            cleanup_project_root(record.project_root)
            raise RuntimeError(checkout_result.stderr.strip() or checkout_result.stdout.strip() or "git checkout commit failed")
    elif record.branch:
        checkout_result = run_command(["git", "-C", record.project_root, "checkout", record.branch])
        if checkout_result.returncode != 0:
            cleanup_project_root(record.project_root)
            raise RuntimeError(checkout_result.stderr.strip() or checkout_result.stdout.strip() or "git checkout branch failed")

    record.commit = get_current_commit(record.project_root)
    if not record.branch:
        record.branch = get_current_branch(record.project_root)
    record.bootstrap_status = "checked_out"


def attach_runtime(record: RuntimeRecord) -> None:
    runtime = create_shell_runtime(record)
    record.mark_ready(runtime)
    threading.Thread(
        target=workspace_reader,
        args=(record, runtime),
        name=f"worker-runtime-{record.workspace_id}",
        daemon=True,
    ).start()


def provision_runtime(record: RuntimeRecord, lifecycle_status: str) -> None:
    ensure_capacity_for_record(record)
    record.status = lifecycle_status
    record.last_error = None
    record.exit_code = None
    bootstrap_project(record)
    attach_runtime(record)


def suspend_runtime(record: RuntimeRecord) -> None:
    if record.status != "ready":
        raise ValueError(f"runtime must be ready to suspend, current status is {record.status}")
    record.status = "suspending"
    if record.runtime is not None:
        record.exit_code = record.runtime.terminate("suspend")
    record.runtime = None
    cleanup_project_root(record.project_root)
    record.suspended_at = utc_now_iso()
    record.last_activity_at = record.suspended_at
    record.status = "suspended"


def resume_runtime(record: RuntimeRecord) -> None:
    if record.status != "suspended":
        raise ValueError(f"runtime must be suspended to resume, current status is {record.status}")
    ensure_capacity_for_record(record)
    record.status = "resuming"
    record.suspended_at = None
    record.last_error = None
    record.exit_code = None
    bootstrap_project(record)
    attach_runtime(record)


def delete_runtime(record: RuntimeRecord) -> None:
    record.status = "stopping"
    if record.runtime is not None:
        record.exit_code = record.runtime.terminate("delete")
    record.runtime = None
    cleanup_project_root(record.project_root)
    record.status = "deleted"
    record.last_activity_at = utc_now_iso()


def get_record_or_404(workspace_id: str) -> RuntimeRecord:
    record = RUNTIMES.get(workspace_id)
    if record is None:
        raise web.HTTPNotFound(
            text=json.dumps({"error": f"Unknown runtime: {workspace_id}"}),
            content_type="application/json",
        )
    return record


def ensure_record_ready_for_io(record: RuntimeRecord) -> web.Response | None:
    record.refresh_status()
    if record.status == "suspended":
        return web.json_response({"error": "Runtime is suspended. Resume it first."}, status=409)
    if record.status != "ready" or record.runtime is None:
        return web.json_response({"error": f"Runtime is not ready. Current status: {record.status}"}, status=409)
    return None


def list_directory_entries(target_path: str, project_root: str) -> list[dict]:
    entries = []
    for entry in sorted(os.scandir(target_path), key=lambda item: (not item.is_dir(), item.name.lower())):
        entry_path = os.path.abspath(entry.path)
        relative_path = os.path.relpath(entry_path, project_root)
        stat_result = entry.stat()
        entries.append(
            {
                "name": entry.name,
                "path": "." if relative_path == "." else relative_path,
                "type": "directory" if entry.is_dir() else "file",
                "size_bytes": stat_result.st_size,
                "modified_at": datetime.fromtimestamp(stat_result.st_mtime, tz=timezone.utc).isoformat(),
            }
        )
    return entries


async def health_handler(_: web.Request) -> web.Response:
    return web.json_response(
        {
            "worker_id": WORKER_ID,
            "status": "healthy",
            "active_usage": active_usage(),
            "capacity_limits": {
                "max_active_workspaces": MAX_ACTIVE_WORKSPACES,
                "max_active_cpus": MAX_ACTIVE_CPUS,
                "max_active_memory_mb": MAX_ACTIVE_MEMORY_MB,
            },
        }
    )


async def list_runtimes_handler(_: web.Request) -> web.Response:
    return web.json_response(
        {
            "worker_id": WORKER_ID,
            "runtimes": [record.to_dict(WORKER_ID) for record in RUNTIMES.values()],
        }
    )


async def register_with_control_plane(app: web.Application) -> bool:
    session = app["http_session"]
    payload = {
        "worker_id": WORKER_ID,
        "url": f"http://{HOST}:{PORT}",
    }
    try:
        async with session.post(f"{CONTROL_PLANE_URL}/workers/register", json=payload) as response:
            if response.status < 400:
                return True
            body = await response.text()
            print(f"[{utc_now_iso()}] worker registration failed: {response.status} {body}")
    except aiohttp.ClientError as exc:
        print(f"[{utc_now_iso()}] worker registration failed: {exc}")
    return False


async def registration_loop(app: web.Application) -> None:
    while True:
        if await register_with_control_plane(app):
            print(f"[{utc_now_iso()}] worker {WORKER_ID} registered with control plane")
            return
        await asyncio.sleep(REGISTRATION_INTERVAL_SECONDS)


async def create_runtime_handler(request: web.Request) -> web.Response:
    payload = await request.json()
    workspace_id = payload["workspace_id"]
    if workspace_id in RUNTIMES:
        return web.json_response({"error": f"Runtime already exists: {workspace_id}"}, status=409)

    try:
        record = RuntimeRecord(
            workspace_id=workspace_id,
            owner_id=payload["owner_id"],
            name=payload["name"],
            shell_path=payload.get("shell", DEFAULT_SHELL),
            created_at=payload.get("created_at", utc_now_iso()),
            loop=asyncio.get_running_loop(),
            home_root=validate_runtime_path(payload["home_root"]),
            project_root=validate_runtime_path(payload["project_root"]),
            container_image=payload.get("container_image", DEFAULT_CONTAINER_IMAGE),
            container_name=payload["container_name"],
            resource_profile=payload["resource_profile"],
            cpu_limit=float(payload["cpu_limit"]),
            memory_limit_mb=int(payload["memory_limit_mb"]),
            repo_url=payload.get("repo_url"),
            branch=payload.get("branch"),
            commit=payload.get("commit"),
        )
        provision_runtime(record, "starting")
    except (KeyError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except RuntimeError as exc:
        record.mark_error(str(exc))
        RUNTIMES[workspace_id] = record
        return web.json_response(record.to_dict(WORKER_ID), status=201)

    RUNTIMES[workspace_id] = record
    return web.json_response(record.to_dict(WORKER_ID), status=201)


async def get_runtime_handler(request: web.Request) -> web.Response:
    record = get_record_or_404(request.match_info["workspace_id"])
    return web.json_response(record.to_dict(WORKER_ID))


async def list_files_handler(request: web.Request) -> web.Response:
    record = get_record_or_404(request.match_info["workspace_id"])
    blocked = ensure_record_ready_for_io(record)
    if blocked is not None:
        return blocked
    requested_path = request.query.get("path", ".")
    try:
        target_path, normalized_relative = resolve_project_path(record.project_root, requested_path)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    if not os.path.exists(target_path):
        return web.json_response({"error": f"path does not exist: {requested_path}"}, status=404)
    if not os.path.isdir(target_path):
        return web.json_response({"error": f"path is not a directory: {requested_path}"}, status=400)
    record.mark_activity()
    log_worker_event(record, f"list files path={normalized_relative}")
    return web.json_response(
        {
            "workspace_id": record.workspace_id,
            "path": normalized_relative,
            "entries": list_directory_entries(target_path, record.project_root),
        }
    )


async def read_file_handler(request: web.Request) -> web.Response:
    record = get_record_or_404(request.match_info["workspace_id"])
    blocked = ensure_record_ready_for_io(record)
    if blocked is not None:
        return blocked
    requested_path = request.query.get("path")
    if not requested_path:
        return web.json_response({"error": "Query parameter 'path' is required."}, status=400)
    try:
        target_path, normalized_relative = resolve_project_path(record.project_root, requested_path)
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
    record.mark_activity()
    log_worker_event(record, f"read file path={normalized_relative}")
    return web.json_response({"workspace_id": record.workspace_id, "path": normalized_relative, "content": content})


async def write_file_handler(request: web.Request) -> web.Response:
    record = get_record_or_404(request.match_info["workspace_id"])
    blocked = ensure_record_ready_for_io(record)
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
        target_path, normalized_relative = resolve_project_path(record.project_root, requested_path)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    Path(target_path).write_text(content, encoding="utf-8")
    record.mark_activity()
    log_worker_event(record, f"write file path={normalized_relative} bytes={len(content.encode('utf-8'))}")
    return web.json_response(
        {"workspace_id": record.workspace_id, "path": normalized_relative, "bytes_written": len(content.encode("utf-8"))}
    )


async def create_directory_handler(request: web.Request) -> web.Response:
    record = get_record_or_404(request.match_info["workspace_id"])
    blocked = ensure_record_ready_for_io(record)
    if blocked is not None:
        return blocked
    payload = await request.json()
    requested_path = payload.get("path")
    if not isinstance(requested_path, str) or not requested_path.strip():
        return web.json_response({"error": "Field 'path' must be a non-empty string."}, status=400)
    try:
        target_path, normalized_relative = resolve_project_path(record.project_root, requested_path)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    os.makedirs(target_path, exist_ok=True)
    record.mark_activity()
    log_worker_event(record, f"mkdir path={normalized_relative}")
    return web.json_response({"workspace_id": record.workspace_id, "path": normalized_relative, "status": "created"}, status=201)


async def suspend_runtime_handler(request: web.Request) -> web.Response:
    record = get_record_or_404(request.match_info["workspace_id"])
    try:
        suspend_runtime(record)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=409)
    return web.json_response(record.to_dict(WORKER_ID))


async def resume_runtime_handler(request: web.Request) -> web.Response:
    record = get_record_or_404(request.match_info["workspace_id"])
    try:
        resume_runtime(record)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=409)
    except RuntimeError as exc:
        record.mark_error(str(exc))
    return web.json_response(record.to_dict(WORKER_ID))


async def delete_runtime_handler(request: web.Request) -> web.Response:
    workspace_id = request.match_info["workspace_id"]
    record = RUNTIMES.pop(workspace_id, None)
    if record is None:
        return web.json_response({"error": f"Unknown runtime: {workspace_id}"}, status=404)
    delete_runtime(record)
    return web.json_response(record.to_dict(WORKER_ID))


async def websocket_handler(request: web.Request) -> web.StreamResponse:
    record = get_record_or_404(request.match_info["workspace_id"])
    blocked = ensure_record_ready_for_io(record)
    if blocked is not None:
        return blocked
    assert record.runtime is not None
    if record.runtime.websocket_attached:
        return web.json_response({"error": "A client is already attached to this runtime."}, status=409)

    ws = web.WebSocketResponse()
    await ws.prepare(request)
    record.runtime.websocket_attached = True
    record.mark_activity()

    async def sender(runtime: ShellRuntime) -> None:
        while True:
            event = await runtime.output_queue.get()
            await ws.send_str(json.dumps(event))
            if event["type"] == "exit":
                break

    runtime = record.runtime
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
                await ws.send_str(json.dumps({"type": "error", "message": "Expected {'type': 'input', 'data': string}."}))
                continue
            try:
                runtime.write_input(payload["data"])
                record.mark_activity()
                command_preview = payload["data"].replace("\r", "\\r").replace("\n", "\\n")
                log_worker_event(record, f"shell input data={command_preview[:120]}")
            except RuntimeError as exc:
                await ws.send_str(json.dumps({"type": "error", "message": str(exc)}))
                break
    finally:
        runtime.websocket_attached = False
        record.mark_activity()
        sender_task.cancel()
        try:
            await sender_task
        except asyncio.CancelledError:
            pass
    return ws


async def shutdown_all(_: web.Application) -> None:
    records = list(RUNTIMES.values())
    RUNTIMES.clear()
    for record in records:
        if record.runtime is not None:
            record.runtime.terminate("shutdown")
        cleanup_project_root(record.project_root)
        record.runtime = None
        record.status = "deleted"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-id", default="worker-a")
    parser.add_argument("--port", type=int, default=8017)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--control-plane-url", default="http://127.0.0.1:8016")
    parser.add_argument("--max-active-workspaces", type=int, default=2)
    parser.add_argument("--max-active-cpus", type=float, default=2.0)
    parser.add_argument("--max-active-memory-mb", type=int, default=2048)
    return parser.parse_args()


async def startup_http_session(app: web.Application) -> None:
    app["http_session"] = aiohttp.ClientSession()


async def shutdown_http_session(app: web.Application) -> None:
    session = app.get("http_session")
    if session is not None:
        await session.close()


async def start_background_tasks(app: web.Application) -> None:
    app["registration_task"] = asyncio.create_task(registration_loop(app))


async def stop_background_tasks(app: web.Application) -> None:
    task = app.get("registration_task")
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def main() -> None:
    global WORKER_ID, HOST, PORT, MAX_ACTIVE_WORKSPACES, MAX_ACTIVE_CPUS, MAX_ACTIVE_MEMORY_MB, CONTROL_PLANE_URL
    args = parse_args()
    WORKER_ID = args.worker_id
    HOST = args.host
    PORT = args.port
    CONTROL_PLANE_URL = args.control_plane_url.rstrip("/")
    MAX_ACTIVE_WORKSPACES = args.max_active_workspaces
    MAX_ACTIVE_CPUS = args.max_active_cpus
    MAX_ACTIVE_MEMORY_MB = args.max_active_memory_mb

    os.makedirs(DATA_ROOT, exist_ok=True)

    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_get("/runtimes", list_runtimes_handler)
    app.router.add_post("/runtimes", create_runtime_handler)
    app.router.add_get("/runtimes/{workspace_id}", get_runtime_handler)
    app.router.add_get("/runtimes/{workspace_id}/files", list_files_handler)
    app.router.add_get("/runtimes/{workspace_id}/file", read_file_handler)
    app.router.add_put("/runtimes/{workspace_id}/file", write_file_handler)
    app.router.add_post("/runtimes/{workspace_id}/directories", create_directory_handler)
    app.router.add_post("/runtimes/{workspace_id}/suspend", suspend_runtime_handler)
    app.router.add_post("/runtimes/{workspace_id}/resume", resume_runtime_handler)
    app.router.add_get("/runtimes/{workspace_id}/ws", websocket_handler)
    app.router.add_delete("/runtimes/{workspace_id}", delete_runtime_handler)
    app.on_startup.append(startup_http_session)
    app.on_startup.append(start_background_tasks)
    app.on_shutdown.append(stop_background_tasks)
    app.on_shutdown.append(shutdown_http_session)
    app.on_shutdown.append(shutdown_all)

    print(f"Stage 13 worker {WORKER_ID}")
    print(f"Listening on http://{HOST}:{PORT}")
    print(f"Control plane: {CONTROL_PLANE_URL}")
    print(
        "Capacity limits: "
        f"{MAX_ACTIVE_WORKSPACES} ready runtimes, "
        f"{MAX_ACTIVE_CPUS:.1f} CPUs, "
        f"{MAX_ACTIVE_MEMORY_MB} MB"
    )
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
