# Local Codespaces Design Exploration

Start with [system_design_human.md](system_design_human.md). That file summarizes what this project is about, the system-design approach, and the mental model behind the implementation.

The rest of this README is the detailed stage-by-stage build log for the 13 local prototypes.

## Stage 1: Remote Command Runner Demo

This is a minimal localhost demo of the Stage 1 system design:

- `server.py` exposes `POST /commands` over HTTP on `127.0.0.1:8000`
- `client.py` is an interactive terminal client
- the server executes one shell command per request and returns:
  - `stdout`
  - `stderr`
  - `exit_code`
  - `status`

## Run It

Open terminal 1 and start the server:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 server.py
```

Open terminal 2 and start the client:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 client.py
```

Then type commands such as:

```bash
pwd
ls -la
echo hello from the server
sleep 2
python3 --version
```

## What You Should See

- the client terminal sends real HTTP requests to `localhost`
- the server terminal logs when a command starts and finishes
- the client prints the returned JSON fields in a human-friendly format

## Demo Limitations

This is intentionally a learning demo, not a safe production design.

- it uses `shell=True`
- it only supports one-shot commands, not interactive shells
- it does not isolate users
- it restricts working directories to stay under the current project directory
- it truncates very large output
- it kills commands that exceed the timeout

## Good Commands To Try

```bash
pwd
ls
ls does-not-exist
sleep 20
find .
```

The `sleep 20` example should time out because the default timeout is 10 seconds.

## Stage 2: Persistent Shell Session Demo

This version keeps one shell alive across many commands by attaching it to a PTY.

- `stage2_server.py` exposes a session API plus a WebSocket on `127.0.0.1:8001`
- `stage2_client.py` creates one shell session and attaches to the WebSocket
- commands like `cd` persist because the same shell process stays alive

### Run It

Open terminal 1:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage2_server.py
```

Open terminal 2:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage2_client.py
```

Then try:

```bash
pwd
cd ..
pwd
cd codespaces
pwd
echo hello from the persistent shell
python3 --version
```

Use `/quit` in the client to close the local client and delete the remote session.

### Stage 2 API

- `POST /sessions`
- `GET /sessions/{id}/ws`
- `DELETE /sessions/{id}`

### Stage 2 Limitations

- output is pushed over a WebSocket, but the client still reads local stdin line by line
- the server keeps sessions only in memory
- if the server restarts, sessions are lost
- the client sends whole lines over the WebSocket, not raw keystrokes
- the shell prompt is intentionally suppressed to keep the terminal output readable

## Stage 3: Workspace-Based Shell Demo

This version turns the shell session into a named workspace with metadata and lifecycle APIs.

- `stage3_server.py` exposes workspace APIs on `127.0.0.1:8002`
- `stage3_client.py` creates one workspace, prints its metadata, and attaches to its shell runtime
- the shell is now treated as one internal component of the workspace instead of the top-level object

### Run It

Open terminal 1:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage3_server.py
```

Open terminal 2:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage3_client.py
```

Then try:

```bash
pwd
cd ..
pwd
cd codespaces
pwd
```

Use `/quit` in the client to close the local client and delete the workspace.

### Stage 3 API

- `POST /workspaces`
- `GET /workspaces`
- `GET /workspaces/{id}`
- `GET /workspaces/{id}/ws`
- `DELETE /workspaces/{id}`

### Stage 3 Notes

- each workspace has metadata like `workspace_id`, `owner_id`, `name`, `status`, and timestamps
- the runtime shell is still PTY-backed and long-lived, but it is no longer the public abstraction
- workspaces stay in memory only; there is still no persistent database
- this is the right stepping stone before adding a filesystem API

## Stage 4: Workspace Plus Filesystem Demo

This version keeps the Stage 3 workspace abstraction and adds a minimal filesystem API scoped to each workspace root.

- `stage4_server.py` exposes workspace lifecycle APIs plus file APIs on `127.0.0.1:8003`
- `stage4_client.py` attaches to the workspace shell and also supports file slash commands
- file paths are restricted to stay inside the workspace root directory

### Run It

Open terminal 1:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage4_server.py
```

Open terminal 2:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage4_client.py
```

Then try:

