#!/usr/bin/env python3
import json
import os
import signal
import subprocess
import time
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


HOST = "127.0.0.1"
PORT = 8000
DEFAULT_TIMEOUT_SECONDS = 10
MAX_TIMEOUT_SECONDS = 30
MAX_OUTPUT_CHARS = 20_000
BASE_DIRECTORY = os.getcwd()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def trim_output(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    return text[:MAX_OUTPUT_CHARS], True


class CommandHandler(BaseHTTPRequestHandler):
    server_version = "CommandRunnerHTTP/0.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] {self.address_string()} - {format % args}")

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/commands":
            self.send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "Route not found. Use POST /commands."},
            )
            return

        content_length = self.headers.get("Content-Length")
        if not content_length:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "Missing Content-Length header."},
            )
            return

        try:
            raw_body = self.rfile.read(int(content_length))
            payload = json.loads(raw_body)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": f"Invalid JSON body: {exc}"},
            )
            return

        command = payload.get("command")
        requested_cwd = payload.get("working_directory", BASE_DIRECTORY)
        timeout_seconds = payload.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)

        if not isinstance(command, str) or not command.strip():
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "Field 'command' must be a non-empty string."},
            )
            return

        if not isinstance(requested_cwd, str) or not requested_cwd.strip():
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "Field 'working_directory' must be a non-empty string."},
            )
            return

        if not isinstance(timeout_seconds, (int, float)):
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "Field 'timeout_seconds' must be a number."},
            )
            return

        if timeout_seconds <= 0 or timeout_seconds > MAX_TIMEOUT_SECONDS:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": (
                        f"Field 'timeout_seconds' must be between 0 and "
                        f"{MAX_TIMEOUT_SECONDS} seconds."
                    )
                },
            )
            return

        safe_cwd = os.path.abspath(requested_cwd)
        if not safe_cwd.startswith(BASE_DIRECTORY):
            self.send_json(
                HTTPStatus.FORBIDDEN,
                {
                    "error": (
                        "working_directory must stay inside the demo base directory: "
                        f"{BASE_DIRECTORY}"
                    )
                },
            )
            return

        if not os.path.isdir(safe_cwd):
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": f"working_directory does not exist: {safe_cwd}"},
            )
            return

        command_id = str(uuid.uuid4())
        started_at = utc_now_iso()
        print("")
        print("=" * 80)
        print(f"[{started_at}] starting command {command_id}")
        print(f"cwd: {safe_cwd}")
        print(f"timeout_seconds: {timeout_seconds}")
        print(f"command: {command}")

        start_time = time.perf_counter()
        status = "success"
        exit_code = 0
        stdout = ""
        stderr = ""
        truncated_stdout = False
        truncated_stderr = False

        try:
            completed = subprocess.run(  # noqa: S602
                command,
                shell=True,
                cwd=safe_cwd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            exit_code = completed.returncode
            stdout, truncated_stdout = trim_output(completed.stdout)
            stderr, truncated_stderr = trim_output(completed.stderr)
            if exit_code != 0:
                status = "failed"
        except subprocess.TimeoutExpired as exc:
            status = "timed_out"
            exit_code = -signal.SIGKILL
            stdout, truncated_stdout = trim_output(exc.stdout or "")
            stderr_text = (exc.stderr or "") + "\nProcess exceeded timeout and was terminated.\n"
            stderr, truncated_stderr = trim_output(stderr_text)
        except Exception as exc:  # noqa: BLE001
            status = "server_error"
            exit_code = -1
            stderr = f"Server failed to execute command: {exc}"

        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
        finished_at = utc_now_iso()

        print(f"[{finished_at}] finished command {command_id}")
        print(f"status: {status}")
        print(f"exit_code: {exit_code}")
        print(f"duration_ms: {duration_ms}")
        print("=" * 80)

        self.send_json(
            HTTPStatus.OK,
            {
                "command_id": command_id,
                "status": status,
                "command": command,
                "working_directory": safe_cwd,
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_ms": duration_ms,
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "truncated_stdout": truncated_stdout,
                "truncated_stderr": truncated_stderr,
            },
        )

    def send_json(self, status_code: HTTPStatus, payload: dict) -> None:
        encoded = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), CommandHandler)
    print("Remote command runner demo server")
    print(f"Listening on http://{HOST}:{PORT}")
    print(f"Base directory: {BASE_DIRECTORY}")
    print("Allowed route: POST /commands")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
