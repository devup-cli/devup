# Manifest Scanners & Discovery Reference

Running `devup list` (or any target command) executes deterministic manifest scanners in the workspace root and, unless `--no-workspaces` is passed, across discovered monorepo subprojects.

## Discovery Order & Overrides

1. **Root manifest scan**: Every built-in scanner inspects the workspace root.
2. **Collision qualification**:
   - The first scanner to register an unqualified task name claims that name cleanly (e.g., `dev`).
   - Subsequent scanners defining the same task name register a source-qualified alias (`<scanner>:<name>`, such as `cargo:test`, `mise:lint`, or `compose:up`) instead of silently discarding either definition.
3. **Subproject / Monorepo scan** (unless `--no-workspaces`):
   - Subprojects discovered via `package.json` `workspaces`, `Cargo.toml` `[workspace].members`, `go.work` `use` directives, Maven `<modules>`, Gradle `settings.gradle(.kts)` `include(...)`, and `devup.toml` `workspaces = [...]` are scanned and registered as `<subproject>:<task>` (plus unqualified group or workspace entries).
4. **Explicit `devup.toml` overrides**:
   - `devup.toml` always wins over auto-discovered tasks of the same name and adds `[groups]`, `default`, `depends_on`, `[env]`, readiness checks, ports, and watch globs.

## Supported Scanners

| Scanner | Files Inspected | Commands Emitted & Detection Rules | Fallbacks & Notes |
| :--- | :--- | :--- | :--- |
| **Node / JS** | `package.json` | `<pm> run <script>` where `<pm>` is chosen from lockfiles (`pnpm-lock.yaml` -> `pnpm`, `yarn.lock` -> `yarn`, `bun.lockb` / `bun.lock` -> `bun`, otherwise `npm`) or `packageManager` field. Skips empty or internal `pre*`/`post*` lifecycle hooks when appropriate. | Invalid JSON logs a warning naming the repository root and exact `package.json` path. Monorepo `workspaces` globs (`packages/*` or `{ "packages": [...] }`) trigger subproject scanning. |
| **Python (`pyproject`)** | `pyproject.toml` | Scans `[tool.poe.tasks]` (`poe <task>`), `[tool.pdm.scripts]` (`pdm run <task>`), `[tool.hatch.envs.default.scripts]` (`hatch run <task>`), `[tool.poetry.scripts]` (`poetry run <task>`), and `[project.scripts]` (`python -m ...` / executable). | Malformed TOML emits a warning with repository root + `pyproject.toml` path instead of aborting unrelated scanners. |
| **Rust (`cargo`)** | `Cargo.toml` | Registers standard targets (`build` -> `cargo build`, `run` -> `cargo run` when binary/main exists, `test` -> `cargo test`, `check` -> `cargo check`, `clippy` -> `cargo clippy`, `fmt` -> `cargo fmt --check`) plus `[package.metadata.scripts]` and `[[bin]]` / `[[example]]` targets. | Respects `[workspace].members` globs (`crates/*`) for subproject discovery and skips `[workspace]`-only virtual manifests for `cargo run` unless a `[[bin]]` target exists. |
| **Go (`go`)** | `go.mod`, `go.work` | Registers `build` (`go build ./...`), `test` (`go test ./...`), `vet` (`go vet ./...`), plus `run` (`go run .`) when a `main.go` or `cmd/` entrypoint exists, and per-command `run:<cmd>` tasks for each `cmd/<name>` directory. | Reads `go.work` `use (...)` and single-line `use ./path` blocks to discover multi-module Go workspaces cleanly. |
| **Taskfile** | `Taskfile.yml`, `Taskfile.yaml`, `taskfile.yml`, `taskfile.yaml` | Parses top-level `tasks:` entries and extracts `desc` / `summary` descriptions, emitting `task <name>`. | Excludes tasks marked `internal: true`. Zero external YAML library required (handles nested blocks, comments, and quoted task names cleanly). |
| **mise** | `mise.toml`, `.mise.toml`, `.config/mise.toml` | Parses `[tasks.<name>]` tables (both `[tasks.name]` inline strings and full tables with `description` and `run`), emitting `mise run <name>`. | Skips tasks marked `hide = true`. Emits contextual warnings on malformed TOML. |
| **Maven (`pom`)** | `pom.xml` | Detects Maven wrapper (`./mvnw` / `mvnw.cmd` when present, falling back to `mvn`), registers standard goals (`compile`, `test`, `package`) and framework goals (`spring-boot:run`, `quarkus:dev`, `exec:java`) when matching plugins appear in `pom.xml`. | Reads `<modules><module>...</module></modules>` for multi-module scanning. |
| **Gradle** | `build.gradle`, `build.gradle.kts`, `settings.gradle(.kts)` | Detects `./gradlew` / `gradlew.bat` wrapper, falling back to `gradle`. Extracts custom `tasks.register(...)` / `task <name>`, plus `run` / `bootRun` / `test` / `build` / `check` based on applied plugins. | Reads `include(...)` subprojects from `settings.gradle` and `settings.gradle.kts`. |
| **Justfile** | `Justfile`, `justfile`, `.justfile` | Parses top-level recipes (ignoring `_private` helpers and variable assignments) and captures `#` doc comments, emitting `just <recipe>`. | Runs cleanly even when `just` is not yet installed (discovery is static file parsing). |
| **Makefile** | `Makefile`, `GNUmakefile`, `makefile` | Parses targets (ignoring `.PHONY` and private targets starting with `.`) and captures inline `##` or preceding `#` doc comments, emitting `make <target>`. | Preserves target order cleanly. |
| **Docker Compose** | `compose.yaml`, `compose.yml`, `docker-compose.yaml`, `docker-compose.yml` | Registers `compose:up` (`docker compose up`), `compose:down` (`docker compose down`), and per-service `compose:<service>` (`docker compose up <service>`). | Works offline by parsing the `services:` YAML block directly. |
| **Explicit Config** | `devup.toml` | Defines authoritative `[tasks.*]`, `[groups.*]`, `default`, `workspaces`, `[env]`, `env_file`, `depends_on`, `port`/`ports`, `ready_*`, and `watch`. | Emits repository root + `devup.toml` path + parse/validation error details on syntax or reference errors. |
