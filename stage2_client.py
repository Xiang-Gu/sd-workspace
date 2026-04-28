#!/usr/bin/env python3
import asyncio
import json
import sys

import aiohttp


SERVER_ROOT = "http://127.0.0.1:8001"


async def create_session(
    http_session: aiohttp.ClientSession, working_directory: str
) -> dict:
    async with http_session.post(
        f"{SERVER_ROOT}/sessions",
        json={"working_directory": working_directory or "."},
    ) as response:
        if response.status >= 400:
            raise RuntimeError(await response.text())
        return await response.json()


async def delete_session(http_session: aiohttp.ClientSession, session_id: str) -> None:
    async with http_session.delete(f"{SERVER_ROOT}/sessions/{session_id}") as response:
        await response.text()


async def websocket_listener(
    ws: aiohttp.ClientWebSocketResponse, stop_event: asyncio.Event
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
                f"\n[remote session exited with code {event['exit_code']}]\n"
            )
            sys.stdout.flush()
            stop_event.set()
            return

    stop_event.set()


async def main() -> int:
    print("Persistent remote shell demo client")
    print("This client keeps one shell session alive on the server.")
    print("Type commands and they will run inside the same remote shell.")
    print("Type /quit to close the client and delete the remote session.")
    print("")

    working_directory = (
        await asyncio.to_thread(
            input,
            "Working directory (press Enter to use the current demo directory on the server): ",
        )
    ).strip()

    async with aiohttp.ClientSession() as http_session:
        try:
            session = await create_session(http_session, working_directory)
        except aiohttp.ClientError as exc:
            print(f"Failed to reach server: {exc}")
            print("Start stage2_server.py in another terminal first.")
            return 1
        except RuntimeError as exc:
            print(exc)
            return 1

        session_id = session["session_id"]
        print("")
        print(f"session_id: {session_id}")
        print(f"remote shell: {session['shell']}")
        print(f"remote cwd: {session['working_directory']}")
        print("")

        stop_event = asyncio.Event()

        try:
            async with http_session.ws_connect(
                f"{SERVER_ROOT}/sessions/{session_id}/ws"
            ) as ws:
                listener_task = asyncio.create_task(websocket_listener(ws, stop_event))

                try:
                    while not stop_event.is_set():
                        line = await asyncio.to_thread(input)
                        if stop_event.is_set():
                            break
                        if line == "/quit":
                            break
                        await ws.send_str(
                            json.dumps({"type": "input", "data": line + "\n"})
                        )
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
            await delete_session(http_session, session_id)
            print("Client closed.")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
