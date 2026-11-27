"""Tests for dotenv loading, port pre-checks, file watching, and interactive picking."""

from __future__ import annotations

import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from devup.cli import main
from devup.discovery import discover_workspace
from devup.dotenv import parse_dotenv
from devup.models import Task, WorkspaceConfig
from devup.picker import PickChoice, pick_targets
from devup.ports import check_task_ports, extract_ports_from_command
from devup.runner import RunOptions, run_tasks
from devup.watcher import diff_snapshots, snapshot_tree


class DotenvTests(unittest.TestCase):
    def test_parse_dotenv_variants(self) -> None:
        raw = """
# comment
PLAIN=hello
export EXPORTED=world
QUOTED="line1\\nline2" # trailing comment
SINGLE='literal\\nvalue' # comment
URL=https://example.com/path#fragment
INLINE=val # inline comment
EMPTY=
""".strip()
        parsed = parse_dotenv(raw)
        self.assertEqual(parsed["PLAIN"], "hello")
        self.assertEqual(parsed["EXPORTED"], "world")
        self.assertEqual(parsed["QUOTED"], "line1\nline2")
        self.assertEqual(parsed["SINGLE"], "literal\\nvalue")
        self.assertEqual(parsed["URL"], "https://example.com/path#fragment")
        self.assertEqual(parsed["INLINE"], "val")
        self.assertEqual(parsed["EMPTY"], "")

    def test_dotenv_auto_and_task_env_file_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("GREETING=from-root\nONLY_ROOT=yes\n", encoding="utf-8")
            (root / ".env.api").write_text("GREETING=from-api\n", encoding="utf-8")
            cmd = json.dumps(
                f'"{sys.executable}" -c "import os; print(os.environ[\'GREETING\'] + \':\' + os.environ[\'ONLY_ROOT\'])"'
            )
            (root / "devup.toml").write_text(
                f"""
[tasks.api]
command = {cmd}
env_file = ".env.api"
""".strip(),
                encoding="utf-8",
            )

            out = io.StringIO()
            rc = main(["-C", str(root), "--no-color", "api"], stream=out)
            self.assertEqual(rc, 0)
            self.assertIn("[api] from-api:yes", out.getvalue())

    def test_shell_env_wins_over_root_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("DEVUP_TEST_SHELL_KEY=from-dotenv\n", encoding="utf-8")
            cmd = json.dumps(
                f'"{sys.executable}" -c "import os; print(os.environ[\'DEVUP_TEST_SHELL_KEY\'])"'
            )
            (root / "devup.toml").write_text(
                f'[tasks.echo]\ncommand = {cmd}\n',
                encoding="utf-8",
            )
            prev = os.environ.get("DEVUP_TEST_SHELL_KEY")
            os.environ["DEVUP_TEST_SHELL_KEY"] = "from-shell"
            try:
                out = io.StringIO()
                rc = main(["-C", str(root), "--no-color", "echo"], stream=out)
                self.assertEqual(rc, 0)
                self.assertIn("[echo] from-shell", out.getvalue())
            finally:
                if prev is None:
                    os.environ.pop("DEVUP_TEST_SHELL_KEY", None)
                else:
                    os.environ["DEVUP_TEST_SHELL_KEY"] = prev


