# devup Examples & Templates

Every example below includes a working `devup.toml` and realistic manifest files so you can test `devup` immediately:

```bash
devup -C examples/fastapi-react list
devup -C examples/fastapi-react validate
devup -C examples/fastapi-react --dry-run dev
```

## Included Examples

- [`spring-boot-react/`](spring-boot-react/README.md) — Maven Spring Boot API + Vite/React frontend with HTTP readiness ordering.
- [`spring-boot-kafka-postgres/`](spring-boot-kafka-postgres/README.md) — Docker Compose PostgreSQL & Kafka + Spring Boot service with TCP & HTTP readiness gates.
- [`fastapi-react/`](fastapi-react/README.md) — FastAPI (`pyproject.toml`) backend + React (`package.json`) frontend with `.env` and `--watch`.
- [`node-monorepo/`](node-monorepo/README.md) — `package.json` workspaces monorepo (`apps/api`, `apps/web`).
- [`maven-multimodule/`](maven-multimodule/README.md) — Maven `<modules>` (`common`, `api`) multi-module workspace.
- [`gradle-multimodule/`](gradle-multimodule/README.md) — Gradle `settings.gradle.kts` (`:api`, `:worker`) multi-project workspace.
- [`docker-compose-multiservice/`](docker-compose-multiservice/README.md) — Multi-service `docker-compose.yml` orchestration with health readiness.
- [`polyglot-microservices/`](polyglot-microservices/README.md) — End-to-end Python + Node.js + Cargo monorepo microservices stack with `.env`, HTTP readiness ordering, and working directories.
- [`templates/`](templates/) — Reusable `devup.toml` starter templates (also usable via `devup init --template <name>`).
