#!/usr/bin/env python3
import asyncio
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import aiohttp
from aiohttp import WSMsgType, web


HOST = "127.0.0.1"
PORT = 8013
BASE_DIRECTORY = os.getcwd()
DATA_ROOT = os.path.join(BASE_DIRECTORY, ".stage12_data")
USERS_ROOT = os.path.join(DATA_ROOT, "users")
EPHEMERAL_ROOT = os.path.join(DATA_ROOT, "ephemeral", "workspaces")
DEFAULT_CONTAINER_IMAGE = "alpine:3.20"
DEFAULT_SHELL = "/bin/sh"
DEFAULT_OWNER_ID = "demo-user"
DEFAULT_WORKSPACE_NAME = "demo-workspace"
DEFAULT_IDLE_TIMEOUT_SECONDS = 30
IDLE_SWEEP_INTERVAL_SECONDS = 5
WORKER_POLL_INTERVAL_SECONDS = 3
WORKER_STALE_TIMEOUT_SECONDS = 10
OWNER_LIMITS = {
    "max_active_workspaces": 2,
    "max_active_cpus": 2.0,
    "max_active_memory_mb": 2048,
}
RESOURCE_PROFILES = {
    "small": {"cpus": 0.5, "memory_mb": 512},
    "medium": {"cpus": 1.0, "memory_mb": 1024},
    "large": {"cpus": 2.0, "memory_mb": 2048},
}
DEFAULT_RESOURCE_PROFILE = "small"


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


def get_resource_profile(profile_name: str) -> dict:
    profile = RESOURCE_PROFILES.get(profile_name)
    if profile is None:
        raise ValueError("resource_profile must be one of: " + ", ".join(sorted(RESOURCE_PROFILES)))
    return profile


def get_home_root(owner_id: str) -> str:
    return os.path.join(USERS_ROOT, slugify(owner_id), "home")


def get_project_root(workspace_id: str) -> str:
    return os.path.join(EPHEMERAL_ROOT, workspace_id, "project")


def get_container_name(worker_id: str, workspace_id: str) -> str:
    return f"stage12-{worker_id}-{workspace_id}"


@dataclass
class WorkerRecord:
    worker_id: str
    url: str
    status: str = "registered"
    registered_at: str = field(default_factory=utc_now_iso)
    last_seen_at: str | None = None
    active_usage: dict = field(default_factory=lambda: {"workspace_count": 0, "cpus": 0.0, "memory_mb": 0})
    capacity_limits: dict = field(
        default_factory=lambda: {
            "max_active_workspaces": 0,
            "max_active_cpus": 0.0,
            "max_active_memory_mb": 0,
        }
    )
    last_error: str | None = None

    def to_dict(self) -> dict:
        return {
            "worker_id": self.worker_id,
            "url": self.url,
            "status": self.status,
            "registered_at": self.registered_at,
            "last_seen_at": self.last_seen_at,
            "active_usage": self.active_usage,
            "capacity_limits": self.capacity_limits,
            "last_error": self.last_error,
        }


