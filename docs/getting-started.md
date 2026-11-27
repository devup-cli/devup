# Five-Minute Getting-Started Tutorial

Get `devup` running on any single-repo or polyglot codebase in under five minutes.

## 1. Run Without Installing (30 seconds)

If you have `uv` installed, inspect any project right away:

```bash
cd /path/to/your-project
uvx --from devup-cli devup list
```

Or install globally so `devup` is always on your `PATH`:

```bash
pipx install devup-cli
# or: uv tool install devup-cli
# or: pip install devup-cli
```

## 2. Discover Existing Scripts (1 minute)

Run `devup list` inside any repository:

```bash
devup list
```

`devup` scans for scripts and targets across:
- `package.json` (`npm`, `pnpm`, `yarn`, or `bun` detected from lockfiles)
- `pyproject.toml` (Poe, PDM, Hatch, Poetry, `[project.scripts]`)
- `pom.xml` (`mvnw`/`mvn` goals, Spring Boot/Quarkus/Exec plugins, `<modules>`)
- `build.gradle` / `build.gradle.kts` (`gradlew`/`gradle`, `bootRun`, `run`, subprojects)
- `Justfile`, `Makefile`, and `docker-compose.yml` / `compose.yaml`

Preview what will run without spawning anything:

```bash
devup --dry-run api web
```

## 3. Run Multiple Services Side by Side (1 minute)

Start any combination of discovered tasks in parallel with colored `[service]` log prefixes:

```bash
devup api web
```

Press `Ctrl+C` once to gracefully stop the entire process tree (including reloaders and child workers), or twice to force-kill immediately.

Or pick tasks interactively with arrow keys:

```bash
devup pick
```

## 4. Add Startup Ordering & `.env` Support with `devup.toml` (2 minutes)

Generate a starter configuration interactively:

```bash
devup init
```

Then customize `devup.toml` for your stack—for example, wait for Postgres TCP + API HTTP health before starting the frontend:

```toml
default = ["dev"]
env_file = [".env", ".env.local"]

[groups]
dev = ["db", "api", "web"]

[tasks.db]
command = "docker compose up postgres"
port = 5432
ready_tcp = "127.0.0.1:5432"

[tasks.api]
command = "uvicorn app.main:app --reload --port 8000"
cwd = "backend"
port = 8000
depends_on = ["db"]
ready_url = "http://127.0.0.1:8000/health"
watch = ["app/**/*.py"]

[tasks.web]
command = "npm run dev"
cwd = "frontend"
port = 5173
depends_on = ["api"]
```

Validate your config and environment anytime:

```bash
devup validate
devup doctor
```
