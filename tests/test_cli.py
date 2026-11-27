"""Tests for the devup CLI interface."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from devup.cli import main


class CliTests(unittest.TestCase):
    def test_list_json_and_init_and_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({"scripts": {"dev": "vite", "test": "vitest"}}),
                encoding="utf-8",
            )

            list_out = io.StringIO()
            rc = main(["-C", str(root), "--no-color", "--json", "list"], stream=list_out)
            self.assertEqual(rc, 0)
            payload = json.loads(list_out.getvalue())
            self.assertIn("dev", payload["tasks"])
            self.assertIn("test", payload["tasks"])

            init_out = io.StringIO()
            rc = main(["-C", str(root), "--no-color", "init"], stream=init_out)
            self.assertEqual(rc, 0)
            self.assertTrue((root / "devup.toml").is_file())

            dry_out = io.StringIO()
            rc = main(["-C", str(root), "--no-color", "--dry-run", "dev", "test"], stream=dry_out)
            self.assertEqual(rc, 0)
            self.assertIn("Dry run execution plan", dry_out.getvalue())
            self.assertIn("npm run dev", dry_out.getvalue())

    def test_unknown_task_returns_exit_code_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            rc = main(["-C", tmp, "--no-color", "run", "nonexistent"], stream=out)
            self.assertEqual(rc, 2)
            self.assertIn("Unknown task or group", out.getvalue())

    def test_shorthand_runs_real_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cmd_one = json.dumps(f'"{sys.executable}" -c "print(101)"')
            cmd_two = json.dumps(f'"{sys.executable}" -c "print(202)"')
            (root / "devup.toml").write_text(
                f"""
[tasks.one]
command = {cmd_one}

[tasks.two]
command = {cmd_two}
""".strip(),
                encoding="utf-8",
            )
            out = io.StringIO()
            rc = main(["-C", str(root), "one", "two", "--no-color"], stream=out)
            self.assertEqual(rc, 0)
            text = out.getvalue()
            self.assertIn("[one] 101", text)
            self.assertIn("[two] 202", text)


if __name__ == "__main__":
    unittest.main()
