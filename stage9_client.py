#!/usr/bin/env python3
import asyncio
import json
import sys

import aiohttp


SERVER_ROOT = "http://127.0.0.1:8008"
DEFAULT_OWNER_ID = "demo-user"
DEFAULT_WORKSPACE_NAME = "demo-workspace"
DEFAULT_IDLE_TIMEOUT_SECONDS = 30


async def create_workspace(
    http_session: aiohttp.ClientSession,
    owner_id: str,
    name: str,
    working_directory: str,
    repo_url: str,
    branch: str,
    commit: str,
    idle_timeout_seconds: int,
) -> dict:
    async with http_session.post(
        f"{SERVER_ROOT}/workspaces",
        json={
            "owner_id": owner_id,
            "name": name,
            "working_directory": working_directory or ".",
            "repo_url": repo_url or None,
            "branch": branch or None,
            "commit": commit or None,
            "idle_timeout_seconds": idle_timeout_seconds,
        },
    ) as response:
        if response.status >= 400:
            raise RuntimeError(await response.text())
        return await response.json()


async def get_workspace(http_session: aiohttp.ClientSession, workspace_id: str) -> dict:
    async with http_session.get(f"{SERVER_ROOT}/workspaces/{workspace_id}") as response:
        if response.status >= 400:
            raise RuntimeError(await response.text())
        return await response.json()


async def list_workspaces(http_session: aiohttp.ClientSession) -> dict:
    async with http_session.get(f"{SERVER_ROOT}/workspaces") as response:
        if response.status >= 400:
            raise RuntimeError(await response.text())
        return await response.json()


async def list_files(http_session: aiohttp.ClientSession, workspace_id: str, path: str = ".") -> dict:
    async with http_session.get(
        f"{SERVER_ROOT}/workspaces/{workspace_id}/files",
        params={"path": path},
    ) as response:
        if response.status >= 400:
            raise RuntimeError(await response.text())
        return await response.json()


async def read_file(http_session: aiohttp.ClientSession, workspace_id: str, path: str) -> dict:
    async with http_session.get(
        f"{SERVER_ROOT}/workspaces/{workspace_id}/file",
        params={"path": path},
    ) as response:
        if response.status >= 400:
            raise RuntimeError(await response.text())
        return await response.json()


async def write_file(http_session: aiohttp.ClientSession, workspace_id: str, path: str, content: str) -> dict:
    async with http_session.put(
        f"{SERVER_ROOT}/workspaces/{workspace_id}/file",
        json={"path": path, "content": content},
    ) as response:
        if response.status >= 400:
            raise RuntimeError(await response.text())
        return await response.json()


async def create_directory(http_session: aiohttp.ClientSession, workspace_id: str, path: str) -> dict:
    async with http_session.post(
        f"{SERVER_ROOT}/workspaces/{workspace_id}/directories",
        json={"path": path},
    ) as response:
        if response.status >= 400:
            raise RuntimeError(await response.text())
        return await response.json()


async def suspend_workspace(http_session: aiohttp.ClientSession, workspace_id: str) -> dict:
    async with http_session.post(f"{SERVER_ROOT}/workspaces/{workspace_id}/suspend") as response:
        if response.status >= 400:
            raise RuntimeError(await response.text())
        return await response.json()


async def resume_workspace(http_session: aiohttp.ClientSession, workspace_id: str) -> dict:
    async with http_session.post(f"{SERVER_ROOT}/workspaces/{workspace_id}/resume") as response:
        if response.status >= 400:
            raise RuntimeError(await response.text())
        return await response.json()


async def delete_workspace(http_session: aiohttp.ClientSession, workspace_id: str) -> None:
    async with http_session.delete(f"{SERVER_ROOT}/workspaces/{workspace_id}") as response:
        await response.text()


def select_existing_workspace(workspaces: list[dict], owner_id: str, name: str) -> dict | None:
    matches = [
        workspace
        for workspace in workspaces
        if workspace.get("owner_id") == owner_id and workspace.get("name") == name
    ]
    if not matches:
        return None
    matches.sort(key=lambda workspace: workspace.get("created_at", ""))
    return matches[-1]


async def websocket_listener(ws: aiohttp.ClientWebSocketResponse, shell_closed: asyncio.Event) -> None:
    async for msg in ws:
        if msg.type != aiohttp.WSMsgType.TEXT:
            continue

        event = json.loads(msg.data)
        event_type = event.get("type")

        if event_type == "output":
            sys.stdout.write(event["data"])
            sys.stdout.flush()
            continue

        if event_type == "error":
            sys.stdout.write(f"\n[server error] {event['message']}\n")
            sys.stdout.flush()
            shell_closed.set()
            return

        if event_type == "suspended":
            sys.stdout.write("\n[workspace suspended; shell disconnected]\n")
            sys.stdout.flush()
            continue

        if event_type == "exit":
            sys.stdout.write(f"\n[workspace runtime exited with code {event['exit_code']}]\n")
            sys.stdout.flush()
            shell_closed.set()
            return

    shell_closed.set()