```text
/files
/mkdir notes
/write notes/todo.txt learn stage 4
/read notes/todo.txt
cat notes/todo.txt
/status
```

Use `/quit` in the client to close the local client and delete the workspace.

### Stage 4 API

- `POST /workspaces`
- `GET /workspaces`
- `GET /workspaces/{id}`
- `GET /workspaces/{id}/files?path=.`
- `GET /workspaces/{id}/file?path=notes/todo.txt`
- `PUT /workspaces/{id}/file`
- `POST /workspaces/{id}/directories`
- `GET /workspaces/{id}/ws`
- `DELETE /workspaces/{id}`

### Stage 4 Notes

- shell access and file access now both hang off the same workspace object
- the file API is still local and in-memory from a metadata point of view; there is no database
- writes happen directly on the local filesystem under the workspace root
- this is the right stepping stone before adding repo bootstrap or persistent home directories

## Stage 5: Repo-Aware Workspace Demo

This version extends Stage 4 with optional Git bootstrap during workspace creation.

- `stage5_server.py` exposes the same workspace + file APIs on `127.0.0.1:8004`
- `stage5_client.py` can optionally supply `repo_url`, `branch`, and `commit`
- if a repo is provided, the server clones it into `workspace_root/repo` before marking the workspace ready

### Run It

Open terminal 1:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage5_server.py
```

Open terminal 2:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage5_client.py
```

You can leave repo fields blank for a normal workspace, or use a public repo URL and optional branch/commit.

Then try:

```text
/status
/files
/files repo
pwd
ls
```

If you cloned a repo, the shell starts in `workspace_root/repo`.

### Stage 5 Metadata Additions

- `repo_url`
- `branch`
- `commit`
- `repo_path`
- `bootstrap_status`
- `workspace_root`
- `project_root`
- `shell_working_directory`

### Stage 5 Notes

- repo bootstrap happens synchronously during `POST /workspaces`
- if clone or checkout fails, the workspace is created in `error` state with `last_error`
- when a repo is bootstrapped, both the shell and the file API default to the repo directory as the user-facing project root
- this is the right stepping stone before separating persistent home from ephemeral repo workspace

## Stage 6: Persistent Home And Persistent Project Demo

This version follows the storage model you preferred: both the user home and the project directory persist across workspace deletion/recreation, while the shell runtime remains ephemeral.

- `stage6_server.py` stores persistent data under `.stage6_data/users/...`
- `stage6_client.py` can recreate the same workspace by using the same `owner_id` and `name`
- if a repo is bootstrapped once, recreating the same workspace reuses that persistent repo instead of recloning it

### Run It

Open terminal 1:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage6_server.py
```

Open terminal 2:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage6_client.py
```

Try creating a workspace, writing a file, quitting, then recreating the same workspace with the same owner/name and reading the file again.

### Stage 6 Metadata Additions

- `home_root`
- `project_root`
- `storage_persistent`
- `base_directory`

### Stage 6 Notes

- the shell process is still ephemeral, but storage is retained across `DELETE /workspaces/{id}`
- repo bootstrap clones directly into `project_root`
- recreating the same workspace name for the same owner reuses the same persistent project directory

## Stage 7: Persistent Home And Ephemeral Project Demo

This version switches back to the original storage model: the user home persists, but the project root is ephemeral and recreated for each workspace instance.

- `stage7_server.py` stores persistent home data under `.stage7_data/users/...`
- each workspace gets a fresh ephemeral `project_root` under `.stage7_data/ephemeral/workspaces/<workspace_id>/project`
- repo bootstrap clones into that ephemeral project root, so reconnecting requires recloning or rebootstrap

### Run It

Open terminal 1:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage7_server.py
```

Open terminal 2:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage7_client.py
```

Try creating a workspace, writing a project file, writing a file under `~` from the shell, quitting, then recreating the same owner/name:

- the project file should be gone
- the file under `~` should still exist

### Stage 7 Notes

- `home_root` is persistent per user
- `project_root` is ephemeral per workspace instance
- repo bootstrap clones into the ephemeral `project_root`
- `DELETE /workspaces/{id}` removes project storage but retains home storage

## Stage 8: Idle Suspend/Resume Demo

