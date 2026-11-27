# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-11-27

### Added
- Native Maven (`pom.xml`, `mvnw` / `mvnw.cmd`, Spring Boot / Quarkus / Exec plugins, and `<modules>`) and Gradle (`build.gradle`, `build.gradle.kts`, `gradlew` / `gradlew.bat`, `settings.gradle(.kts)` subprojects, and custom tasks) discovery.
- HTTP (`ready_url`) and TCP (`ready_tcp` / `ready_port`) readiness probes with configurable `ready_timeout` for dependency startup ordering.
- Windows Job Object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) and Toolhelp32 descendant-tracking process-tree cleanup, plus full POSIX process-group (`os.killpg`) sweeping.
- Built-in zero-dependency `.env` loading (`auto_env`, `env_file`, `--env-file`, `--no-dotenv`).
- Pre-flight port availability checks (`port`, `ports`, `--strict-ports`, `--no-port-check`).
- Polling file watcher (`-w` / `--watch`, `watch` patterns) and interactive arrow-key TUI picker (`devup pick`).
- Regression tests covering streaming HTTP responses, Windows fallback process cleanup, `SIGINT` shutdown, Maven/Gradle discovery, and cross-platform behavior.

### Changed
- HTTP readiness probes now check response status headers without reading the full response body, preventing hangs on slow or streaming endpoints.
- Updated `README.md` introduction and configuration docs to cover Maven, Gradle, readiness probes, `.env` loading, port checks, and watch mode.

## [0.1.0] - 2026-11-27

### Added
- Initial release of `devup`: polyglot script discovery (`devup.toml`, `package.json`, `pyproject.toml`, `Justfile`, `Makefile`, `docker-compose.yml`), parallel colored prefixed log runner, `ready_pattern` dependency ordering, `--dry-run`, `devup list --json`, and `devup init`.