def print_help() -> None:
    print("Slash commands:")
    print("  /help")
    print("  /files [path]")
    print("  /read <path>")
    print("  /write <path> <content>")
    print("  /mkdir <path>")
    print("  /status")
    print("  /suspend")
    print("  /resume")
    print("  /delete")
    print("  /quit")
    print("Any other input is sent to the workspace shell over WebSocket when attached.")


def print_file_listing(result: dict) -> None:
    print(f"[files at {result['path']}]")
    for entry in result["entries"]:
        print(f"{entry['type']:9} {entry['path']}")


def print_workspace_summary(workspace_snapshot: dict, workspace_count: int) -> None:
    print("")
    print(f"workspace_id: {workspace_snapshot['workspace_id']}")
    print(f"owner_id: {workspace_snapshot['owner_id']}")
    print(f"name: {workspace_snapshot['name']}")
    print(f"status: {workspace_snapshot['status']}")
    print(f"bootstrap_status: {workspace_snapshot['bootstrap_status']}")
    print(f"repo_url: {workspace_snapshot['repo_url']}")
    print(f"branch: {workspace_snapshot['branch']}")
    print(f"commit: {workspace_snapshot['commit']}")
    print(f"home_root: {workspace_snapshot['home_root']}")
    print(f"project_root: {workspace_snapshot['project_root']}")
    print(f"shell cwd: {workspace_snapshot['shell_working_directory']}")
    print(f"idle_timeout_seconds: {workspace_snapshot['idle_timeout_seconds']}")
    print(f"active workspaces for demo server: {workspace_count}")
    print("")


async def connect_shell(
    http_session: aiohttp.ClientSession,
    workspace_id: str,
) -> tuple[aiohttp.ClientWebSocketResponse | None, asyncio.Task | None, asyncio.Event]:
    shell_closed = asyncio.Event()
    try:
        ws = await http_session.ws_connect(f"{SERVER_ROOT}/workspaces/{workspace_id}/ws")
    except aiohttp.ClientResponseError as exc:
        print(f"Shell attach rejected: {exc.status} {exc.message}")
        shell_closed.set()
        return None, None, shell_closed
    except aiohttp.ClientError as exc:
        print(f"WebSocket error: {exc}")
        shell_closed.set()
        return None, None, shell_closed

    listener_task = asyncio.create_task(websocket_listener(ws, shell_closed))
    return ws, listener_task, shell_closed


async def close_shell(
    ws: aiohttp.ClientWebSocketResponse | None,
    listener_task: asyncio.Task | None,
) -> None:
    if ws is not None and not ws.closed:
        await ws.close()
    if listener_task is not None:
        listener_task.cancel()
        try:
            await listener_task
        except asyncio.CancelledError:
            pass


async def handle_slash_command(
    line: str,
    http_session: aiohttp.ClientSession,
    workspace_id: str,
) -> tuple[bool, str | None]:
    parts = line.split(" ", 2)
    command = parts[0]

    try:
        if command == "/help":
            print_help()
            return True, None

        if command == "/files":
            path = parts[1] if len(parts) > 1 and parts[1] else "."
            result = await list_files(http_session, workspace_id, path)
            print_file_listing(result)
            return True, None

        if command == "/read":
            if len(parts) < 2 or not parts[1]:
                print("Usage: /read <path>")
                return True, None
            result = await read_file(http_session, workspace_id, parts[1])
            print(f"[file {result['path']}]")
            print(result["content"])
            return True, None

        if command == "/write":
            if len(parts) < 3 or not parts[1]:
                print("Usage: /write <path> <content>")
                return True, None
            result = await write_file(http_session, workspace_id, parts[1], parts[2])
            print(f"[wrote {result['bytes_written']} bytes to {result['path']}]")
            return True, None

        if command == "/mkdir":
            if len(parts) < 2 or not parts[1]:
                print("Usage: /mkdir <path>")
                return True, None
            result = await create_directory(http_session, workspace_id, parts[1])
            print(f"[directory {result['status']}: {result['path']}]")
            return True, None

        if command == "/status":
            result = await get_workspace(http_session, workspace_id)
            print(json.dumps(result, indent=2))
            return True, None

        if command == "/suspend":
            result = await suspend_workspace(http_session, workspace_id)
            print(f"[workspace suspended: {result['status']}]")
            return True, "suspend"

        if command == "/resume":
            result = await resume_workspace(http_session, workspace_id)
            print(f"[workspace resumed: {result['status']}]")
            return True, "resume"

        if command == "/delete":
            await delete_workspace(http_session, workspace_id)
            print("[workspace deleted]")
            return True, "delete"
    except RuntimeError as exc:
        print(exc)
        return True, None

    return False, None