@dataclass
class Workspace:
    workspace_id: str
    owner_id: str
    name: str
    base_directory: str
    shell_path: str
    created_at: str
    home_root: str
    project_root: str
    idle_timeout_seconds: int
    container_image: str = DEFAULT_CONTAINER_IMAGE
    resource_profile: str = DEFAULT_RESOURCE_PROFILE
    cpu_limit: float = RESOURCE_PROFILES[DEFAULT_RESOURCE_PROFILE]["cpus"]
    memory_limit_mb: int = RESOURCE_PROFILES[DEFAULT_RESOURCE_PROFILE]["memory_mb"]
    repo_url: str | None = None
    branch: str | None = None
    commit: str | None = None
    bootstrap_status: str = "not_requested"
    status: str = "starting"
    assigned_worker_id: str | None = None
    last_activity_at: str = field(default_factory=utc_now_iso)
    suspended_at: str | None = None
    exit_code: int | None = None
    last_error: str | None = None
    websocket_attached: bool = False

    def shell_working_directory(self) -> str:
        return "/workspace"

    def should_suspend_for_idle(self, now: datetime) -> bool:
        if self.status != "ready" or self.websocket_attached:
            return False
        last_activity = parse_utc_timestamp(self.last_activity_at)
        if last_activity is None:
            return False
        return (now - last_activity).total_seconds() >= self.idle_timeout_seconds

    def mark_activity(self) -> None:
        self.last_activity_at = utc_now_iso()

    def mark_worker_lost(self, message: str) -> None:
        self.status = "worker_lost"
        self.websocket_attached = False
        self.last_error = message
        self.exit_code = None
        self.mark_activity()

    def to_dict(self) -> dict:
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
            "container_name": get_container_name(self.assigned_worker_id or "unassigned", self.workspace_id),
            "resource_profile": self.resource_profile,
            "cpu_limit": self.cpu_limit,
            "memory_limit_mb": self.memory_limit_mb,
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
            "runtime_attached": self.status == "ready",
            "assigned_worker_id": self.assigned_worker_id,
            "websocket_attached": self.websocket_attached,
        }


WORKSPACES: dict[str, Workspace] = {}


async def startup_http_session(app: web.Application) -> None:
    app["http_session"] = aiohttp.ClientSession()


async def shutdown_http_session(app: web.Application) -> None:
    session = app.get("http_session")
    if session is not None:
        await session.close()


def get_http_session(app: web.Application) -> aiohttp.ClientSession:
    return app["http_session"]


def get_worker_registry(app: web.Application) -> dict[str, WorkerRecord]:
    return app["workers"]


def get_active_client_sockets(app: web.Application) -> dict[str, web.WebSocketResponse]:
    return app["active_client_sockets"]


async def close_workspace_client_socket(app: web.Application, workspace_id: str, message: str) -> None:
    client_ws = get_active_client_sockets(app).pop(workspace_id, None)
    if client_ws is None or client_ws.closed:
        return
    try:
        await client_ws.send_str(json.dumps({"type": "error", "message": message}))
    except ConnectionResetError:
        pass
    await client_ws.close()


async def register_worker_handler(request: web.Request) -> web.Response:
    payload = await request.json()
    worker_id = payload.get("worker_id")
    url = payload.get("url")
    if not isinstance(worker_id, str) or not worker_id.strip():
        return web.json_response({"error": "Field 'worker_id' must be a non-empty string."}, status=400)
    if not isinstance(url, str) or not url.strip():
        return web.json_response({"error": "Field 'url' must be a non-empty string."}, status=400)

    registry = get_worker_registry(request.app)
    record = registry.get(worker_id.strip())
    if record is None:
        record = WorkerRecord(worker_id=worker_id.strip(), url=url.strip())
        registry[record.worker_id] = record
        print(f"[{utc_now_iso()}] registered worker {record.worker_id} at {record.url}")
    else:
        if record.url != url.strip():
            print(f"[{utc_now_iso()}] updated worker {record.worker_id} url from {record.url} to {url.strip()}")
        record.url = url.strip()
    return web.json_response(record.to_dict(), status=201)


async def fetch_worker_health(session: aiohttp.ClientSession, worker: WorkerRecord) -> dict:
    try:
        async with session.get(f"{worker.url}/health") as response:
            if response.status >= 400:
                return {"worker_id": worker.worker_id, "status": "unhealthy", "error": await response.text(), "url": worker.url}
            payload = await response.json()
            payload["url"] = worker.url
            return payload
    except aiohttp.ClientError as exc:
        return {"worker_id": worker.worker_id, "status": "unreachable", "error": str(exc), "url": worker.url}


