"""Tests for polyglot script and service discovery."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from devup.discovery import discover_workspace, resolve_targets


class DiscoveryTests(unittest.TestCase):
    def test_package_json_lockfile_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pnpm-lock.yaml").write_text("", encoding="utf-8")
            (root / "package.json").write_text(
                json.dumps(
                    {
                        "name": "web",
                        "scripts": {
                            "dev": "vite",
                            "build": "vite build",
                            "//comment": "ignored",
                        },
                    }
                ),
                encoding="utf-8",
            )

            cfg = discover_workspace(root)
            self.assertIn("dev", cfg.tasks)
            self.assertEqual(cfg.tasks["dev"].command, "pnpm run dev")
            self.assertEqual(cfg.tasks["dev"].description, "vite")
            self.assertEqual(cfg.tasks["build"].runner, "pnpm")
            self.assertNotIn("//comment", cfg.tasks)

    def test_pyproject_poetry_poe_pdm_hatch_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                """
[project]
name = "demo"

[project.scripts]
serve = "demo.cli:main"

[tool.poe.tasks]
lint = { cmd = "ruff check .", help = "Run linter" }

[tool.pdm.scripts]
fmt = "ruff format ."

[tool.hatch.envs.test.scripts]
unit = "pytest tests"

[tool.poetry.scripts]
worker = "demo.worker:run"
""".strip(),
                encoding="utf-8",
            )

            cfg = discover_workspace(root)
            self.assertEqual(cfg.tasks["lint"].command, "poe lint")
            self.assertEqual(cfg.tasks["lint"].description, "Run linter")
            self.assertEqual(cfg.tasks["fmt"].command, "pdm run fmt")
            self.assertEqual(cfg.tasks["test:unit"].command, "hatch run test:unit")
            self.assertEqual(cfg.tasks["worker"].command, "poetry run worker")
            self.assertEqual(cfg.tasks["serve"].command, "serve")

    def test_makefile_and_justfile_and_collision_qualification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Justfile").write_text(
                """
set dotenv-load

# Run test suite
test:
    pytest

_private:
    echo hidden
""".strip(),
                encoding="utf-8",
            )
            (root / "Makefile").write_text(
                """
.PHONY: test migrate

# Run make tests
test:
\techo make-test

migrate: ## Apply DB migrations
\talembic upgrade head
""".strip(),
                encoding="utf-8",
            )

            cfg = discover_workspace(root)
            self.assertEqual(cfg.tasks["test"].command, "just test")
            self.assertEqual(cfg.tasks["test"].description, "Run test suite")
            self.assertIn("make:test", cfg.tasks)
            self.assertEqual(cfg.tasks["make:test"].command, "make test")
            self.assertEqual(cfg.tasks["migrate"].description, "Apply DB migrations")
            self.assertNotIn("_private", cfg.tasks)
            self.assertNotIn(".PHONY", cfg.tasks)

    def test_docker_compose_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-compose.yml").write_text(
                """
services:
  postgres:
    image: postgres:16
    ports:
      - "5432:5432"
  redis:
    image: redis:7

volumes:
  pgdata:
""".strip(),
                encoding="utf-8",
            )

            cfg = discover_workspace(root)
            self.assertIn("compose:up", cfg.tasks)
            self.assertIn("compose:down", cfg.tasks)
            self.assertIn("compose:postgres", cfg.tasks)
            self.assertIn("compose:redis", cfg.tasks)
            self.assertNotIn("compose:pgdata", cfg.tasks)

    def test_devup_toml_overrides_and_workspace_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            apps_api = root / "apps" / "api"
            apps_web = root / "apps" / "web"
            apps_api.mkdir(parents=True)
            apps_web.mkdir(parents=True)

            (apps_api / "Makefile").write_text("dev:\n\tuvicorn app:app\n", encoding="utf-8")
            (apps_web / "package.json").write_text(
                json.dumps({"scripts": {"dev": "vite"}}),
                encoding="utf-8",
            )
            (root / "devup.toml").write_text(
                """
default = ["stack"]
workspaces = ["apps/*"]

[groups]
stack = ["api:dev", "web:dev"]
""".strip(),
                encoding="utf-8",
            )

            cfg = discover_workspace(root)
            self.assertIn("api:dev", cfg.tasks)
            self.assertIn("web:dev", cfg.tasks)
            self.assertEqual(resolve_targets(cfg, ["stack"]), ["api:dev", "web:dev"])


if __name__ == "__main__":
    unittest.main()
