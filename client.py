#!/usr/bin/env python3
import json
import sys
import urllib.error
import urllib.request


SERVER_URL = "http://127.0.0.1:8000/commands"
DEFAULT_TIMEOUT_SECONDS = 10


def send_command(command: str, working_directory: str, timeout_seconds: int) -> dict:
    payload = json.dumps(
        {
            "command": command,
            "working_directory": working_directory,
            "timeout_seconds": timeout_seconds,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        SERVER_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=timeout_seconds + 5) as response:
        return json.loads(response.read().decode("utf-8"))


def print_result(result: dict) -> None:
    print("")
    print("-" * 80)
    print(f"command_id: {result['command_id']}")
    print(f"status: {result['status']}")
    print(f"exit_code: {result['exit_code']}")
    print(f"working_directory: {result['working_directory']}")
    print(f"duration_ms: {result['duration_ms']}")
    print("")
    print("stdout:")
    print(result["stdout"] or "<empty>")
    print("")
    print("stderr:")
    print(result["stderr"] or "<empty>")
    if result.get("truncated_stdout") or result.get("truncated_stderr"):
        print("")
        print("Note: output was truncated by the server.")
    print("-" * 80)


def main() -> int:
    print("Remote command runner demo client")
    print("Type a shell command and press Enter.")
    print("Type 'exit' or 'quit' to stop.")
    print("")

    current_directory = input(
        "Working directory "
        "(press Enter to use the current demo directory on the server): "
    ).strip()

    while True:
        command = input("client> ").strip()
        if command in {"exit", "quit"}:
            print("Exiting client.")
            return 0
        if not command:
            continue

        try:
            result = send_command(
                command=command,
                working_directory=current_directory or ".",
                timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
            )
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            print("")
            print(f"HTTP error {exc.code}")
            print(body)
            print("")
            continue
        except urllib.error.URLError as exc:
            print("")
            print(f"Failed to reach server: {exc.reason}")
            print("Start server.py in another terminal first.")
            print("")
            continue
        except KeyboardInterrupt:
            print("\nExiting client.")
            return 0

        print_result(result)


if __name__ == "__main__":
    sys.exit(main())
