# Polyglot Microservices Example (`examples/polyglot-microservices`)

End-to-end multi-service monorepo demonstrating Python, Node.js, Cargo/Rust workspace metadata, shared `.env` hierarchy, HTTP readiness probes, and dependency ordering (`auth` + `worker` -> `gateway`).

## Services

- **`auth`** (`services/auth/server.py`, `pyproject.toml`): Python stdlib HTTP server on port `18081` exposing `/health`.
- **`worker`** (`services/worker/worker.mjs`, `Cargo.toml`): Background Node.js worker logging inherited environment configuration.
- **`gateway`** (`services/gateway/server.mjs`, `package.json`, `.env.gateway`): Node.js HTTP server on port `18082` waiting for `auth` (`ready_url`) and `worker` before serving `/status`.

## Try It

```bash
devup -C examples/polyglot-microservices list
devup -C examples/polyglot-microservices validate
devup -C examples/polyglot-microservices --dry-run
devup -C examples/polyglot-microservices --timestamps --only auth,gateway dev
```
