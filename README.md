# devup

**Polyglot project script discovery and multi-service parallel runner.**

`devup` scans your repository for runnable scripts and services across `devup.toml`, `package.json`, `pyproject.toml`, `Justfile`, `Makefile`, and `docker-compose.yml`, then runs one or many of them concurrently with color-coded prefixed logs and clean process-tree shutdown on `Ctrl+C`.

Zero third-party runtime dependencies — pure Python 3.11+ standard library.

## Features

- **Polyglot auto-discovery:**
  - `package.json` (automatically detects `pnpm`, `yarn`, `bun`, or `npm` from lockfiles, plus `workspaces` monorepos)
  - `pyproject.toml` (supports Poe the Poet, PDM, Hatch, Poetry scripts, and `[project.scripts]`)
  - `Justfile` / `justfile` (recipes + doc comments)
  - `Makefile` / `GNUmakefile` (targets + `#` / `##` doc comments)
  - `compose.yaml` / `docker-compose.yml` (`compose:up`, `compose:down`, and per-service `compose:<service>`)
  - `devup.toml` (custom tasks, service groups, defaults, working directories, env vars, `depends_on`, and `ready_pattern`)
- **Parallel runner with readable logs:**
  - Distinct ANSI colors and aligned `[service]` prefixes per process
  - Optional `--timestamps` and `--no-prefix` modes
- **Clean shutdown & fail-fast:**
  - Stops all sibling services if one crashes (override with `--keep-going`)
  - Kills full process trees on `Ctrl+C` (process groups on POSIX; `CTRL_BREAK_EVENT` + `taskkill /T` fallback on Windows) so child reloaders don't leak ports
- **Built-in `.env` loading:**
  - Automatically loads `.env` from the project root (without overriding variables already exported in your shell)
  - Supports `env_file` at both workspace and per-task levels in `devup.toml`, plus `--env-file` and `--no-dotenv` CLI flags
- **Port availability pre-check:**
  - Checks declared (`port` / `ports`) or inferred (`--port`, `PORT=`) ports before launch, warns with the owning PID/process if a port is occupied, or aborts with `--strict-ports`
- **Watch & auto-restart (`--watch` / `-w`):**
  - Polls watched files (zero external dependencies) and restarts services cleanly when source files change
- **Interactive arrow-key picker (`devup pick`):**
  - Multi-select tasks/groups with `Up`/`Down`, `Space`, and `Enter` in a TTY (with numbered input fallback)
- **Dependency & readiness ordering:**
  - Start `web` only after `api` logs a `ready_pattern` regex or after `build` finishes cleanly

## Installation

```bash
# With pipx or uv (recommended for global CLI usage)
pipx install git+https://github.com/devup-cli/devup.git
# or
uv tool install git+https://github.com/devup-cli/devup.git

# Or install locally from a cloned checkout
pip install -e .
```

## Quick Start

```bash
# List all discovered scripts and groups in the current repo
devup list

# Run multiple discovered tasks side by side
devup run api web worker
# Shorthand (when names don't collide with subcommands):
devup api web worker

# Run with auto-restart on file changes and extra env file
devup -w --env-file .env.local api web

# Abort startup if any declared port is already occupied
devup --strict-ports api web

# Interactive picker (also opens automatically when running `devup` in a TTY with no default)
devup pick

# Preview execution plan without spawning processes
devup --dry-run api web

# Generate a starter devup.toml
devup init
```

## Configuration (`devup.toml`)

`devup` works out of the box without configuration, and you can add `devup.toml` to define service groups, `.env` files, ports, file watching, startup order, or readiness checks:

```toml
default = ["dev"]
fail_fast = true
strict_ports = false
auto_env = true
env_file = [".env", ".env.local"]
shutdown_timeout = 5.0

[env]
LOG_LEVEL = "info"

[groups]
dev = ["db", "api", "web"]

[tasks.db]
command = "docker compose up postgres"
port = 5432
ready_pattern = "database system is ready to accept connections"

[tasks.api]
command = "uvicorn app.main:app --reload --port 8000"
cwd = "backend"
port = 8000
env_file = ".env.api"
depends_on = ["db"]
ready_pattern = "Uvicorn running on"
watch = ["app/**/*.py"]
env = { PORT = "8000" }

[tasks.web]
command = "npm run dev"
cwd = "frontend"
port = 5173
depends_on = ["api"]
```

## Running Tests

```bash
python -m unittest discover -s tests -v
```
