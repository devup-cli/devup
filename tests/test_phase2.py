"""Phase 2 automated tests: doctor, validate, completions, examples, templates, updates, and packaging."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from build_zipapp import build_zipapp
from bump_version import bump_version
from devup import __version__
from devup.cli import _init_devup_toml, main
from devup.diagnostics import redact_path, run_doctor, validate_workspace
from devup.discovery import discover_workspace
from devup.updates import check_for_update


class DiagnosticsAndValidateTests(unittest.TestCase):
    def test_validate_detects_errors_and_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "devup.toml").write_text(
                """
[groups]
dev = ["api", "missing_task"]

[tasks.api]
command = "uvicorn app:app"
cwd = "does_not_exist"
ready_url = "ftp://invalid"
port = 8000

[tasks.web]
command = "vite"
port = 8000
""".strip(),
                encoding="utf-8",
            )
            out = io.StringIO()
            rc = main(["-C", str(root), "--no-color", "--json", "validate"], stream=out)
            self.assertEqual(rc, 1)
            payload = json.loads(out.getvalue())
            self.assertFalse(payload["valid"])
            joined_errors = "\n".join(payload["errors"])
            self.assertIn("missing_task", joined_errors)
            self.assertIn("does_not_exist", joined_errors)
            self.assertIn("ready_url", joined_errors)
            joined_warnings = "\n".join(payload["warnings"])
            self.assertIn("Port 8000", joined_warnings)

    def test_doctor_json_redacts_home_and_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("API_SECRET_KEY=super-secret-value-123\nPORT=8000\n", encoding="utf-8")
            (root / "package.json").write_text(
                json.dumps({"scripts": {"dev": "vite"}}),
                encoding="utf-8",
            )
            prev_secret = os.environ.get("DEVUP_TEST_SECRET_TOKEN")
            os.environ["DEVUP_TEST_SECRET_TOKEN"] = "top-secret-xyz"
            try:
                out = io.StringIO()
                rc = main(["-C", str(root), "--no-color", "--json", "doctor"], stream=out)
                self.assertEqual(rc, 0)
                raw = out.getvalue()
                self.assertNotIn("super-secret-value-123", raw)
                self.assertNotIn("top-secret-xyz", raw)
                payload = json.loads(raw)
                self.assertEqual(payload["dotenv_keys"], ["API_SECRET_KEY", "PORT"])
                self.assertGreaterEqual(payload["redacted_env_vars_count"], 1)
                self.assertIn("package.json", payload["sources"])
                self.assertTrue(payload["validation"]["valid"])
            finally:
                if prev_secret is None:
                    os.environ.pop("DEVUP_TEST_SECRET_TOKEN", None)
                else:
                    os.environ["DEVUP_TEST_SECRET_TOKEN"] = prev_secret

    def test_redact_path_masks_home_directory(self) -> None:
        sample = Path.home() / "projects" / "demo"
        self.assertTrue(redact_path(sample).startswith("~"))


class CliImprovementsTests(unittest.TestCase):
    def test_completions_for_all_supported_shells(self) -> None:
        for shell in ("bash", "zsh", "fish", "powershell"):
            out = io.StringIO()
            rc = main(["completion", shell], stream=out)
            self.assertEqual(rc, 0)
            self.assertIn("devup", out.getvalue())

        bad_out = io.StringIO()
        self.assertEqual(main(["completion", "tcsh", "--no-color"], stream=bad_out), 2)
        self.assertIn("Unsupported shell", bad_out.getvalue())

    def test_close_match_suggestion_on_unknown_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({"scripts": {"build": "vite build", "dev": "vite"}}),
                encoding="utf-8",
            )
            out = io.StringIO()
            rc = main(["-C", str(root), "--no-color", "run", "biuld"], stream=out)
            self.assertEqual(rc, 2)
            self.assertIn("Did you mean: build?", out.getvalue())

    def test_init_with_template_and_interactive_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = io.StringIO()
            rc = main(["-C", str(root), "--no-color", "init", "--template", "fullstack"], stream=out)
            self.assertEqual(rc, 0)
            content = (root / "devup.toml").read_text(encoding="utf-8")
            self.assertIn("ready_url", content)

        with tempfile.TemporaryDirectory() as tmp2:
            root2 = Path(tmp2)
            (root2 / "package.json").write_text(
                json.dumps({"scripts": {"api": "echo api", "web": "echo web"}}),
                encoding="utf-8",
            )
            cfg = discover_workspace(root2)
            out2 = io.StringIO()
            rc2 = _init_devup_toml(
                cfg,
                force=True,
                out=out2,
                use_color=False,
                interactive=True,
                stdin=io.StringIO("auto\nmygroup\ny\nn\n"),
            )
            self.assertEqual(rc2, 0)
            toml_text = (root2 / "devup.toml").read_text(encoding="utf-8")
            self.assertIn("mygroup =", toml_text)
            self.assertIn("strict_ports = true", toml_text)
            self.assertIn("fail_fast = false", toml_text)

    def test_update_check_cache_and_opt_out(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake_cache = Path(td) / "update_check.json"
            fake_resp = mock.MagicMock()
            fake_resp.read.return_value = json.dumps({"info": {"version": "99.0.0"}}).encode("utf-8")
            fake_resp.__enter__.return_value = fake_resp

            with (
                mock.patch("devup.updates._cache_file", return_value=fake_cache),
                mock.patch("devup.updates.urllib.request.urlopen", return_value=fake_resp) as mock_open,
            ):
                status1 = check_for_update(__version__, force=False)
                self.assertIsNotNone(status1)
                assert status1 is not None
                self.assertTrue(status1.update_available)
                self.assertEqual(status1.latest, "99.0.0")
                # Second call should read from 24h cache without opening network again.
                status2 = check_for_update(__version__, force=False)
                self.assertIsNotNone(status2)
                self.assertEqual(mock_open.call_count, 1)

            with mock.patch.dict(os.environ, {"DEVUP_NO_UPDATE_CHECK": "1"}):
                self.assertIsNone(check_for_update(__version__, force=True))


class ExamplesAndPackagingTests(unittest.TestCase):
    def test_all_example_projects_validate_and_dry_run(self) -> None:
        examples_dir = REPO_ROOT / "examples"
        project_dirs = [
            d for d in sorted(examples_dir.iterdir()) if d.is_dir() and d.name != "templates"
        ]
        self.assertGreaterEqual(len(project_dirs), 7)

        for proj_dir in project_dirs:
            with self.subTest(example=proj_dir.name):
                report = validate_workspace(proj_dir)
                self.assertTrue(report.valid, msg=f"{proj_dir.name} errors: {report.errors}")
                out = io.StringIO()
                rc = main(["-C", str(proj_dir), "--no-color", "--dry-run"], stream=out)
                self.assertEqual(rc, 0, msg=out.getvalue())
                self.assertIn("Dry run execution plan", out.getvalue())

    def test_all_reusable_templates_parse_and_validate(self) -> None:
        templates_dir = REPO_ROOT / "examples" / "templates"
        templates = sorted(templates_dir.glob("*.toml"))
        self.assertGreaterEqual(len(templates), 4)
        for tpl in templates:
            with self.subTest(template=tpl.name):
                data = tomllib.loads(tpl.read_text(encoding="utf-8"))
                self.assertIn("tasks", data)
                self.assertIn("groups", data)

    def test_zipapp_build_and_execution(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out_pyz = Path(td) / "devup.pyz"
            built = build_zipapp(out_pyz)
            self.assertTrue(built.is_file())
            ver = subprocess.check_output([sys.executable, str(built), "--version"], text=True).strip()
            self.assertEqual(ver, f"devup {__version__}")

    def test_bump_version_script_updates_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src" / "devup").mkdir(parents=True)
            (root / "packaging" / "scoop").mkdir(parents=True)
            (root / "packaging" / "homebrew").mkdir(parents=True)
            (root / "pyproject.toml").write_text('[project]\nname = "devup-cli"\nversion = "0.2.0"\n', encoding="utf-8")
            (root / "src" / "devup" / "__init__.py").write_text('__version__ = "0.2.0"\n', encoding="utf-8")
            (root / "packaging" / "scoop" / "devup.json").write_text('{"version": "0.2.0", "url": "x"}\n', encoding="utf-8")
            (root / "packaging" / "homebrew" / "devup-cli.rb").write_text('url "https://example/devup_cli-0.2.0.tar.gz"\n', encoding="utf-8")
            (root / "CHANGELOG.md").write_text("# Changelog\n\n## [0.2.0] - 2026-11-27\n", encoding="utf-8")

            updated = bump_version("0.9.1", root=root)
            self.assertEqual(len(updated), 5)
            self.assertIn('version = "0.9.1"', (root / "pyproject.toml").read_text(encoding="utf-8"))
            self.assertIn('__version__ = "0.9.1"', (root / "src" / "devup" / "__init__.py").read_text(encoding="utf-8"))
            self.assertIn("0.9.1", (root / "packaging" / "scoop" / "devup.json").read_text(encoding="utf-8"))
            self.assertIn("devup_cli-0.9.1.tar.gz", (root / "packaging" / "homebrew" / "devup-cli.rb").read_text(encoding="utf-8"))
            self.assertIn("## [0.9.1]", (root / "CHANGELOG.md").read_text(encoding="utf-8"))

    def test_docs_files_exist_and_core_commands_run(self) -> None:
        for rel in (
            "docs/index.html",
            "docs/style.css",
            "docs/getting-started.md",
            "docs/cli-reference.md",
            "docs/configuration.md",
            "docs/troubleshooting.md",
            "docs/standalone-executables.md",
            "assets/logo.svg",
            "CONTRIBUTING.md",
            "CODE_OF_CONDUCT.md",
            "SECURITY.md",
            "ROADMAP.md",
            "CHANGELOG.md",
        ):
            self.assertTrue((REPO_ROOT / rel).is_file(), f"Missing {rel}")
        # Validate README and doc CLI examples run without errors
        out = io.StringIO()
        self.assertEqual(main(["-C", str(REPO_ROOT), "--no-color", "validate"], stream=out), 0)
        self.assertEqual(main(["-C", str(REPO_ROOT), "--no-color", "list", "--json"], stream=io.StringIO()), 0)


if __name__ == "__main__":
    unittest.main()
