# Roadmap

## Phase 1 (v0.2.0) — Reliability & Core Polyglot Support [Completed]
- Native Maven & Gradle discovery (`pom.xml`, `build.gradle(.kts)`)
- HTTP (`ready_url`) and TCP (`ready_tcp` / `ready_port`) readiness probes
- Windows Job Objects + Toolhelp32 fallback and POSIX `os.killpg` cleanup
- `.env` loading, port availability pre-checks, `--watch`, and arrow-key `devup pick`
- Cross-platform CI across Windows, macOS, and Linux (Python 3.11–3.14)

## Phase 2 (v0.3.0) — Onboarding, Diagnostics & Community [Current]
- Documentation website on GitHub Pages, logo, 5-minute tutorial, CLI & config references
- Ready-to-run `examples/` and interactive `devup init --template`
- `devup doctor` (with redacted `--json`), `devup validate`, shell completions (`bash`, `zsh`, `fish`, `powershell`), and update checks
- Homebrew, Scoop, portable `devup.pyz` zipapp, and automated release pipeline

## Phase 3 — Next Ideas
- Service restart-on-failure policies (`restart = "on-failure"`, backoff)
- Log filtering (`--only`, `--exclude`, `--save-logs`)
- Support for Cargo (`Cargo.toml`), Go (`go.mod` / Taskfile), and `.tool-versions` / `mise.toml`
- Split-pane TUI viewer mode as an opt-in interface
