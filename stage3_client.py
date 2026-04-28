#!/usr/bin/env python3
import asyncio
import json
import sys

import aiohttp


SERVER_ROOT = "http://127.0.0.1:8002"
DEFAULT_OWNER_ID = "demo-user"
DEFAULT_WORKSPACE_NAME = "demo-workspace"


async def create_workspace(
    http_session: aiohttp.ClientSession,
    owner_id: str,
    name: str,
    working_directory: str,
) -> dict:
    async with http_session.post(
        f"{SERVER_ROOT}/workspaces",
        json={
            "owner_id": owner_id,
            "name": name,
            "working_directory": working_directory or ".",
        },
    ) as response:
        if response.status >= 400:
            raise RuntimeError(await response.text())
        return await response.json()


async def get_workspace(
    http_session: aiohttp.ClientSession,
    workspace_id: str,
) -> dict:
    async with http_session.get(f"{SERVER_ROOT}/workspaces/{workspace_id}") as response:
        if response.status >= 400:
            raise RuntimeError(await response.text())
        return await response.json()


async def list_workspaces(http_session: aiohttp.ClientSession) -> dict:
    async with http_session.get(f"{SERVER_ROOT}/workspaces") as response:
        if response.status >= 400:
            raise RuntimeError(await response.text())
        return await response.json()


async def delete_workspace(
    http_session: aiohttp.ClientSession,
    workspace_id: str,
) -> None:
    async with http_session.delete(f"{SERVER_ROOT}/workspaces/{workspace_id}") as response:
        await response.text()


async def websocket_listener(
    ws: aiohttp.ClientWebSocketResponse,
    stop_event: asyncio.Event,
) -> None:
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
            sys.stdout.write(
                f"\n[workspace runtime exited with code {event['exit_code']}]\n"
            )
            sys.stdout.flush()
            stop_event.set()
            return

    stop_event.set()


async def main() -> int:
    print("Workspace-based remote shell demo client")
    print("This client creates one workspace and attaches to its shell runtime.")
    print("Type commands and they will run inside the workspace shell.")
    print("Type /quit to close the client and delete the workspace.")
    print("")

    owner_id = (
        await asyncio.to_thread(
            input,
            f"Owner ID (press Enter for {DEFAULT_OWNER_ID}): ",
        )
    ).strip() or DEFAULT_OWNER_ID
    name = (
        await asyncio.to_thread(
            input,
            f"Workspace name (press Enter for {DEFAULT_WORKSPACE_NAME}): ",
        )
    ).strip() or DEFAULT_WORKSPACE_NAME
    working_directory = (
        await asyncio.to_thread(
            input,
            "Working directory (press Enter to use the current demo directory on the server): ",
        )
    ).strip()

    async with aiohttp.ClientSession() as http_session:
        try:
            workspace = await create_workspace(
                http_session,
                owner_id=owner_id,
                name=name,
                working_directory=working_directory,
            )
        except aiohttp.ClientError as exc:
            print(f"Failed to reach server: {exc}")
            print("Start stage3_server.py in another terminal first.")
            return 1
        except RuntimeError as exc:
            print(exc)
            return 1

        workspace_id = workspace["workspace_id"]
        workspace_snapshot = await get_workspace(http_session, workspace_id)
        workspace_list = await list_workspaces(http_session)

        print("")
        print(f"workspace_id: {workspace_snapshot['workspace_id']}")
        print(f"owner_id: {workspace_snapshot['owner_id']}")
        print(f"name: {workspace_snapshot['name']}")
        print(f"status: {workspace_snapshot['status']}")
        print(f"remote shell: {workspace_snapshot['shell']}")
        print(f"remote cwd: {workspace_snapshot['working_directory']}")
        print(f"active workspaces for demo server: {len(workspace_list['workspaces'])}")
        print("")

        stop_event = asyncio.Event()

        try:
            async with http_session.ws_connect(
                f"{SERVER_ROOT}/workspaces/{workspace_id}/ws"
            ) as ws:
                listener_task = asyncio.create_task(websocket_listener(ws, stop_event))

                try:
                    while not stop_event.is_set():
                        line = await asyncio.to_thread(input)
                        if stop_event.is_set():
                            break
                        if line == "/quit":
                            break
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
            print("Client closed.")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