async def main() -> int:
    print("Containerized workspace demo client")
    print("This client reuses an existing workspace when owner and name match, and talks to a shell running inside a per-workspace container.")
    print("Type /help for client-side commands.")
    print("")

    owner_id = (await asyncio.to_thread(input, f"Owner ID (press Enter for {DEFAULT_OWNER_ID}): ")).strip() or DEFAULT_OWNER_ID
    name = (await asyncio.to_thread(input, f"Workspace name (press Enter for {DEFAULT_WORKSPACE_NAME}): ")).strip() or DEFAULT_WORKSPACE_NAME
    working_directory = (
        await asyncio.to_thread(
            input,
            "Base directory hint (press Enter to use the current demo directory on the server): ",
        )
    ).strip()
    repo_url = (await asyncio.to_thread(input, "Repo URL (optional): ")).strip()
    branch = (await asyncio.to_thread(input, "Branch (optional): ")).strip()
    commit = (await asyncio.to_thread(input, "Commit (optional, overrides branch): ")).strip()
    idle_timeout_raw = (
        await asyncio.to_thread(input, f"Idle timeout seconds (press Enter for {DEFAULT_IDLE_TIMEOUT_SECONDS}): ")
    ).strip()

    try:
        idle_timeout_seconds = int(idle_timeout_raw) if idle_timeout_raw else DEFAULT_IDLE_TIMEOUT_SECONDS
    except ValueError:
        print("Idle timeout must be an integer.")
        return 1

    async with aiohttp.ClientSession() as http_session:
        try:
            workspace_list = await list_workspaces(http_session)
        except aiohttp.ClientError as exc:
            print(f"Failed to reach server: {exc}")
            print("Start stage9_server.py in another terminal first.")
            return 1
        except RuntimeError as exc:
            print(exc)
            return 1

        existing_workspace = select_existing_workspace(workspace_list["workspaces"], owner_id, name)
        created_new_workspace = False

        try:
            if existing_workspace is None:
                workspace = await create_workspace(
                    http_session,
                    owner_id=owner_id,
                    name=name,
                    working_directory=working_directory,
                    repo_url=repo_url,
                    branch=branch,
                    commit=commit,
                    idle_timeout_seconds=idle_timeout_seconds,
                )
                created_new_workspace = True
            else:
                workspace = existing_workspace
                print(
                    f"Reusing existing workspace {workspace['workspace_id']} with status {workspace['status']}."
                )
                if workspace["status"] == "suspended":
                    workspace = await resume_workspace(http_session, workspace["workspace_id"])
                    print(f"Resumed suspended workspace {workspace['workspace_id']}.")
        except RuntimeError as exc:
            print(exc)
            return 1

        workspace_id = workspace["workspace_id"]
        workspace_snapshot = await get_workspace(http_session, workspace_id)
        workspace_list = await list_workspaces(http_session)
        print_workspace_summary(workspace_snapshot, len(workspace_list["workspaces"]))
        if created_new_workspace:
            print("Created a new workspace for this owner/name.")
        else:
            print("Attached to an existing logical workspace.")

        ws = None
        listener_task = None
        shell_closed = asyncio.Event()
        deleted_workspace = False

        if workspace_snapshot["status"] == "ready":
            ws, listener_task, shell_closed = await connect_shell(http_session, workspace_id)
        else:
            shell_closed.set()
            print("Workspace did not become ready. Use /status to inspect it, then /quit to clean up.")

        try:
            while True:
                if shell_closed.is_set() and ws is not None:
                    await close_shell(ws, listener_task)
                    ws = None
                    listener_task = None

                line = await asyncio.to_thread(input)

                if line == "/quit":
                    break

                if line.startswith("/"):
                    handled, lifecycle_action = await handle_slash_command(line, http_session, workspace_id)
                    if handled:
                        if lifecycle_action == "suspend":
                            shell_closed.set()
                        elif lifecycle_action == "resume":
                            await close_shell(ws, listener_task)
                            ws, listener_task, shell_closed = await connect_shell(http_session, workspace_id)
                        elif lifecycle_action == "delete":
                            deleted_workspace = True
                            break
                        continue

                if ws is None or shell_closed.is_set():
                    print("Shell is not currently attached. Use /resume if the workspace is suspended.")
                    continue

                await ws.send_str(json.dumps({"type": "input", "data": line + "\n"}))
        finally:
            await close_shell(ws, listener_task)
            if deleted_workspace:
                print("Client closed after deleting the workspace.")
            else:
                print("Client detached. The workspace remains on the server until /delete or idle suspend.")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
