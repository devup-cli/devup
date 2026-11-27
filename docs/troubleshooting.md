# Troubleshooting & FAQ

## Common Issues

### A discovered task isn't executable because the runner isn't installed
Run `devup doctor` to see which external runners (`pnpm`, `poetry`, `mvn`, `gradle`, `docker`, `make`, `just`) are missing from your `PATH`.

### Port already in use warning or error
`devup` checks `port` / `ports` (and `--port` / `PORT=` flags) before launching services.
- To see the warning without blocking startup, keep `strict_ports = false` (default).
- To skip port checks temporarily, pass `--no-port-check`.
- To abort when a port is busy, pass `--strict-ports`.

### A downstream service starts too early
Use one or more readiness gates on the upstream service in `devup.toml`:
- `ready_tcp = "127.0.0.1:5432"` (or `ready_port = 5432`) for databases/brokers.
- `ready_url = "http://127.0.0.1:8000/health"` for HTTP servers.
- `ready_pattern = "Uvicorn running"` for log-line matching.

### A service crashes on `--watch` startup and keeps restarting?
When `--watch` is enabled, `devup` intentionally keeps watching after a non-zero exit so fixing a syntax error automatically restarts the service on save. Press `Ctrl+C` to exit.

### `Ctrl+C` on Windows with nested child processes
`devup` isolates each task in a Windows Job Object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) and tracks descendant PIDs via Toolhelp32 as a fallback, ensuring reloaders (like `uvicorn --reload` or `vite`) terminate cleanly with the parent. Press `Ctrl+C` a second time to force-kill immediately.

## FAQ

**Does `devup` require any third-party Python packages?**  
No. `devup-cli` uses only the Python 3.11+ standard library.

**Why is the PyPI package named `devup-cli` while the command is `devup`?**  
The name `devup` was already registered on PyPI by an unrelated package, so we publish under `devup-cli` while keeping `devup` as the installed CLI command.

**Does `devup` work in monorepos?**  
Yes. It automatically scans `package.json` `workspaces`, Maven `<modules>`, Gradle `settings.gradle(.kts)` `include(...)` subprojects, and custom `workspaces = [...]` globs in `devup.toml`, prefixing tasks as `<workspace>:<task>`.

**How do I prevent `.env` from overriding my shell variables?**  
By default, root `.env` and workspace `env_file` entries never overwrite variables already exported in your shell.

**Is `devup doctor --json` safe to paste into a public GitHub issue?**  
Yes. `devup doctor --json` never prints `.env` values (only key names), redacts user home directories and OS usernames (`~` / `<user>`), and scrubs URL credentials (`scheme://user:pass@`), `Bearer`/JWT tokens, `KEY=VALUE` secrets, and sensitive CLI flags from commands and validation errors. Always review the output once before sharing.

**Does `devup` phone home or check for updates automatically?**  
No. Update checks are strictly opt-in (`--check-updates`, `devup doctor --online`, or `DEVUP_CHECK_UPDATES=1`), and can also be explicitly disabled with `--no-update-check` or `DEVUP_NO_UPDATE_CHECK=1`.

**Why does `scoop install ./packaging/scoop/devup.json` fail on Windows?**  
Scoop only treats a local JSON path as a file when written with Windows backslashes (`.\packaging\scoop\devup.json`) or as an `https://` URL, and requires Scoop's `main` bucket (`scoop bucket add main`) to resolve the `python` dependency.

**What if both the Python (`devup-cli`) and Node.js (`@devup-cli/devup`) packages are installed?**  
Run `devup doctor` to see which runtime is currently first on `PATH` and whether another `devup` executable is shadowed. Uninstall whichever copy you don't need (`pipx uninstall devup-cli` or `npm uninstall -g @devup-cli/devup`), or invoke a specific runtime directly with `devup-py` or `devup-node`.