This version keeps the Stage 7 storage semantics and adds workspace lifecycle management.

- `stage8_server.py` adds an idle sweeper that suspends `ready` workspaces after a timeout
- `stage8_client.py` supports `/suspend`, `/resume`, and `/status`
- suspended workspaces keep their metadata and persistent home, but lose runtime and project storage until resumed

### Run It

Open terminal 1:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage8_server.py
```

Open terminal 2:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage8_client.py
```

Try:

```text
/status
/write notes/check.txt hello
/suspend
/status
/resume
/status
```

Or set a short idle timeout and simply wait until the workspace becomes suspended.

### Stage 8 Notes

- new statuses include `suspending`, `suspended`, and `resuming`
- `POST /workspaces/{id}/resume` rebuilds the ephemeral project root and runtime
- `POST /workspaces/{id}/suspend` is available for manual testing in addition to idle auto-suspend
- file APIs and shell attachment reject requests while the workspace is suspended
- this is the first stage where the logical workspace outlives the runtime
- the client now treats `owner_id + name` as the logical workspace identity: startup reuses an existing workspace when present, `/quit` only detaches, and `/delete` is the explicit destructive action

## Stage 9: Containerized Workspace Runtime Demo

This version keeps the Stage 8 lifecycle semantics, but moves the workspace shell into a per-workspace container instead of running it directly on the host.

- `stage9_server.py` runs on `127.0.0.1:8008`
- each workspace runtime is a `docker run` process with bind-mounted `home_root` and `project_root`
- the shell runs inside the container, while the file API still operates on the host paths that are mounted into that container

### Run It

Open terminal 1:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage9_server.py
```

Open terminal 2:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage9_client.py
```

Then try:

```text
/status
/files
pwd
echo hello > ~/from-container.txt
/suspend
/resume
/status
```

### Stage 9 Notes

- the runtime kind is now `container`
- the default container image is `alpine:3.20`
- `home_root` is bind-mounted into the container at `/home/workspace-user`
- `project_root` is bind-mounted into the container at `/workspace`
- suspend/resume still destroys and rebuilds the runtime; it now does that by removing and recreating the container

## Stage 10: Resource-Limited Workspace Demo

This version keeps the Stage 9 container runtime, but adds resource profiles and simple admission control.

- `stage10_server.py` runs on `127.0.0.1:8009`
- each workspace chooses a profile: `small`, `medium`, or `large`
- the server passes CPU and memory limits to `docker run`
- create/resume is rejected when total active usage would exceed the single-node capacity budget

### Run It

Open terminal 1:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage10_server.py
```

Open terminal 2:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage10_client.py
```

Then try:

```text
/status
/suspend
/resume
/delete
```

And create multiple workspaces with different profiles to trigger admission failures once the host budget is full.

### Stage 10 Notes

- `small` = `0.5` CPU / `512 MB`
- `medium` = `1.0` CPU / `1024 MB`
- `large` = `2.0` CPU / `2048 MB`
- the demo host allows at most:
  - `2` ready workspaces
  - `2.0` total CPUs
  - `2048 MB` total memory
- these limits are enforced on create and resume, not by queueing

## Stage 11: Control Plane Plus Worker Nodes Demo

This version splits the single server into a control plane and separate worker processes.

- `stage11_server.py` runs the control plane on `127.0.0.1:8010`
- `stage11_worker.py` runs worker nodes that actually host workspace containers
- the control plane stores workspace metadata, schedules create/resume onto workers, proxies file APIs, and relays the shell WebSocket to the assigned worker

### Run It

Open terminal 1:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage11_worker.py --worker-id worker-a --port 8011
```

Open terminal 2:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage11_worker.py --worker-id worker-b --port 8012
```

Open terminal 3:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage11_server.py
```

Open terminal 4:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage11_client.py
```

### Stage 11 Notes

- the control plane is the only thing the client talks to
- worker nodes dynamically register themselves with the control plane
- the control plane polls each registered worker's `/health` endpoint and keeps an eventually consistent worker registry
- worker nodes expose private runtime APIs used only by the control plane
- placement is chosen by a simple scheduler that inspects the last known healthy workers and their remaining capacity
- suspend/resume still work, but now they may involve a remote worker API call rather than a local runtime function
- if a worker stops responding long enough, the control plane marks workspaces assigned to it as `worker_lost`, closes active client sockets for those workspaces, and later allows `/resume` to reprovision them on another healthy worker
- because all processes are still on one laptop, the workers share the same underlying `.stage11_data` storage paths even though runtime execution is split by process