async def refresh_workers_once(app: web.Application) -> None:
    registry = get_worker_registry(app)
    if not registry:
        return

    session = get_http_session(app)
    results = await asyncio.gather(*(fetch_worker_health(session, worker) for worker in registry.values()))
    now = utc_now()
    now_iso = now.isoformat()

    for payload in results:
        worker_id = payload["worker_id"]
        worker = registry.get(worker_id)
        if worker is None:
            continue

        if payload.get("status") == "healthy":
            previous_status = worker.status
            worker.status = "healthy"
            worker.last_seen_at = now_iso
            worker.active_usage = payload.get("active_usage", worker.active_usage)
            worker.capacity_limits = payload.get("capacity_limits", worker.capacity_limits)
            worker.last_error = None
            if previous_status == "unhealthy":
                print(f"[{utc_now_iso()}] worker {worker.worker_id} recovered")
            continue

        previous_status = worker.status
        worker.last_error = payload.get("error", "health check failed")
        last_seen = parse_utc_timestamp(worker.last_seen_at)
        if last_seen is None:
            worker.status = "unhealthy"
        elif (now - last_seen).total_seconds() >= WORKER_STALE_TIMEOUT_SECONDS:
            worker.status = "unhealthy"
        else:
            worker.status = "degraded"

        if worker.status != previous_status:
            if worker.status == "unhealthy":
                print(
                    f"[{utc_now_iso()}] worker {worker.worker_id} health checks timed out; "
                    f"marking unhealthy ({worker.last_error})"
                )
            else:
                print(
                    f"[{utc_now_iso()}] worker {worker.worker_id} health check failed; "
                    f"marking {worker.status} ({worker.last_error})"
                )


async def reconcile_worker_failures(app: web.Application) -> None:
    registry = get_worker_registry(app)
    unhealthy = {worker_id for worker_id, worker in registry.items() if worker.status == "unhealthy"}
    if not unhealthy:
        return

    for workspace in list(WORKSPACES.values()):
        if workspace.assigned_worker_id not in unhealthy:
            continue
        if workspace.status in {"deleted", "worker_lost"}:
            continue
        await close_workspace_client_socket(
            app,
            workspace.workspace_id,
            f"Assigned worker {workspace.assigned_worker_id} became unavailable. Reconnect later to resume on another worker.",
        )
        workspace.mark_worker_lost(f"assigned worker {workspace.assigned_worker_id} became unavailable")


async def worker_monitor(app: web.Application) -> None:
    while True:
        await asyncio.sleep(WORKER_POLL_INTERVAL_SECONDS)
        await refresh_workers_once(app)
        await reconcile_worker_failures(app)


def get_healthy_workers(app: web.Application) -> list[WorkerRecord]:
    workers = [worker for worker in get_worker_registry(app).values() if worker.status == "healthy"]
    workers.sort(key=lambda item: item.worker_id)
    return workers


def dominant_load(usage: dict, limits: dict) -> float:
    workspace_limit = limits["max_active_workspaces"] or 1
    cpu_limit = limits["max_active_cpus"] or 1.0
    memory_limit = limits["max_active_memory_mb"] or 1
    return max(
        usage["workspace_count"] / workspace_limit,
        usage["cpus"] / cpu_limit,
        usage["memory_mb"] / memory_limit,
    )


def projected_dominant_load(worker: WorkerRecord, workspace: Workspace) -> float:
    usage = worker.active_usage
    projected_usage = {
        "workspace_count": usage["workspace_count"] + 1,
        "cpus": usage["cpus"] + workspace.cpu_limit,
        "memory_mb": usage["memory_mb"] + workspace.memory_limit_mb,
    }
    return dominant_load(projected_usage, worker.capacity_limits)


