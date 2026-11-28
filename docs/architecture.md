# Architecture Overview

`devup` is implemented twice with identical behavior and zero third-party runtime dependencies:

- **Python implementation (`devup-cli`)** under `src/devup/` (Python 3.11+ standard library)
- **Node.js implementation (`@devup-cli/devup`)** under `packages/npm/src/` (Node.js 20+ built-in modules)

Both implementations share the same five-stage pipeline:

```text
  CLI Parsing (cli)
        |
        v
  Workspace & Manifest Discovery (discovery + toml + xml)
        |
        v
  Pre-flight Validation & Environment Loading (dotenv + ports)
        |
        v
  Dependency DAG Scheduling & Parallel Runner (runner + readiness + watcher)
        |
        v
  Cross-Platform Process-Tree Shutdown (proc_tree)
```

## Module Responsibilities

| Stage | Python (`src/devup/`) | Node.js (`packages/npm/src/`) | Responsibility |
| :--- | :--- | :--- | :--- |
| Entry & CLI | `cli.py`, `__main__.py` | `cli.mjs`, `bin/devup.js` | Argument parsing, flag dispatch, exit codes (`0`/`1`/`2`), target fuzzy suggestions, and subcommands (`list`, `run`, `pick`, `init`, `validate`, `doctor`, `completion`). |
| Configuration & Data Models | `models.py`, standard `tomllib`, `_xml` | `models.mjs`, `toml.mjs`, `xml.mjs` | Datastructures (`Task`, `WorkspaceConfig`, `TaskResult`) plus zero-dependency TOML and XML parsers for manifests when running on Node or older environments. |
| Polyglot Discovery | `discovery.py` | `discovery.mjs` | Scans workspace root and monorepo subprojects (`package.json`, `pyproject.toml`, `pom.xml`, Gradle, `Cargo.toml`, `go.mod`/`go.work`, `Taskfile.yml`, `mise.toml`, `Justfile`, `Makefile`, Docker Compose, and `devup.toml`), resolves collisions via `<prefix>:<task>` qualification, and records warnings/debug traces with exact repository and manifest paths. |
| Env & Port Guards | `dotenv.py`, `ports.py` | `dotenv.mjs`, `ports.mjs` | Loads `.env` hierarchy without clobbering exported shell variables, infers listening ports (`port`, `ports`, `--port`, `PORT=`), detects busy ports and owning PIDs, and supports `--strict-ports`. |
| Runner & Readiness | `runner.py`, `readiness.py`, `watcher.py`, `colors.py` | `runner.mjs`, `readiness.mjs`, `watcher.mjs`, `colors.mjs` | Topologically orders `depends_on` tasks, launches siblings concurrently, polls `ready_url` (HTTP `2xx`/`3xx`), `ready_tcp`/`ready_port`, and stdout/stderr `ready_pattern` gates, filters/buffers logs (`--only`, `--exclude`, `--buffer`, `--save-logs`), and restarts on file changes (`--watch`). |
| Process-Tree Shutdown | `runner.py` (Win32 ctypes / POSIX `os.killpg`) | `proc_tree.mjs` | Isolates each task in its own process group (`os.setsid` / POSIX `killpg`) or Windows Job Object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) + Toolhelp32/`taskkill /T` fallback so reloaders (`uvicorn --reload`, `vite`, Maven/Gradle wrappers) never orphan child processes on `Ctrl+C` or `fail_fast`. |
| Diagnostics & Updates | `diagnostics.py`, `updates.py`, `completions.py`, `picker.py` | `diagnostics.mjs`, `updates.mjs`, `completions.mjs`, `picker.mjs` | Redacted `devup doctor --json`, conflicting binary detection across `PATH`, opt-in release checks, interactive TTY/numbered picker, and shell completions. |

## Concurrency & Failure Handling

1. **Topological Scheduling (`runner`)**: Tasks with `depends_on` block only downstream dependents. Independent tasks start immediately in parallel threads/async workers.
2. **Readiness Propagation**: A downstream task unblocks as soon as its upstream dependency either exits `0` cleanly (one-shot build/migration task) or passes all configured readiness gates (`ready_pattern`, `ready_tcp`/`ready_port`, `ready_url`).
3. **Failure & Cascade Cancellation**:
   - If an upstream dependency fails, exits before becoming ready, or times out waiting on readiness, every transitive dependent is marked `skipped` with an explanatory error instead of hanging.
   - Under `fail_fast = true` (default), the first non-zero exit triggers graceful process-tree termination of remaining services, followed by a forced kill after `shutdown_timeout` seconds.
   - If spawning fails (e.g., `ENOENT` / `FileNotFoundError`), `devup` prints the missing binary name, full command, working directory, and suggests `devup doctor`.
4. **Log Attribution & Buffering**:
   - Line mode (`--buffer line`, default) streams prefixed `[service]` lines as soon as newlines arrive.
   - Group mode (`--buffer group`) collects each service's output internally and prints contiguous blocks on completion or flush (ideal for CI and `--only`/`--exclude` troubleshooting), while `--save-logs` always records full unfiltered logs for post-mortem debugging.
