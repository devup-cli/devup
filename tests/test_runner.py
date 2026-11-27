"""Integration tests for the parallel service runner."""

from __future__ import annotations

import io
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from devup.models import Task, WorkspaceConfig
from devup.runner import RunOptions, run_tasks


def _py_cmd(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


class RunnerTests(unittest.TestCase):
    def test_parallel_prefixed_logs_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = WorkspaceConfig(
                root=root,
                env={"SHARED_TAG": "hello"},
                tasks={
                    "api": Task(
                        name="api",
                        command=_py_cmd("import os; print(os.environ['SHARED_TAG'] + '-api')"),
                        source="devup.toml",
                        cwd=root,
                    ),
                    "web": Task(
                        name="web",
                        command=_py_cmd("import os; print(os.environ['PORT'] + '-web')"),
                        source="devup.toml",
                        cwd=root,
                        env={"PORT": "5173"},
                    ),
                },
            )
            buf = io.StringIO()
            results = run_tasks(
                cfg,
                ["api", "web"],
                options=RunOptions(color=False, timestamps=False),
                stream=buf,
            )
            output = buf.getvalue()
            self.assertEqual([r.status for r in results], ["ok", "ok"])
            self.assertIn("[api] hello-api", output)
            self.assertIn("[web] 5173-web", output)

    def test_ready_pattern_ordering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = WorkspaceConfig(
                root=root,
                tasks={
                    "db": Task(
                        name="db",
                        command=_py_cmd(
                            "import time; print('booting', flush=True); "
                            "time.sleep(0.15); print('READY_ON_5432', flush=True); "
                            "time.sleep(0.25)"
                        ),
                        source="devup.toml",
                        cwd=root,
                        ready_pattern=r"READY_ON_\d+",
                    ),
                    "api": Task(
                        name="api",
                        command=_py_cmd("print('API_CONNECTED', flush=True)"),
                        source="devup.toml",
                        cwd=root,
                        depends_on=["db"],
                    ),
                },
            )
            buf = io.StringIO()
            results = run_tasks(
                cfg,
                ["api"],
                options=RunOptions(color=False),
                stream=buf,
            )
            output = buf.getvalue()
            self.assertEqual([r.status for r in results], ["ok", "ok"])
            self.assertLess(output.index("READY_ON_5432"), output.index("API_CONNECTED"))

    def test_fail_fast_stops_sibling_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = WorkspaceConfig(
                root=root,
                fail_fast=True,
                shutdown_timeout=1.0,
                tasks={
                    "crasher": Task(
                        name="crasher",
                        command=_py_cmd("import sys, time; time.sleep(0.1); sys.exit(2)"),
                        source="devup.toml",
                        cwd=root,
                    ),
                    "sleeper": Task(
                        name="sleeper",
                        command=_py_cmd("import time; print('sleeping', flush=True); time.sleep(15)"),
                        source="devup.toml",
                        cwd=root,
                    ),
                },
            )
            buf = io.StringIO()
            t0 = time.monotonic()
            results = run_tasks(
                cfg,
                ["crasher", "sleeper"],
                options=RunOptions(color=False),
                stream=buf,
            )
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 5.0)
            by_name = {r.name: r for r in results}
            self.assertEqual(by_name["crasher"].status, "failed")
            self.assertEqual(by_name["crasher"].exit_code, 2)
            self.assertEqual(by_name["sleeper"].status, "stopped")

    def test_keep_going_allows_other_services_to_finish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = WorkspaceConfig(
                root=root,
                tasks={
                    "crasher": Task(
                        name="crasher",
                        command=_py_cmd("import sys; sys.exit(3)"),
                        source="devup.toml",
                        cwd=root,
                    ),
                    "survivor": Task(
                        name="survivor",
                        command=_py_cmd("import time; time.sleep(0.2); print('still-here')"),
                        source="devup.toml",
                        cwd=root,
                    ),
                },
            )
            buf = io.StringIO()
            results = run_tasks(
                cfg,
                ["crasher", "survivor"],
                options=RunOptions(fail_fast=False, color=False),
                stream=buf,
            )
            by_name = {r.name: r for r in results}
            self.assertEqual(by_name["crasher"].status, "failed")
            self.assertEqual(by_name["survivor"].status, "ok")
            self.assertIn("still-here", buf.getvalue())

    def test_dependency_cycle_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = WorkspaceConfig(
                root=root,
                tasks={
                    "a": Task(name="a", command="echo a", source="devup.toml", depends_on=["b"]),
                    "b": Task(name="b", command="echo b", source="devup.toml", depends_on=["a"]),
                },
            )
            with self.assertRaises(ValueError):
                run_tasks(cfg, ["a"], options=RunOptions(color=False), stream=io.StringIO())


if __name__ == "__main__":
    unittest.main()