def stable_tie_breaker(workspace_id: str, worker_id: str) -> int:
    digest = hashlib.sha256(f"{workspace_id}:{worker_id}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def owner_active_usage(owner_id: str, exclude_workspace_id: str | None = None) -> dict:
    active = [
        workspace
        for workspace in WORKSPACES.values()
        if workspace.owner_id == owner_id
        and workspace.workspace_id != exclude_workspace_id
        and workspace.status == "ready"
    ]
    return {
        "workspace_count": len(active),
        "cpus": sum(workspace.cpu_limit for workspace in active),
        "memory_mb": sum(workspace.memory_limit_mb for workspace in active),
    }


def ensure_owner_capacity(workspace: Workspace, *, exclude_workspace_id: str | None = None) -> None:
    usage = owner_active_usage(workspace.owner_id, exclude_workspace_id=exclude_workspace_id)
    next_workspace_count = usage["workspace_count"] + 1
    next_cpus = usage["cpus"] + workspace.cpu_limit
    next_memory_mb = usage["memory_mb"] + workspace.memory_limit_mb

    if next_workspace_count > OWNER_LIMITS["max_active_workspaces"]:
        raise ValueError(f"owner {workspace.owner_id} is at the active workspace limit")
    if next_cpus > OWNER_LIMITS["max_active_cpus"]:
        raise ValueError(f"owner {workspace.owner_id} is at the CPU limit")
    if next_memory_mb > OWNER_LIMITS["max_active_memory_mb"]:
        raise ValueError(f"owner {workspace.owner_id} is at the memory limit")


async def choose_worker(app: web.Application, workspace: Workspace, preferred_worker_id: str | None = None) -> WorkerRecord:
    profile_cpu = workspace.cpu_limit
    profile_memory = workspace.memory_limit_mb

    healthy = get_healthy_workers(app)
    candidates = []
    for worker in healthy:
        usage = worker.active_usage
        limits = worker.capacity_limits
        if usage["workspace_count"] + 1 > limits["max_active_workspaces"]:
            continue
        if usage["cpus"] + profile_cpu > limits["max_active_cpus"]:
            continue
        if usage["memory_mb"] + profile_memory > limits["max_active_memory_mb"]:
            continue
        candidates.append(worker)

    if not candidates:
        raise ValueError("no healthy worker has enough remaining capacity")

    candidates.sort(
        key=lambda item: (
            item.worker_id != preferred_worker_id if preferred_worker_id else False,
            projected_dominant_load(item, workspace),
            item.active_usage["workspace_count"],
            stable_tie_breaker(workspace.workspace_id, item.worker_id),
        )
    )
    return candidates[0]


async def worker_json_request(
    app: web.Application,
    method: str,
    worker_id: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
) -> tuple[int, dict]:
    worker = get_worker_registry(app).get(worker_id)
    if worker is None:
        return 404, {"error": f"Unknown worker: {worker_id}"}

    session = get_http_session(app)
    try:
        async with session.request(method, f"{worker.url}{path}", params=params, json=json_body) as response:
            payload = await response.json()
            return response.status, payload
    except aiohttp.ClientError as exc:
        worker.last_error = str(exc)
        return 503, {"error": str(exc)}


def create_workspace_record(
    owner_id: str,
    name: str,
    requested_cwd: str,
    repo_url: str | None,
    branch: str | None,
    commit: str | None,
    idle_timeout_seconds: int,
    resource_profile: str,
) -> Workspace:
    base_directory = validate_base_directory(requested_cwd)
    workspace_id = str(uuid.uuid4())
    profile = get_resource_profile(resource_profile)
    return Workspace(
        workspace_id=workspace_id,
        owner_id=owner_id,
        name=name,
        base_directory=base_directory,
        shell_path=DEFAULT_SHELL,
        created_at=utc_now_iso(),
        home_root=get_home_root(owner_id),
        project_root=get_project_root(workspace_id),
        container_image=DEFAULT_CONTAINER_IMAGE,
        resource_profile=resource_profile,
        cpu_limit=profile["cpus"],
        memory_limit_mb=profile["memory_mb"],
        repo_url=repo_url,
        branch=branch,
        commit=commit,
        idle_timeout_seconds=idle_timeout_seconds,
    )


def get_workspace_or_404(workspace_id: str) -> Workspace:
    workspace = WORKSPACES.get(workspace_id)
    if workspace is None:
        raise web.HTTPNotFound(text=json.dumps({"error": f"Unknown workspace: {workspace_id}"}), content_type="application/json")
    return workspace


def ensure_workspace_ready_for_io(workspace: Workspace) -> web.Response | None:
    if workspace.status == "suspended":
        return web.json_response({"error": "Workspace is suspended. Resume it first."}, status=409)
    if workspace.status == "worker_lost":
        return web.json_response({"error": "Workspace lost its worker. Resume it to reprovision on a healthy worker."}, status=409)
    if workspace.status != "ready" or not workspace.assigned_worker_id:
        return web.json_response({"error": f"Workspace is not ready for interaction. Current status: {workspace.status}"}, status=409)
    return None


def merge_worker_runtime(workspace: Workspace, runtime_payload: dict) -> None:
    workspace.status = runtime_payload["status"]
    workspace.bootstrap_status = runtime_payload.get("bootstrap_status", workspace.bootstrap_status)
    workspace.branch = runtime_payload.get("branch", workspace.branch)
    workspace.commit = runtime_payload.get("commit", workspace.commit)
    workspace.last_error = runtime_payload.get("last_error")
    workspace.exit_code = runtime_payload.get("exit_code")
    workspace.suspended_at = runtime_payload.get("suspended_at")
    workspace.mark_activity()


async def create_workspace_handler(request: web.Request) -> web.Response:
    payload = await request.json()
    requested_cwd = payload.get("working_directory", BASE_DIRECTORY)
    owner_id = payload.get("owner_id", DEFAULT_OWNER_ID)
    name = payload.get("name", DEFAULT_WORKSPACE_NAME)
    repo_url = payload.get("repo_url")
    branch = payload.get("branch")
    commit = payload.get("commit")
    idle_timeout_seconds = payload.get("idle_timeout_seconds", DEFAULT_IDLE_TIMEOUT_SECONDS)
    resource_profile = payload.get("resource_profile", DEFAULT_RESOURCE_PROFILE)

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
    if not isinstance(resource_profile, str) or not resource_profile.strip():
        return web.json_response({"error": "Field 'resource_profile' must be a non-empty string."}, status=400)

    async with request.app["scheduler_lock"]:
        try:
            workspace = create_workspace_record(
                owner_id=owner_id.strip(),
                name=name.strip(),
                requested_cwd=requested_cwd,
                repo_url=repo_url.strip() if isinstance(repo_url, str) else None,
                branch=branch.strip() if isinstance(branch, str) else None,
                commit=commit.strip() if isinstance(commit, str) else None,
                idle_timeout_seconds=idle_timeout_seconds,
                resource_profile=resource_profile.strip(),
            )
            ensure_owner_capacity(workspace)
            worker = await choose_worker(request.app, workspace)
        except ValueError as exc:
            is_capacity_error = any(token in str(exc) for token in ("worker", "healthy", "limit"))
            return web.json_response({"error": str(exc)}, status=409 if is_capacity_error else 400)

        workspace.assigned_worker_id = worker.worker_id
        status_code, runtime_payload = await worker_json_request(
            request.app,
            "POST",
            worker.worker_id,
            "/runtimes",
            json_body={
                "workspace_id": workspace.workspace_id,
                "owner_id": workspace.owner_id,
                "name": workspace.name,
                "shell": workspace.shell_path,
                "created_at": workspace.created_at,
                "home_root": workspace.home_root,
                "project_root": workspace.project_root,
                "container_image": workspace.container_image,
                "container_name": get_container_name(worker.worker_id, workspace.workspace_id),
                "resource_profile": workspace.resource_profile,
                "cpu_limit": workspace.cpu_limit,
                "memory_limit_mb": workspace.memory_limit_mb,
                "repo_url": workspace.repo_url,
                "branch": workspace.branch,
                "commit": workspace.commit,
            },
        )
        if status_code >= 400:
            worker.status = "degraded"
            worker.last_error = runtime_payload.get("error", "runtime create failed")
            return web.json_response(runtime_payload, status=status_code)

        merge_worker_runtime(workspace, runtime_payload)
        WORKSPACES[workspace.workspace_id] = workspace
        await refresh_workers_once(request.app)
        return web.json_response(workspace.to_dict(), status=201)


async def list_workspaces_handler(request: web.Request) -> web.Response:
    workers = [worker.to_dict() for worker in sorted(get_worker_registry(request.app).values(), key=lambda item: item.worker_id)]
    aggregate = {
        "workspace_count": sum(item["active_usage"]["workspace_count"] for item in workers if item["status"] == "healthy"),
        "cpus": sum(item["active_usage"]["cpus"] for item in workers if item["status"] == "healthy"),
        "memory_mb": sum(item["active_usage"]["memory_mb"] for item in workers if item["status"] == "healthy"),
    }
    capacity = {
        "max_active_workspaces": sum(item["capacity_limits"]["max_active_workspaces"] for item in workers if item["status"] == "healthy"),
        "max_active_cpus": sum(item["capacity_limits"]["max_active_cpus"] for item in workers if item["status"] == "healthy"),
        "max_active_memory_mb": sum(item["capacity_limits"]["max_active_memory_mb"] for item in workers if item["status"] == "healthy"),
    }
    return web.json_response(
        {
            "workspaces": [workspace.to_dict() for workspace in WORKSPACES.values()],
            "active_usage": aggregate,
            "capacity_limits": capacity,
            "owner_limits": OWNER_LIMITS,
            "resource_profiles": RESOURCE_PROFILES,
            "workers": workers,
        }
    )


async def get_workspace_handler(request: web.Request) -> web.Response:
    return web.json_response(get_workspace_or_404(request.match_info["workspace_id"]).to_dict())


async def proxy_worker_json(request: web.Request, method: str, suffix: str, *, mark_activity=False) -> web.Response:
    workspace = get_workspace_or_404(request.match_info["workspace_id"])
    if workspace.assigned_worker_id is None:
        return web.json_response({"error": "Workspace is not assigned to a worker."}, status=409)
    params = dict(request.query)
    json_body = None
    if method in {"POST", "PUT"} and request.can_read_body:
        json_body = await request.json()
    status_code, payload = await worker_json_request(
        request.app,
        method,
        workspace.assigned_worker_id,
        f"/runtimes/{workspace.workspace_id}{suffix}",
        params=params or None,
        json_body=json_body,
    )
    if status_code == 503:
        workspace.mark_worker_lost(f"assigned worker {workspace.assigned_worker_id} became unavailable")
        await close_workspace_client_socket(
            request.app,
            workspace.workspace_id,
            f"Assigned worker {workspace.assigned_worker_id} became unavailable. Reconnect later to resume on another worker.",
        )
    if status_code < 400 and mark_activity:
        workspace.mark_activity()
    return web.json_response(payload, status=status_code)


async def list_files_handler(request: web.Request) -> web.Response:
    workspace = get_workspace_or_404(request.match_info["workspace_id"])
    blocked = ensure_workspace_ready_for_io(workspace)
    if blocked is not None:
        return blocked
    return await proxy_worker_json(request, "GET", "/files", mark_activity=True)


async def read_file_handler(request: web.Request) -> web.Response:
    workspace = get_workspace_or_404(request.match_info["workspace_id"])
    blocked = ensure_workspace_ready_for_io(workspace)
    if blocked is not None:
        return blocked
    return await proxy_worker_json(request, "GET", "/file", mark_activity=True)


async def write_file_handler(request: web.Request) -> web.Response:
    workspace = get_workspace_or_404(request.match_info["workspace_id"])
    blocked = ensure_workspace_ready_for_io(workspace)
    if blocked is not None:
        return blocked
    return await proxy_worker_json(request, "PUT", "/file", mark_activity=True)


async def create_directory_handler(request: web.Request) -> web.Response:
    workspace = get_workspace_or_404(request.match_info["workspace_id"])
    blocked = ensure_workspace_ready_for_io(workspace)
    if blocked is not None:
        return blocked
    return await proxy_worker_json(request, "POST", "/directories", mark_activity=True)


async def suspend_workspace_handler(request: web.Request) -> web.Response:
    workspace = get_workspace_or_404(request.match_info["workspace_id"])
    if workspace.assigned_worker_id is None:
        return web.json_response({"error": "Workspace is not assigned to a worker."}, status=409)
    status_code, payload = await worker_json_request(request.app, "POST", workspace.assigned_worker_id, f"/runtimes/{workspace.workspace_id}/suspend")
    if status_code < 400:
        merge_worker_runtime(workspace, payload)
        workspace.websocket_attached = False
        await refresh_workers_once(request.app)
    elif status_code == 503:
        workspace.mark_worker_lost(f"assigned worker {workspace.assigned_worker_id} became unavailable")
    return web.json_response(workspace.to_dict() if status_code < 400 else payload, status=status_code)


async def resume_workspace_handler(request: web.Request) -> web.Response:
    workspace = get_workspace_or_404(request.match_info["workspace_id"])
    if workspace.status not in {"suspended", "worker_lost"}:
        return web.json_response({"error": f"Workspace must be suspended or worker_lost to resume, current status is {workspace.status}."}, status=409)
    async with request.app["scheduler_lock"]:
        try:
            ensure_owner_capacity(workspace, exclude_workspace_id=workspace.workspace_id)
            worker = await choose_worker(request.app, workspace, preferred_worker_id=workspace.assigned_worker_id)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=409)

        workspace.assigned_worker_id = worker.worker_id
        status_code, payload = await worker_json_request(request.app, "POST", worker.worker_id, f"/runtimes/{workspace.workspace_id}/resume")
        if status_code < 400:
            merge_worker_runtime(workspace, payload)
            workspace.websocket_attached = False
            await refresh_workers_once(request.app)
            return web.json_response(workspace.to_dict())
        if status_code == 404:
            create_status, create_payload = await worker_json_request(
                request.app,
                "POST",
                worker.worker_id,
                "/runtimes",
                json_body={
                    "workspace_id": workspace.workspace_id,
                    "owner_id": workspace.owner_id,
                    "name": workspace.name,
                    "shell": workspace.shell_path,
                    "created_at": workspace.created_at,
                    "home_root": workspace.home_root,
                    "project_root": workspace.project_root,
                    "container_image": workspace.container_image,
                    "container_name": get_container_name(worker.worker_id, workspace.workspace_id),
                    "resource_profile": workspace.resource_profile,
                    "cpu_limit": workspace.cpu_limit,
                    "memory_limit_mb": workspace.memory_limit_mb,
                    "repo_url": workspace.repo_url,
                    "branch": workspace.branch,
                    "commit": workspace.commit,
                },
            )
            if create_status < 400:
                merge_worker_runtime(workspace, create_payload)
                await refresh_workers_once(request.app)
                return web.json_response(workspace.to_dict())
            return web.json_response(create_payload, status=create_status)
        return web.json_response(payload, status=status_code)