class PortCheckTests(unittest.TestCase):
    def test_extract_ports_from_command(self) -> None:
        self.assertEqual(
            extract_ports_from_command("PORT=3000 uvicorn app:app --port 8000 --port=8001"),
            [8000, 8001, 3000],
        )
        self.assertEqual(extract_ports_from_command("pytest -p no:cacheprovider"), [])

    def test_strict_ports_aborts_when_port_is_in_use(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            busy_port = srv.getsockname()[1]

            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                cfg = WorkspaceConfig(
                    root=root,
                    tasks={
                        "api": Task(
                            name="api",
                            command=f'"{sys.executable}" -c "print(\'should-not-run\')"',
                            source="devup.toml",
                            cwd=root,
                            ports=[busy_port],
                        )
                    },
                )
                buf = io.StringIO()
                results = run_tasks(
                    cfg,
                    ["api"],
                    options=RunOptions(color=False, strict_ports=True),
                    stream=buf,
                )
                self.assertEqual(results[0].status, "error")
                self.assertEqual(results[0].exit_code, 2)
                self.assertIn(f"port {busy_port} is already in use", buf.getvalue())
                self.assertNotIn("should-not-run", buf.getvalue())

    def test_duplicate_ports_across_tasks_detected(self) -> None:
        tasks = {
            "a": Task(name="a", command="echo a", source="devup.toml", ports=[45678]),
            "b": Task(name="b", command="echo b", source="devup.toml", ports=[45678]),
        }
        conflicts = check_task_ports(tasks, ["a", "b"])
        self.assertTrue(any(c.task_name == "b" and c.port == 45678 for c in conflicts))


class WatcherAndPickerTests(unittest.TestCase):
    def test_snapshot_ignores_git_and_node_modules_and_detects_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "node_modules").mkdir()
            (root / "node_modules" / "dep.js").write_text("1", encoding="utf-8")
            src = root / "src"
            src.mkdir()
            app_py = src / "app.py"
            app_py.write_text("v1", encoding="utf-8")

            snap1 = snapshot_tree(root, patterns=["**/*.py", "*.js"])
            self.assertIn(app_py.resolve(), snap1)
            self.assertNotIn((root / "node_modules" / "dep.js").resolve(), snap1)

            new_py = src / "extra.py"
            new_py.write_text("v1", encoding="utf-8")
            snap2 = snapshot_tree(root, patterns=["**/*.py", "*.js"])
            changed = diff_snapshots(snap1, snap2)
            self.assertEqual(changed, new_py.resolve())

    def test_watch_restarts_task_on_file_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            msg_file = root / "message.txt"
            msg_file.write_text("v1", encoding="utf-8")
            script = root / "runner_probe.py"
            script.write_text(
                "from pathlib import Path\n"
                "print('BOOT:' + Path('message.txt').read_text(encoding='utf-8'), flush=True)\n",
                encoding="utf-8",
            )
            cfg = WorkspaceConfig(
                root=root,
                tasks={
                    "probe": Task(
                        name="probe",
                        command=f'"{sys.executable}" "{script}"',
                        source="devup.toml",
                        cwd=root,
                        watch=["*.txt"],
                        watch_enabled=True,
                    )
                },
            )

            stop_sig = threading.Event()
            buf = io.StringIO()

            def trigger_change_and_stop() -> None:
                deadline = time.monotonic() + 5.0
                while "BOOT:v1" not in buf.getvalue() and time.monotonic() < deadline:
                    time.sleep(0.05)
                msg_file.write_text("v2", encoding="utf-8")
                deadline2 = time.monotonic() + 5.0
                while "BOOT:v2" not in buf.getvalue() and time.monotonic() < deadline2:
                    time.sleep(0.05)
                stop_sig.set()

            driver = threading.Thread(target=trigger_change_and_stop, daemon=True)
            driver.start()
            run_tasks(
                cfg,
                ["probe"],
                options=RunOptions(color=False, port_check=False, watch_interval=0.1),
                stream=buf,
                stop_signal=stop_sig,
            )
            driver.join(timeout=2.0)

            output = buf.getvalue()
            self.assertIn("BOOT:v1", output)
            self.assertIn("Change detected in message.txt", output)
            self.assertIn("BOOT:v2", output)

    def test_discovery_parses_new_devup_toml_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "devup.toml").write_text(
                """
auto_env = false
strict_ports = true
env_file = [".env", ".env.local"]

[tasks.api]
command = "uvicorn app:app"
port = 8000
env_file = ".env.api"
watch = ["src/**/*.py"]
""".strip(),
                encoding="utf-8",
            )
            cfg = discover_workspace(root)
            self.assertFalse(cfg.auto_env)
            self.assertTrue(cfg.strict_ports)
            self.assertEqual(cfg.env_files, [".env", ".env.local"])
            self.assertEqual(cfg.tasks["api"].ports, [8000])
            self.assertEqual(cfg.tasks["api"].env_files, [".env.api"])
            self.assertTrue(cfg.tasks["api"].watch_enabled)
            self.assertEqual(cfg.tasks["api"].watch, ["src/**/*.py"])

    def test_numbered_picker_fallback(self) -> None:
        choices = [
            PickChoice("api", "uvicorn app:app"),
            PickChoice("web", "vite"),
        ]
        out = io.StringIO()
        picked = pick_targets(choices, out=out, use_color=False, stdin=io.StringIO("2, 1\n"))
        self.assertEqual(picked, ["web", "api"])


if __name__ == "__main__":
    unittest.main()
