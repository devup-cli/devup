"""Phase 1 reliability tests: Maven/Gradle discovery, HTTP/TCP readiness, process cleanup, and OS portability."""

from __future__ import annotations

import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from devup.discovery import discover_workspace
from devup.models import Task, WorkspaceConfig
from devup.ports import _lookup_posix_owner
from devup.proc_tree import ManagedProcess, spawn_managed_process, terminate_managed_process
from devup.readiness import (
    normalize_tcp_target,
    parse_tcp_target,
    probe_http,
    validate_ready_url,
)
from devup.runner import RunOptions, run_tasks


def _is_pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        synchronize = 0x00100000
        process_query_limited = 0x1000
        handle = kernel32.OpenProcess(synchronize | process_query_limited, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == 259  # STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class MavenGradleDiscoveryTests(unittest.TestCase):
    def test_maven_wrapper_plugins_and_modules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wrapper = "mvnw.cmd" if os.name == "nt" else "mvnw"
            (root / wrapper).write_text("", encoding="utf-8")
            (root / "pom.xml").write_text(
                """
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>demo</artifactId>
  <modules>
    <module>orders</module>
  </modules>
  <build>
    <plugins>
      <plugin>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-maven-plugin</artifactId>
      </plugin>
    </plugins>
  </build>
</project>
""".strip(),
                encoding="utf-8",
            )
            orders = root / "orders"
            orders.mkdir()
            (orders / "pom.xml").write_text(
                "<project><modelVersion>4.0.0</modelVersion><artifactId>orders</artifactId></project>",
                encoding="utf-8",
            )

            cfg = discover_workspace(root)
            expected_mvn = "mvnw.cmd" if os.name == "nt" else "./mvnw"
            self.assertEqual(cfg.tasks["dev"].command, f"{expected_mvn} spring-boot:run")
            self.assertEqual(cfg.tasks["test"].command, f"{expected_mvn} test")
            self.assertIn("orders:compile", cfg.tasks)
            self.assertIn("orders:test", cfg.tasks)
            expected_rel = "..\\mvnw.cmd" if os.name == "nt" else "../mvnw"
            self.assertEqual(cfg.tasks["orders:compile"].command, f"{expected_rel} compile")

    def test_gradle_kts_and_subprojects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wrapper = "gradlew.bat" if os.name == "nt" else "gradlew"
            (root / wrapper).write_text("", encoding="utf-8")
            (root / "settings.gradle.kts").write_text(
                'rootProject.name = "platform"\ninclude(":api", ":worker")\n',
                encoding="utf-8",
            )
            (root / "build.gradle.kts").write_text(
                """
plugins {
    id("org.springframework.boot") version "3.3.0"
    application
}

tasks.register("smoke") {
    doLast { println("smoke") }
}
""".strip(),
                encoding="utf-8",
            )
            for sub in ("api", "worker"):
                sub_dir = root / sub
                sub_dir.mkdir()
                (sub_dir / "build.gradle.kts").write_text('plugins { java }\n', encoding="utf-8")

            cfg = discover_workspace(root)
            expected_gradle = "gradlew.bat" if os.name == "nt" else "./gradlew"
            self.assertEqual(cfg.tasks["bootRun"].command, f"{expected_gradle} bootRun")
            self.assertEqual(cfg.tasks["run"].command, f"{expected_gradle} run")
            self.assertEqual(cfg.tasks["smoke"].command, f"{expected_gradle} smoke")
            self.assertIn("api:build", cfg.tasks)
            self.assertIn("worker:test", cfg.tasks)

    def test_posix_wrapper_selection_cross_platform(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mvnw").write_text("", encoding="utf-8")
            (root / "gradlew").write_text("", encoding="utf-8")
            (root / "pom.xml").write_text(
                "<project><artifactId>svc</artifactId></project>",
                encoding="utf-8",
            )
            (root / "build.gradle").write_text("apply plugin: 'java'\ntask customJob {}\n", encoding="utf-8")

            with mock.patch("devup.discovery.os.name", "posix"):
                cfg = discover_workspace(root)
            self.assertEqual(cfg.tasks["test"].command, "./mvnw test")
            self.assertEqual(cfg.tasks["customJob"].command, "./gradlew customJob")


class ReadinessProbeTests(unittest.TestCase):
    def test_tcp_and_url_parsers(self) -> None:
        self.assertEqual(normalize_tcp_target(5432), "127.0.0.1:5432")
        self.assertEqual(parse_tcp_target("127.0.0.1:8080"), ("127.0.0.1", 8080))
        self.assertEqual(parse_tcp_target("[::1]:9000"), ("::1", 9000))
        self.assertEqual(validate_ready_url("http://127.0.0.1:8000/health"), "http://127.0.0.1:8000/health")
        with self.assertRaises(ValueError):
            validate_ready_url("ftp://127.0.0.1/file")
        with self.assertRaises(ValueError):
            parse_tcp_target("invalid-target")

    def test_tcp_readiness_ordering(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_sock:
            probe_sock.bind(("127.0.0.1", 0))
            port = probe_sock.getsockname()[1]

        server_code = (
            "import socket, time; "
            "time.sleep(0.25); "
            "s = socket.socket(); "
            "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); "
            f"s.bind(('127.0.0.1', {port})); "
            "s.listen(5); "
            "print('TCP_BOUND', flush=True); "
            "time.sleep(0.8); "
            "s.close()"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = WorkspaceConfig(
                root=root,
                tasks={
                    "db": Task(
                        name="db",
                        command=f'"{sys.executable}" -c "{server_code}"',
                        source="devup.toml",
                        cwd=root,
                        ready_tcp=f"127.0.0.1:{port}",
                        ready_timeout=5.0,
                    ),
                    "api": Task(
                        name="api",
                        command=f'"{sys.executable}" -c "print(\'API_AFTER_TCP\', flush=True)"',
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
                options=RunOptions(color=False, port_check=False),
                stream=buf,
            )
            output = buf.getvalue()
            self.assertEqual([r.status for r in results], ["ok", "ok"])
            self.assertIn(f"ready (tcp://127.0.0.1:{port})", output)
            self.assertLess(output.index("TCP_BOUND"), output.index("API_AFTER_TCP"))

    def test_http_readiness_waits_for_200(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_sock:
            probe_sock.bind(("127.0.0.1", 0))
            port = probe_sock.getsockname()[1]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            srv_script = root / "http_srv.py"
            srv_script.write_text(
                "import http.server, socketserver, threading, time\n"
                "calls = 0\n"
                "done = threading.Event()\n"
                "class FastHTTPServer(http.server.HTTPServer):\n"
                "    def server_bind(self):\n"
                "        socketserver.TCPServer.server_bind(self)\n"
                "        self.server_name = '127.0.0.1'\n"
                "        self.server_port = self.server_address[1]\n"
                "class H(http.server.BaseHTTPRequestHandler):\n"
                "    def do_GET(self):\n"
                "        global calls\n"
                "        calls += 1\n"
                "        code = 503 if calls < 3 else 200\n"
                "        self.send_response(code)\n"
                "        self.send_header('Content-Length', '2')\n"
                "        self.end_headers()\n"
                "        self.wfile.write(b'ok')\n"
                "        if calls >= 3:\n"
                "            print('HTTP_200_SERVED', flush=True)\n"
                "            done.set()\n"
                "    def log_message(self, *args):\n"
                "        pass\n"
                f"srv = FastHTTPServer(('127.0.0.1', {port}), H)\n"
                "threading.Thread(target=srv.serve_forever, daemon=True).start()\n"
                "done.wait(timeout=4.5)\n"
                "time.sleep(0.2)\n"
                "srv.shutdown()\n"
                "srv.server_close()\n",
                encoding="utf-8",
            )
            url = f"http://127.0.0.1:{port}/health"
            cfg = WorkspaceConfig(
                root=root,
                tasks={
                    "api": Task(
                        name="api",
                        command=f'"{sys.executable}" "{srv_script}"',
                        source="devup.toml",
                        cwd=root,
                        ready_url=url,
                        ready_timeout=5.0,
                    ),
                    "web": Task(
                        name="web",
                        command=f'"{sys.executable}" -c "print(\'WEB_AFTER_HTTP\', flush=True)"',
                        source="devup.toml",
                        cwd=root,
                        depends_on=["api"],
                    ),
                },
            )
            buf = io.StringIO()
            results = run_tasks(
                cfg,
                ["web"],
                options=RunOptions(color=False, port_check=False),
                stream=buf,
            )
            output = buf.getvalue()
            self.assertEqual([r.status for r in results], ["ok", "ok"], msg=output)
            self.assertIn(f"ready ({url})", output)
            self.assertLess(output.index("HTTP_200_SERVED"), output.index("WEB_AFTER_HTTP"))

    def test_readiness_timeout_skips_dependent(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_sock:
            probe_sock.bind(("127.0.0.1", 0))
            unused_port = probe_sock.getsockname()[1]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = WorkspaceConfig(
                root=root,
                tasks={
                    "db": Task(
                        name="db",
                        command=f'"{sys.executable}" -c "import time; time.sleep(10)"',
                        source="devup.toml",
                        cwd=root,
                        ready_tcp=f"127.0.0.1:{unused_port}",
                        ready_timeout=0.5,
                    ),
                    "api": Task(
                        name="api",
                        command=f'"{sys.executable}" -c "print(\'NEVER\')"',
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
                options=RunOptions(color=False, port_check=False, shutdown_timeout=0.5),
                stream=buf,
            )
            by_name = {r.name: r for r in results}
            self.assertEqual(by_name["db"].status, "failed")
            self.assertEqual(by_name["api"].status, "skipped")
            self.assertIn("readiness check timed out", buf.getvalue())
            self.assertNotIn("NEVER", buf.getvalue())

    def test_http_readiness_handles_streaming_body_without_blocking(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_sock:
            probe_sock.bind(("127.0.0.1", 0))
            port = probe_sock.getsockname()[1]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            srv_script = root / "stream_srv.py"
            srv_script.write_text(
                "import http.server, socketserver, threading, time\n"
                "done = threading.Event()\n"
                "class FastHTTPServer(http.server.HTTPServer):\n"
                "    def server_bind(self):\n"
                "        socketserver.TCPServer.server_bind(self)\n"
                "        self.server_name = '127.0.0.1'\n"
                "        self.server_port = self.server_address[1]\n"
                "class H(http.server.BaseHTTPRequestHandler):\n"
                "    def do_GET(self):\n"
                "        self.send_response(200)\n"
                "        self.send_header('Content-Type', 'text/event-stream')\n"
                "        self.end_headers()\n"
                "        try:\n"
                "            self.wfile.write(b'data: open\\n\\n')\n"
                "            self.wfile.flush()\n"
                "            done.wait(timeout=8.0)\n"
                "        except OSError:\n"
                "            pass\n"
                "    def log_message(self, *args):\n"
                "        pass\n"
                f"srv = FastHTTPServer(('127.0.0.1', {port}), H)\n"
                "threading.Thread(target=srv.serve_forever, daemon=True).start()\n"
                "print('STREAM_LISTENING', flush=True)\n"
                "done.wait(timeout=8.0)\n",
                encoding="utf-8",
            )
            url = f"http://127.0.0.1:{port}/events"
            proc = subprocess.Popen(
                [sys.executable, str(srv_script)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                assert proc.stdout is not None
                line = proc.stdout.readline()
                self.assertIn("STREAM_LISTENING", line)
                t0 = time.monotonic()
                self.assertTrue(probe_http(url, timeout=0.5))
                self.assertLess(time.monotonic() - t0, 1.5)
            finally:
                proc.kill()
                proc.wait(timeout=2)
                if proc.stdout is not None:
                    proc.stdout.close()


class ProcessCleanupAndCrossPlatformTests(unittest.TestCase):
    def test_grandchild_process_tree_is_killed_on_fail_fast(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "grandchild.pid"
            spawner = root / "spawn_child.py"
            spawner.write_text(
                "import subprocess, sys, time\n"
                "from pathlib import Path\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                f"Path({json.dumps(str(pid_file))}).write_text(str(child.pid), encoding='utf-8')\n"
                "print('CHILD_SPAWNED', flush=True)\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            cfg = WorkspaceConfig(
                root=root,
                fail_fast=True,
                shutdown_timeout=0.5,
                tasks={
                    "parent": Task(
                        name="parent",
                        command=f'"{sys.executable}" "{spawner}"',
                        source="devup.toml",
                        cwd=root,
                    ),
                    "crasher": Task(
                        name="crasher",
                        command=f'"{sys.executable}" -c "import sys, time; time.sleep(0.5); sys.exit(1)"',
                        source="devup.toml",
                        cwd=root,
                    ),
                },
            )
            buf = io.StringIO()
            results = run_tasks(
                cfg,
                ["parent", "crasher"],
                options=RunOptions(color=False, port_check=False),
                stream=buf,
            )
            by_name = {r.name: r for r in results}
            self.assertEqual(by_name["parent"].status, "stopped")
            self.assertEqual(by_name["crasher"].status, "failed")
            self.assertTrue(pid_file.is_file(), "Grandchild PID file should have been written")
            grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())
            # Give the OS a moment to finalize process reaping.
            deadline = time.monotonic() + 2.0
            while _is_pid_alive(grandchild_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(
                _is_pid_alive(grandchild_pid),
                f"Grandchild PID {grandchild_pid} survived process-tree termination",
            )

    def test_posix_terminate_managed_process_sweeps_pgid(self) -> None:
        fake_proc = mock.MagicMock()
        fake_proc.poll.side_effect = [None, None, None, 0]
        fake_proc.pid = 4242
        managed = ManagedProcess(proc=fake_proc, pgid=4242, job_handle=None)

        with (
            mock.patch("devup.proc_tree.os.name", "posix"),
            mock.patch("devup.proc_tree.os.killpg", create=True) as mock_killpg,
            mock.patch("devup.proc_tree.signal.SIGTERM", 15, create=True),
            mock.patch("devup.proc_tree.signal.SIGKILL", 9, create=True),
        ):
            terminate_managed_process(managed, timeout=0.05, force_immediately=False)

        calls = [call.args for call in mock_killpg.call_args_list]
        self.assertIn((4242, 15), calls)
        self.assertIn((4242, 9), calls)

    def test_posix_port_owner_lookup_lsof_and_ss(self) -> None:
        lsof_res = subprocess.CompletedProcess(
            args=["lsof"],
            returncode=0,
            stdout="p9123\ncuvicorn\n",
            stderr="",
        )
        with mock.patch("devup.ports.subprocess.run", return_value=lsof_res):
            self.assertEqual(_lookup_posix_owner(8000), "PID 9123 (uvicorn)")

        ss_res = subprocess.CompletedProcess(
            args=["ss"],
            returncode=0,
            stdout='LISTEN 0 128 127.0.0.1:5173 0.0.0.0:* users:(("node",pid=3210,fd=21))\n',
            stderr="",
        )
        with mock.patch("devup.ports.subprocess.run", side_effect=[OSError("no lsof"), ss_res]):
            self.assertEqual(_lookup_posix_owner(5173), "PID 3210 (node)")

    @unittest.skipUnless(os.name == "nt", "Windows fallback process cleanup regression test")
    def test_windows_cleanup_when_job_object_fails_and_parent_exits_early(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "orphan.pid"
            spawner = root / "early_parent.py"
            spawner.write_text(
                "import subprocess, sys, time\n"
                "from pathlib import Path\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                f"Path({json.dumps(str(pid_file))}).write_text(str(child.pid), encoding='utf-8')\n"
                "print('ORPHAN_SPAWNED', flush=True)\n"
                "time.sleep(0.3)\n",
                encoding="utf-8",
            )
            with mock.patch("devup.proc_tree._create_windows_job_for_process", return_value=None):
                managed = spawn_managed_process(
                    f'"{sys.executable}" "{spawner}"',
                    cwd=str(root),
                    env=os.environ.copy(),
                )
            self.assertIsNone(managed.job_handle)
            deadline = time.monotonic() + 3.0
            while not pid_file.is_file() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(pid_file.is_file())
            managed.refresh_descendants()
            orphan_pid = int(pid_file.read_text(encoding="utf-8").strip())
            self.assertIn(orphan_pid, managed.known_descendants)
            # Wait for the parent to exit early before terminate_managed_process runs.
            managed.proc.wait(timeout=3.0)
            self.assertTrue(_is_pid_alive(orphan_pid))

            terminate_managed_process(managed, timeout=0.5, force_immediately=False)
            kill_deadline = time.monotonic() + 2.0
            while _is_pid_alive(orphan_pid) and time.monotonic() < kill_deadline:
                time.sleep(0.05)
            self.assertFalse(
                _is_pid_alive(orphan_pid),
                f"Orphan PID {orphan_pid} survived fallback cleanup after early parent exit",
            )
            if managed.proc.stdout is not None:
                managed.proc.stdout.close()

    def test_sigint_handler_stops_running_services_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = WorkspaceConfig(
                root=root,
                shutdown_timeout=0.5,
                tasks={
                    "worker": Task(
                        name="worker",
                        command=f'"{sys.executable}" -c "import time; print(\'STARTED\', flush=True); time.sleep(15)"',
                        source="devup.toml",
                        cwd=root,
                    )
                },
            )
            buf = io.StringIO()

            def send_sigint() -> None:
                deadline = time.monotonic() + 3.0
                while "STARTED" not in buf.getvalue() and time.monotonic() < deadline:
                    time.sleep(0.05)
                signal.raise_signal(signal.SIGINT)

            t0 = time.monotonic()
            trigger = threading.Thread(target=send_sigint, daemon=True)
            trigger.start()
            results = run_tasks(
                cfg,
                ["worker"],
                options=RunOptions(color=False, port_check=False),
                stream=buf,
            )
            trigger.join(timeout=2.0)
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 5.0)
            self.assertEqual([r.status for r in results], ["stopped"], msg=buf.getvalue())
            self.assertIn("Interrupted:", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
