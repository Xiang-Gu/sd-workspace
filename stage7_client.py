#!/usr/bin/env python3
import asyncio
import json
import sys

import aiohttp


SERVER_ROOT = "http://127.0.0.1:8006"
DEFAULT_OWNER_ID = "demo-user"
DEFAULT_WORKSPACE_NAME = "demo-workspace"


async def create_workspace(
    http_session: aiohttp.ClientSession,
    owner_id: str,
    name: str,
    working_directory: str,
    repo_url: str,
    branch: str,
    commit: str,
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


async def list_files(
    http_session: aiohttp.ClientSession,
    workspace_id: str,
    path: str = ".",
) -> dict:
    async with http_session.get(
        f"{SERVER_ROOT}/workspaces/{workspace_id}/files",
        params={"path": path},
    ) as response:
        if response.status >= 400:
            raise RuntimeError(await response.text())
        return await response.json()


async def read_file(
    http_session: aiohttp.ClientSession,
    workspace_id: str,
    path: str,
) -> dict:
    async with http_session.get(
        f"{SERVER_ROOT}/workspaces/{workspace_id}/file",
        params={"path": path},
    ) as response:
        if response.status >= 400:
            raise RuntimeError(await response.text())
        return await response.json()


async def write_file(
    http_session: aiohttp.ClientSession,
    workspace_id: str,
    path: str,
    content: str,
) -> dict:
    async with http_session.put(
        f"{SERVER_ROOT}/workspaces/{workspace_id}/file",
        json={"path": path, "content": content},
    ) as response:
        if response.status >= 400:
            raise RuntimeError(await response.text())
        return await response.json()


async def create_directory(
    http_session: aiohttp.ClientSession,
    workspace_id: str,
    path: str,
) -> dict:
    async with http_session.post(
        f"{SERVER_ROOT}/workspaces/{workspace_id}/directories",
        json={"path": path},
    ) as response:
        if response.status >= 400:
            raise RuntimeError(await response.text())
        return await response.json()


async def delete_workspace(http_session: aiohttp.ClientSession, workspace_id: str) -> None:
    async with http_session.delete(f"{SERVER_ROOT}/workspaces/{workspace_id}") as response:
        await response.text()


async def websocket_listener(ws: aiohttp.ClientWebSocketResponse, stop_event: asyncio.Event) -> None:
    async for msg in ws:
        if msg.type != aiohttp.WSMsgType.TEXT:
            continue

        event = json.loads(msg.data)
        if event["type"] == "output":
            sys.stdout.write(event["data"])
            sys.stdout.flush()
            continue

        if event["type"] == "error":
            sys.stdout.write(f"\n[server error] {event['message']}\n")
            sys.stdout.flush()
            stop_event.set()
            return

        if event["type"] == "exit":
            sys.stdout.write(f"\n[workspace runtime exited with code {event['exit_code']}]\n")
            sys.stdout.flush()
            stop_event.set()
            return

    stop_event.set()


def print_help() -> None:
    print("Slash commands:")
    print("  /help")
    print("  /files [path]")
    print("  /read <path>")
    print("  /write <path> <content>")
    print("  /mkdir <path>")
    print("  /status")
    print("  /quit")
    print("Any other input is sent to the workspace shell over WebSocket.")


def print_file_listing(result: dict) -> None:
    print(f"[files at {result['path']}]")
    for entry in result["entries"]:
        print(f"{entry['type']:9} {entry['path']}")


async def handle_slash_command(
    line: str,
    http_session: aiohttp.ClientSession,
    workspace_id: str,
) -> bool:
    parts = line.split(" ", 2)
    command = parts[0]

    try:
        if command == "/help":
            print_help()
            return True

        if command == "/files":
            path = parts[1] if len(parts) > 1 and parts[1] else "."
            result = await list_files(http_session, workspace_id, path)
            print_file_listing(result)
            return True

        if command == "/read":
            if len(parts) < 2 or not parts[1]:
                print("Usage: /read <path>")
                return True
            result = await read_file(http_session, workspace_id, parts[1])
            print(f"[file {result['path']}]")
            print(result["content"])
            return True

        if command == "/write":
            if len(parts) < 3 or not parts[1]:
                print("Usage: /write <path> <content>")
                return True
            result = await write_file(http_session, workspace_id, parts[1], parts[2])
            print(f"[wrote {result['bytes_written']} bytes to {result['path']}]")
            return True

        if command == "/mkdir":
            if len(parts) < 2 or not parts[1]:
                print("Usage: /mkdir <path>")
                return True
            result = await create_directory(http_session, workspace_id, parts[1])
            print(f"[directory {result['status']}: {result['path']}]")
            return True

        if command == "/status":
            result = await get_workspace(http_session, workspace_id)
            print(json.dumps(result, indent=2))
            return True
    except RuntimeError as exc:
        print(exc)
        return True

    return False


async def main() -> int:
    print("Persistent home + ephemeral project demo client")
    print("This client creates one workspace, bootstraps an ephemeral project, and retains only HOME across recreation.")
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

    async with aiohttp.ClientSession() as http_session:
        try:
            workspace = await create_workspace(
                http_session,
                owner_id=owner_id,
                name=name,
                working_directory=working_directory,
                repo_url=repo_url,
                branch=branch,
                commit=commit,
            )
        except aiohttp.ClientError as exc:
            print(f"Failed to reach server: {exc}")
            print("Start stage7_server.py in another terminal first.")
            return 1
        except RuntimeError as exc:
            print(exc)
            return 1

        workspace_id = workspace["workspace_id"]
        workspace_snapshot = await get_workspace(http_session, workspace_id)
        workspace_list = await list_workspaces(http_session)
        root_listing = await list_files(http_session, workspace_id, ".")

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
        print(f"home_persistent: {workspace_snapshot['home_persistent']}")
        print(f"project_persistent: {workspace_snapshot['project_persistent']}")
        print(f"active workspaces for demo server: {len(workspace_list['workspaces'])}")
        print_file_listing(root_listing)
        print("")

        if workspace_snapshot["status"] != "ready":
            print("Workspace did not become ready. Use /status to inspect the error.")
            await delete_workspace(http_session, workspace_id)
            return 1

        stop_event = asyncio.Event()

        try:
            async with http_session.ws_connect(f"{SERVER_ROOT}/workspaces/{workspace_id}/ws") as ws:
                listener_task = asyncio.create_task(websocket_listener(ws, stop_event))

                try:
                    while not stop_event.is_set():
                        line = await asyncio.to_thread(input)
                        if stop_event.is_set():
                            break
                        if line == "/quit":
                            break
                        if line.startswith("/"):
                            handled = await handle_slash_command(line, http_session, workspace_id)
                            if handled:
                                continue
                        await ws.send_str(json.dumps({"type": "input", "data": line + "\n"}))
                finally:
                    stop_event.set()
                    listener_task.cancel()
                    try:
                        await listener_task
                    except asyncio.CancelledError:
                        pass
        except aiohttp.ClientError as exc:
            print(f"WebSocket error: {exc}")
        finally:
            await delete_workspace(http_session, workspace_id)
            print("Client closed. Home storage was retained; project storage was removed.")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