## Stage 12: Fair Scheduler Demo

This version keeps the Stage 11 control-plane/worker split and improves runtime placement fairness.

- `stage12_server.py` runs the control plane on `127.0.0.1:8013`
- `stage12_worker.py` runs worker nodes that host workspace containers
- the control plane first applies per-owner active usage limits, then schedules accepted workspaces onto healthy workers by projected dominant load
- worker tie-breaks use a stable hash of `workspace_id + worker_id` instead of alphabetical worker IDs, so equal workers do not always favor `worker-a`

### Run It

Open terminal 1:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage12_worker.py --worker-id worker-a --port 8014
```

Open terminal 2:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage12_worker.py --worker-id worker-b --port 8015
```

Open terminal 3:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage12_server.py
```

Open terminal 4:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage12_client.py
```

### Stage 12 Scheduling Policy

Admission control rejects create/resume when one owner would exceed:

- `2` active workspaces
- `2.0` active CPUs
- `2048 MB` active memory

Placement then considers only registered healthy workers with enough remaining capacity. For each candidate worker, the scheduler computes:

```text
projected_worker_load = max(
  (used_cpu + requested_cpu) / max_cpu,
  (used_memory + requested_memory) / max_memory,
  (used_workspace_count + 1) / max_workspace_count
)
```

The selected worker is the candidate with the lowest projected dominant load. If a suspended workspace resumes and its previous worker is healthy with enough capacity, the scheduler prefers that worker to reduce churn. Remaining ties are broken by active workspace count and then by a stable SHA-256 hash of `workspace_id:worker_id`.

### Stage 12 Notes

- this stage separates user/client fairness from worker-side load balancing
- per-owner limits are intentionally simple fixed quotas for the demo
- worker-side fairness uses projected load, not current load, so the cost of the requested runtime is included in the placement decision
- stable tie-breaking keeps decisions deterministic without biasing equal candidates toward the lexicographically first worker ID

## Stage 13: Durable Control Plane And Runtime Reconciliation Demo

This version keeps the Stage 12 fair scheduler and adds durable control-plane state.

- `stage13_server.py` runs the control plane on `127.0.0.1:8016`
- `stage13_worker.py` runs worker nodes that host workspace containers
- workspace metadata and worker registry data are persisted in `.stage13_data/control_plane.db`
- workers expose `GET /runtimes` so the control plane can reconcile durable workspace rows with live runtime processes after a restart

### Run It

Open terminal 1:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage13_worker.py --worker-id worker-a --port 8017
```

Open terminal 2:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage13_worker.py --worker-id worker-b --port 8018
```

Open terminal 3:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage13_server.py
```

Open terminal 4:

```bash
cd /Users/xiangu/codingPractice/codespaces
python3 stage13_client.py
```

### Stage 13 Recovery Flow

On startup, the control plane:

- opens `.stage13_data/control_plane.db`
- reloads durable workspace rows into memory
- reloads known worker URLs into the worker registry
- polls known workers with `GET /health`
- queries healthy workers with `GET /runtimes`
- marks known workspaces as `ready` when their assigned runtime is still reported by a worker
- marks known active workspaces as `worker_lost` when their assigned worker is unavailable
- marks known active workspaces as `suspended` when their assigned worker is healthy but no longer reports the runtime
- logs worker-reported runtimes whose `workspace_id` does not exist in SQLite as orphaned and leaves them unmanaged

### Stage 13 Notes

- SQLite is the source of truth for logical workspaces and known workers
- workers are the source of truth for currently running runtime containers
- WebSocket attachment state is not durable; after a control-plane restart, clients reconnect through the normal client flow
- worker registration retries until the first successful registration, then stops; after a control-plane restart, the control plane recovers known worker URLs from SQLite and resumes pull-based health checks
- this stage still assumes all workers can access the same shared filesystem paths for persistent workspace storage
