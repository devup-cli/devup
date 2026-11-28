# `devup.toml` Configuration Reference

`devup` works without any config file. Adding `devup.toml` at your project root lets you define service groups, defaults, `.env` files, startup dependencies, readiness checks, port guards, and file watch rules.

## Top-Level Properties

| Key | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `default` | `string \| string[]` | `[]` | Targets or groups run when invoking `devup` with no arguments. |
| `fail_fast` | `bool` | `true` | Stop remaining services if any task exits non-zero. |
| `shutdown_timeout` | `float` | `5.0` | Seconds to wait after `SIGINT`/`SIGTERM`/`CTRL_BREAK_EVENT` before force-killing. |
| `auto_env` | `bool` | `true` | Automatically load `.env` from the project root if present. |
| `env_file` | `string \| string[]` | `[]` | Workspace-wide `.env` file(s) loaded relative to project root. |
| `strict_ports` | `bool` | `false` | Abort startup if any task port is already in use or duplicated. |
| `workspaces` | `string[]` | `[]` | Glob patterns for subproject directories (e.g. `["apps/*", "services/*"]`). |

## `[env]` Table

Key-value string pairs injected into every task's environment (overrides `.env` values, overridden by per-task `[tasks.<name>.env]`):

```toml
[env]
LOG_LEVEL = "debug"
PYTHONUNBUFFERED = "1"
```

## `[groups]` Table

Named collections of tasks (or nested groups) that launch concurrently:

```toml
[groups]
dev = ["db", "api", "web"]
checks = ["lint", "test"]
```

## `[tasks.<name>]` Table

Shorthand string form is supported: `lint = "ruff check ."`. Full table properties:

| Key | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `command` | `string` *(required)* | — | Shell command executed for this task. |
| `description` | `string` | `""` | Human-readable summary shown in `devup list`. |
| `cwd` | `string` | `"."` | Working directory relative to project root. |
| `env` | `table<string, string>` | `{}` | Task-specific environment variables. |
| `env_file` | `string \| string[]` | `[]` | Task-specific `.env` file(s) resolved relative to `cwd`. |
| `depends_on` | `string[]` | `[]` | Upstream tasks that must finish cleanly (or pass readiness checks) before this task starts. |
| `ready_pattern` | `string` | `null` | Regex matched against stdout/stderr to signal readiness. |
| `ready_url` | `string` | `null` | HTTP/HTTPS URL polled until a `2xx` or `3xx` status is returned. |
| `ready_tcp` | `string \| int` | `null` | TCP `host:port` (or port on `127.0.0.1`) polled until connection succeeds. |
| `ready_port` | `int` | `null` | Alias for `ready_tcp = "127.0.0.1:<port>"`. |
| `ready_timeout` | `float` | `30.0` | Maximum seconds to wait for HTTP/TCP readiness probes before failing. |
| `port` | `int` | `null` | Single expected listening TCP port checked before startup. |
| `ports` | `int[]` | `[]` | Multiple expected listening TCP ports checked before startup. |
| `watch` | `bool \| string \| string[]` | `false` | Enable auto-restart on file changes matching glob pattern(s) under `cwd`. |

## Environment Precedence

From lowest to highest priority:
1. Project root `.env` (does **not** override variables already exported in your shell)
2. Workspace `env_file` and CLI `--env-file` (does **not** override shell exports)
3. Shell `os.environ`
4. Top-level `[env]` in `devup.toml`
5. Task-level `env_file` in `[tasks.<name>]`
6. Task-level `env` in `[tasks.<name>]`

See also: [Manifest Scanners & Discovery](scanners.md) and [Architecture Overview](architecture.md).
