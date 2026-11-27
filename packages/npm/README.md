# @devup-cli/devup

Run all the scripts and services in your repository from a single terminal, with zero npm dependencies.

`devup` scans manifests already in your project (`devup.toml`, `package.json`, `pyproject.toml`, `pom.xml`, `build.gradle(.kts)`, `Justfile`, `Makefile`, and `docker-compose.yml`), runs selected tasks side by side with colored `[service]` log prefixes, waits for HTTP/TCP/regex readiness probes, loads `.env` files, checks for busy ports, and stops child process trees cleanly on `Ctrl+C`.

## Install or Run Directly

```bash
# Run without a global install
npx @devup-cli/devup list

# Or install globally
npm install -g @devup-cli/devup
devup --help
```

Requires Node.js 20 or newer. Zero runtime dependencies.

## Quick Start

```bash
# Discover scripts and groups in the current repo
devup list

# Run services concurrently
devup api web worker

# Interactive arrow-key task picker
devup pick

# Generate a starter devup.toml
devup init

# Check local tools and validate config
devup validate
devup doctor
```

Full documentation, `devup.toml` reference, and examples live at [devup-cli.github.io/devup](https://devup-cli.github.io/devup/) and [github.com/devup-cli/devup](https://github.com/devup-cli/devup).
