# CLI Command Reference

```text
devup [OPTIONS] [SUBCOMMAND | TARGETS...]
```

## Subcommands

### `devup` (no arguments)
- If `devup.toml` defines `default = [...]`, runs the default target(s).
- Otherwise, opens the interactive picker in a TTY, or prints `devup list` when non-interactive.

### `devup list` (alias: `ls`)
Lists all discovered groups and tasks in the workspace.
- `--json` — Emit machine-readable JSON (`root`, `default`, `groups`, `tasks`, `warnings`).

### `devup run <target> [<target> ...]`
Runs one or more tasks or groups concurrently.
- Shorthand: `devup <target1> <target2>` works directly whenever `<target1>` is not a built-in subcommand name.

### `devup pick`
Opens an interactive arrow-key multi-select menu (`Up`/`Down`, `Space` to toggle, `Enter` to run, `q`/`Esc` to cancel) when attached to a TTY, falling back to numbered input in non-TTY streams.

### `devup init`
Creates a starter `devup.toml` in the project root.
- `--template <auto|fullstack|python-node|java-microservices|docker-compose>` — Choose a preset template.
- `--interactive` — Prompt for group name, default targets, `fail_fast`, and `strict_ports`.
- `--yes` — Skip interactive prompts and write immediately.
- `--force` — Overwrite an existing `devup.toml`.

### `devup validate`
Validates `devup.toml`, group expansions, task `depends_on` references, dependency cycles, working directories, `env_file` paths, readiness URLs/TCP specs, and duplicate port declarations.
- `--json` — Output validation status, errors, and warnings as JSON.
- Exit code `0` when valid, `1` when errors are found.

### `devup doctor`
Inspects the local environment: Python/OS details, `devup` version, detected external runners (`git`, `node`, `npm`, `pnpm`, `yarn`, `bun`, `poetry`, `pdm`, `hatch`, `poe`, `make`, `just`, `docker`, `java`, `mvn`, `gradle`), discovered task count, and config validation status. Runs **offline by default**.
- `--json` — Output a shareable diagnostic JSON report with sensitive data automatically redacted (user home directories, OS usernames, URL userinfo credentials `scheme://user:pass@`, `KEY=VALUE` / `Bearer` / `Basic` / JWT tokens, CLI secret flags, and sensitive environment variable values).
- `--online` — Opt in to checking PyPI for whether a newer `devup-cli` version is available.

### `devup completion <shell>`
Outputs a shell autocompletion script to stdout. Supported shells: `bash`, `zsh`, `fish`, `powershell`.

## Global Options

| Flag | Description |
| :--- | :--- |
| `-V`, `--version` | Show `devup` version and exit. |
| `-C`, `--cwd <dir>` | Project root directory to scan (defaults to current working directory). |
| `--no-workspaces` | Disable monorepo/multi-module subdirectory scanning. |
| `-t`, `--timestamps` | Prepend `HH:MM:SS.mmm` timestamps to log lines. |
| `--no-prefix` | Disable `[service]` prefixes on log lines. |
| `--keep-going` | Keep sibling services running if one exits with a non-zero code. |
| `--dry-run` | Print the resolved dependency order and commands without starting processes. |
| `-w`, `--watch` | Watch task working directories and auto-restart tasks when files change. |
| `--env-file <path>` | Load an extra `.env` file (repeatable). |
| `--no-dotenv` | Disable automatic loading of `.env` in the project root. |
| `--no-port-check` | Skip pre-flight port availability checks. |
| `--strict-ports` | Abort startup if any declared or inferred port is already occupied. |
| `--check-updates` | Check PyPI once (24h cached) for a newer release and notify if available. |
| `--no-update-check` | Disable any update check (also respects `DEVUP_NO_UPDATE_CHECK=1`). |
| `--color` / `--no-color` | Force or disable ANSI color output (also respects `NO_COLOR` / `FORCE_COLOR`). |
| `--json` | Output JSON (for `list`, `validate`, `doctor`). |
| `--force` | Overwrite existing `devup.toml` (for `init`). |