async def delete_workspace_handler(request: web.Request) -> web.Response:
    workspace_id = request.match_info["workspace_id"]
    workspace = WORKSPACES.pop(workspace_id, None)
    if workspace is None:
        return web.json_response({"error": f"Unknown workspace: {workspace_id}"}, status=404)
    await close_workspace_client_socket(request.app, workspace.workspace_id, "workspace deleted")
    if workspace.assigned_worker_id is not None:
        await worker_json_request(request.app, "DELETE", workspace.assigned_worker_id, f"/runtimes/{workspace.workspace_id}")
        await refresh_workers_once(request.app)
    workspace.status = "deleted"
    workspace.websocket_attached = False
    workspace.mark_activity()
    return web.json_response(workspace.to_dict())


async def workspace_websocket_handler(request: web.Request) -> web.StreamResponse:
    workspace = get_workspace_or_404(request.match_info["workspace_id"])
    blocked = ensure_workspace_ready_for_io(workspace)
    if blocked is not None:
        return blocked
    if workspace.websocket_attached:
        return web.json_response({"error": "A client is already attached to this workspace."}, status=409)

    worker = get_worker_registry(request.app).get(workspace.assigned_worker_id or "")
    if worker is None:
        workspace.mark_worker_lost("assigned worker is unknown")
        return web.json_response({"error": "Assigned worker is not registered."}, status=409)

    session = get_http_session(request.app)
    client_ws = web.WebSocketResponse()
    await client_ws.prepare(request)
    get_active_client_sockets(request.app)[workspace.workspace_id] = client_ws

    try:
        worker_ws = await session.ws_connect(f"{worker.url}/runtimes/{workspace.workspace_id}/ws")
    except aiohttp.ClientError:
        get_active_client_sockets(request.app).pop(workspace.workspace_id, None)
        workspace.mark_worker_lost(f"assigned worker {workspace.assigned_worker_id} became unavailable")
        await client_ws.send_str(json.dumps({"type": "error", "message": workspace.last_error}))
        await client_ws.close()
        return client_ws

    workspace.websocket_attached = True
    workspace.mark_activity()
    print(f"[{utc_now_iso()}] websocket attached for workspace {workspace.workspace_id} on worker {workspace.assigned_worker_id}")

    async def worker_to_client() -> None:
        async for msg in worker_ws:
            if msg.type != WSMsgType.TEXT:
                continue
            await client_ws.send_str(msg.data)
            try:
                event = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "exit":
                break

    async def client_to_worker() -> None:
        async for msg in client_ws:
            if msg.type != WSMsgType.TEXT:
                continue
            workspace.mark_activity()
            await worker_ws.send_str(msg.data)

    worker_task = asyncio.create_task(worker_to_client())
    client_task = asyncio.create_task(client_to_worker())
    _, pending = await asyncio.wait({worker_task, client_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    workspace.websocket_attached = False
    workspace.mark_activity()
    print(f"[{utc_now_iso()}] websocket detached for workspace {workspace.workspace_id}; workspace is now eligible for idle suspension")
    get_active_client_sockets(request.app).pop(workspace.workspace_id, None)
    await worker_ws.close()
    await client_ws.close()
    return client_ws


async def idle_sweeper(app: web.Application) -> None:
    while True:
        await asyncio.sleep(IDLE_SWEEP_INTERVAL_SECONDS)
        now = utc_now()
        for workspace in list(WORKSPACES.values()):
            if workspace.should_suspend_for_idle(now) and workspace.assigned_worker_id:
                print(
                    f"[{utc_now_iso()}] idle sweeper suspending workspace {workspace.workspace_id} "
                    f"after {workspace.idle_timeout_seconds}s idle on worker {workspace.assigned_worker_id}"
                )
                status_code, payload = await worker_json_request(app, "POST", workspace.assigned_worker_id, f"/runtimes/{workspace.workspace_id}/suspend")
                if status_code < 400:
                    merge_worker_runtime(workspace, payload)
                    workspace.websocket_attached = False
                elif status_code == 503:
                    workspace.mark_worker_lost(f"assigned worker {workspace.assigned_worker_id} became unavailable")
                    await close_workspace_client_socket(
                        app,
                        workspace.workspace_id,
                        f"Assigned worker {workspace.assigned_worker_id} became unavailable. Reconnect later to resume on another worker.",
                    )


async def start_background_tasks(app: web.Application) -> None:
    app["idle_sweeper_task"] = asyncio.create_task(idle_sweeper(app))
    app["worker_monitor_task"] = asyncio.create_task(worker_monitor(app))


async def stop_background_tasks(app: web.Application) -> None:
    for key in ("idle_sweeper_task", "worker_monitor_task"):
        task = app.get(key)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def main() -> None:
    os.makedirs(USERS_ROOT, exist_ok=True)
    os.makedirs(EPHEMERAL_ROOT, exist_ok=True)
    app = web.Application()
    app["workers"] = {}
    app["active_client_sockets"] = {}
    app["scheduler_lock"] = asyncio.Lock()
    app.router.add_post("/workers/register", register_worker_handler)
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
    app.on_startup.append(startup_http_session)
    app.on_startup.append(start_background_tasks)
    app.on_shutdown.append(stop_background_tasks)
    app.on_shutdown.append(shutdown_http_session)

    print("Stage 12 fair-scheduler control plane")
    print(f"Listening on http://{HOST}:{PORT}")
    print("Workers register dynamically via POST /workers/register")
    print("Scheduling policy: per-owner admission, then lowest projected dominant worker load with stable tie-breaks")
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
