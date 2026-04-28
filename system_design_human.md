This is a practice to designing a remote devbox where we can perform file operations (list files, read files, write files) and run interactive commands remotely under the concept of a "workspace".

The UX is that the client can start and connect to the server, optionally cloning a Git repo so the client can start to edit files, and the client can also execute arbitrary commands (like `git`, `make`) to build/run files. User has a persistent home directory in the cloud + some ephemeral storage for the workspace. Workspace has its lifecycle and the system should be able to clean up idle workspaces (and therefore release resources). Workspace usages should have a per-user quota and ensure a fair scheduling of runtimes.

How I would approach such a complicated system in an interview, and what tools are available to me to achieve such a design?

Tools/mechanisms: - view/edit files in the cloud --> define a few commands that sends from client to server to view/manipulate files stored in server (for example, server can expose a few HTTP endpoints where clients can call to view/edits files stored in the server) - run arbitrary commands in the cloud --> establish a WebSocket connection (e.g. `GET /workspace/{workspace_id}/ws`) to send arbitrary commands from client to server, and server starts a shell process, and execute those commands in the server, and finally sends back the shell responses back to client - limited cpu/memory resources --> think of running the shell process in a container and container runtime (like Docker) allows us to specify cpu/memory limit (via `--cpus` and `--memory` flags) - scheduling --> control plane and worker node separation. Containers, one per workspace, are scheduled to run in different work nodes, and communication is proxied through the control plane - quotas and fair scheduling --> limit the number of resource usages (total CPU/memory/workspaceCnt) per user and intelligently pick a worker from the pool to run the container for a workspace - Persistent/ephemeral storage: use a shared NFS where worker node mount a particular persistent directory into the container and it can use local directory to mount into the container for any ephemeral storage

The mental model is to start building something very simple: can we design a system where users can send some predefined filesystem API to the server and the server will view/edit those files locally so that it feels like the client is operating on a filesystem (but in the cloud).

Then, we also introduce the ability to send and run arbitrary linux commands from client to server, run the commands in a shell process in the server, and sends back the response.

Then, we encapsulate those two abilities into the concept of a `workspace` and the server starts to track those workspaces' meta data. We are going to introduce two more commands in the client `/suspend` and `/resume` to allow clients to suspend and later resume a workspace. Suspending a workspace simply means the server can close the Shell process associated with that workspace.

Then, we further separate the storage of each workspace in the server into a persistent directory + a ephemeral directory. The idea is that the ephemeral directory on the server should/can be wiped out whenever a workspace is suspended.

Then we can improve isolation and allow resource control by running the Shell process associated with each workspace in a container, and mount the two directories (one persistent + one ephemeral) into the container so that each client cannot view/edit files of another workspace.

Then we can start to separate running each containerized Shell process of each workspace into separate worker nodes and simplify the server to more of a "control plane". The idea is that the control plane now tracks the metadata of all workspaces but the actual command execution and file storage is now on a worker node. This allows us to support more workspaces and their operations. This of course requires a worker pool monitoring from the server (via a timeout based, heartbeat approach so the server knows which worker nodes are healthy and are handling which active workspaces).

Then we can also introduce a "Idle Sweeper" which monitors all workspaces (running in all work nodes) to see if their websocket connection is closed and if they have been idle for X period of time, and if so, call `/suspend` to the server on those workspaces, to ensure we don't have idle containers/workspaces running.

Then we can enforce and implement things related to per-user quotas and fair scheduling. The idea is simply two layers: 1. Can this user create/resume this workspace? 2. Which work node should I schedule/run this workspace container? For 1, we can simply assert a limit on the workspaceCnt,totalWorkspaceCPU,totalWorkspaceMemory. For 2, we can schedule this workspace onto the healthy node with the least usage.

Details can be found in `README.md` on each one of the 13 stages.

[System design diagram](system_design_pic.svg)
