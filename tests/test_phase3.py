"""Phase 3 tests: Cargo/Go/Taskfile/mise discovery, log filtering, buffering, verbose debug, and bundles."""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from build_bundles import build_platform_bundles
from devup.cli import main
from devup.diagnostics import validate_workspace
from devup.discovery import discover_workspace, resolve_targets
from devup.models import Task, WorkspaceConfig
from devup.runner import RunOptions, run_tasks


class BroadenedDiscoveryTests(unittest.TestCase):
    def test_cargo_package_bins_and_workspace_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Cargo.toml").write_text(
                """
[workspace]
members = ["crates/*"]

[package]
name = "root-app"
version = "0.1.0"

[[bin]]
name = "api-srv"
path = "src/bin/api.rs"
""".strip(),
                encoding="utf-8",
            )
            crate_dir = root / "crates" / "worker"
            (crate_dir / "src").mkdir(parents=True)
            (crate_dir / "src" / "main.rs").write_text("fn main() {}", encoding="utf-8")
            (crate_dir / "Cargo.toml").write_text(
                '[package]\nname = "worker"\nversion = "0.1.0"\n',
                encoding="utf-8",
            )

            cfg = discover_workspace(root)
            self.assertEqual(cfg.tasks["run"].command, "cargo run")
            self.assertEqual(cfg.tasks["run:api-srv"].command, "cargo run --bin api-srv")
            self.assertEqual(cfg.tasks["clippy"].command, "cargo clippy")
            self.assertEqual(cfg.tasks["worker:run"].command, "cargo run")
            self.assertEqual(cfg.tasks["worker:test"].runner, "cargo")

    def test_go_mod_cmd_bins_and_go_work_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "go.work").write_text(
                'go 1.22\n\nuse (\n  ./services/auth\n)\n',
                encoding="utf-8",
            )
            (root / "go.mod").write_text("module example.com/root\n\ngo 1.22\n", encoding="utf-8")
            (root / "cmd" / "gateway").mkdir(parents=True)
            (root / "cmd" / "gateway" / "main.go").write_text("package main\n", encoding="utf-8")

            auth_dir = root / "services" / "auth"
            auth_dir.mkdir(parents=True)
            (auth_dir / "go.mod").write_text("module example.com/auth\n\ngo 1.22\n", encoding="utf-8")
            (auth_dir / "main.go").write_text("package main\n", encoding="utf-8")

            cfg = discover_workspace(root)
            self.assertEqual(cfg.tasks["run:gateway"].command, "go run ./cmd/gateway")
            self.assertEqual(cfg.tasks["test"].command, "go test ./...")
            self.assertEqual(cfg.tasks["vet"].command, "go vet ./...")
            self.assertEqual(cfg.tasks["auth:run"].command, "go run .")

    def test_taskfile_and_mise_toml_with_collision_and_error_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Taskfile.yml").write_text(
                """
version: '3'

tasks:
  dev:
    desc: Start Taskfile dev stack
    cmds:
      - echo dev
  secret:
    internal: true
    cmds:
      - echo hidden
""".strip(),
                encoding="utf-8",
            )
            (root / "mise.toml").write_text(
                """
[tasks.dev]
description = "Run mise dev"
run = "python app.py"

[tasks.lint]
run = "ruff check ."

[tasks.hidden]
hide = true
run = "echo no"
""".strip(),
                encoding="utf-8",
            )

            cfg = discover_workspace(root)
            self.assertEqual(cfg.tasks["dev"].command, "task dev")
            self.assertEqual(cfg.tasks["dev"].description, "Start Taskfile dev stack")
            self.assertEqual(cfg.tasks["mise:dev"].command, "mise run dev")
            self.assertEqual(cfg.tasks["lint"].command, "mise run lint")
            self.assertNotIn("secret", cfg.tasks)
            self.assertNotIn("hidden", cfg.tasks)

        with tempfile.TemporaryDirectory() as bad_tmp:
            bad_root = Path(bad_tmp)
            (bad_root / "Cargo.toml").write_text("[package\nbroken", encoding="utf-8")
            bad_cfg = discover_workspace(bad_root)
            self.assertTrue(
                any("Cargo.toml" in w and str(bad_root.resolve()) in w for w in bad_cfg.warnings),
                msg=f"Expected repo & manifest path in warning: {bad_cfg.warnings}",
            )


class RunnerUsabilityAndDiagnosticsTests(unittest.TestCase):
    def test_log_filtering_only_exclude_group_buffer_and_save_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_file = root / "logs" / "run.log"
            cfg = WorkspaceConfig(
                root=root,
                groups={"web": ["api", "worker"]},
                tasks={
                    "api": Task(
                        name="api",
                        command=f'"{sys.executable}" -c "print(\'API_LINE_1\'); print(\'API_LINE_2\')"',
                        source="devup.toml",
                        cwd=root,
                    ),
                    "worker": Task(
                        name="worker",
                        command=f'"{sys.executable}" -c "print(\'WORKER_SECRET_NOISE\')"',
                        source="devup.toml",
                        cwd=root,
                    ),
                },
            )
            out = io.StringIO()
            targets = resolve_targets(cfg, ["web"])
            results = run_tasks(
                cfg,
                targets,
                options=RunOptions(
                    color=False,
                    port_check=False,
                    only=["api"],
                    buffer_mode="group",
                    save_logs=log_file,
                    verbose=True,
                ),
                stream=out,
            )
            self.assertEqual([r.status for r in results], ["ok", "ok"])
            text = out.getvalue()
            self.assertIn("API_LINE_1", text)
            self.assertIn("API_LINE_2", text)
            self.assertNotIn("WORKER_SECRET_NOISE", text)
            self.assertIn("[debug]", text)
            saved = log_file.read_text(encoding="utf-8")
            self.assertIn("API_LINE_1", saved)
            self.assertIn("WORKER_SECRET_NOISE", saved)

            out_excl = io.StringIO()
            run_tasks(
                cfg,
                targets,
                options=RunOptions(color=False, port_check=False, exclude=["api"]),
                stream=out_excl,
            )
            self.assertNotIn("API_LINE_1", out_excl.getvalue())
            self.assertIn("WORKER_SECRET_NOISE", out_excl.getvalue())

    def test_missing_executable_hint_and_verbose_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "devup.toml").write_text(
                '[tasks.bad]\ncommand = "definitely_missing_binary_xyz_999 --flag"\n',
                encoding="utf-8",
            )
            out = io.StringIO()
            rc = main(["-C", str(root), "--no-color", "-v", "bad"], stream=out)
            self.assertNotEqual(rc, 0)
            text = out.getvalue()
            self.assertIn("definitely_missing_binary_xyz_999", text)
            self.assertIn("devup doctor", text)
            self.assertIn("[debug]", text)

    def test_package_json_makefile_justfile_real_execution_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("ROOT_VAR=from_dotenv\n", encoding="utf-8")
            (root / "package.json").write_text(
                json.dumps({"scripts": {"echoenv": f'"{sys.executable}" -c "import os; print(os.environ.get(\'ROOT_VAR\'))"'}}),
                encoding="utf-8",
            )
            (root / "Makefile").write_text("hello:\n\techo make-ok\n", encoding="utf-8")
            (root / "justfile").write_text("greet:\n\techo just-ok\n", encoding="utf-8")

            out = io.StringIO()
            rc = main(["-C", str(root), "--no-color", "--dry-run", "echoenv", "hello", "greet"], stream=out)
            self.assertEqual(rc, 0, out.getvalue())
            self.assertIn("npm run echoenv", out.getvalue())
            self.assertIn("make hello", out.getvalue())
            self.assertIn("just greet", out.getvalue())

    def test_platform_bundle_builder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "bundles"
            files = build_platform_bundles(out_dir, tag="0.3.0")
            self.assertEqual(len(files), 4)
            win_zip = out_dir / "devup-v0.3.0-windows.zip"
            posix_tgz = out_dir / "devup-v0.3.0-posix.tar.gz"
            with zipfile.ZipFile(win_zip) as zf:
                self.assertIn("devup.cmd", zf.namelist())
                self.assertIn("devup.pyz", zf.namelist())
            with tarfile.open(posix_tgz, "r:gz") as tf:
                self.assertIn("devup", tf.getnames())

    def test_make_just_monorepo_shims_and_env_precedence_integration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "shim_bin"
            bin_dir.mkdir()
            pkg_dir = root / "packages" / "web"
            pkg_dir.mkdir(parents=True)

            # Create cross-platform fake make, just, and npm shims to verify real execution
            # of Makefile, Justfile, and package.json workspaces without external binary deps.
            for tool in ("make", "just", "npm"):
                py_target = (bin_dir / f"{tool}_impl.py")
                py_target.write_text(
                    f"import os, sys\nprint('{tool.upper()}_EXEC:' + ' '.join(sys.argv[1:]) + ':ENV=' + os.environ.get('DEVUP_T_VAR', ''))\n",
                    encoding="utf-8",
                )
                if os.name == "nt":
                    cmd_path = bin_dir / f"{tool}.cmd"
                    cmd_path.write_text(
                        f'@echo off\r\n"{sys.executable}" "{py_target}" %*\r\n',
                        encoding="utf-8",
                    )
                else:
                    sh_path = bin_dir / tool
                    sh_path.write_text(
                        f'#!/bin/sh\nexec "{sys.executable}" "{py_target}" "$@"\n',
                        encoding="utf-8",
                    )
                    sh_path.chmod(0o755)

            (root / ".env").write_text("DEVUP_T_VAR=from_root_dotenv\nSHELL_WINS=dotenv_lost\n", encoding="utf-8")
            (root / "Makefile").write_text("build:\n\techo built\n", encoding="utf-8")
            (root / "Justfile").write_text("check:\n\techo checked\n", encoding="utf-8")
            (root / "package.json").write_text(
                json.dumps({"private": True, "workspaces": ["packages/*"]}),
                encoding="utf-8",
            )
            (pkg_dir / "package.json").write_text(
                json.dumps({"name": "web", "scripts": {"dev": "vite"}}),
                encoding="utf-8",
            )
            (root / "devup.toml").write_text(
                """
[env]
GLOBAL_FLAG = "from_global_env"

[tasks.custom_env]
command = ""
env_file = ".env.task"

[tasks.custom_env.env]
TASK_ONLY = "task_wins"
""".strip(),
                encoding="utf-8",
            )
            (root / ".env.task").write_text("TASK_FILE_VAR=from_task_env_file\n", encoding="utf-8")
            # Override custom_env command cleanly via sys.executable
            py_env_probe = (
                f'"{sys.executable}" -c "import os; print('
                "'PROBE:' + os.environ.get('SHELL_WINS','') + '|' + "
                "os.environ.get('GLOBAL_FLAG','') + '|' + "
                "os.environ.get('TASK_FILE_VAR','') + '|' + "
                "os.environ.get('TASK_ONLY',''))\""
            )
            toml_text = (root / "devup.toml").read_text(encoding="utf-8").replace('command = ""', f"command = {json.dumps(py_env_probe)}")
            (root / "devup.toml").write_text(toml_text, encoding="utf-8")

            env_patch = {
                "PATH": os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")]),
                "SHELL_WINS": "shell_kept",
            }
            with mock.patch.dict(os.environ, env_patch):
                out = io.StringIO()
                rc = main(["-C", str(root), "--no-color", "--port-check=false" if False else "--no-port-check", "build", "check", "web:dev", "custom_env"], stream=out)

            self.assertEqual(rc, 0, out.getvalue())
            text = out.getvalue()
            self.assertIn("MAKE_EXEC:build:ENV=from_root_dotenv", text)
            self.assertIn("JUST_EXEC:check:ENV=from_root_dotenv", text)
            self.assertIn("NPM_EXEC:run dev:ENV=from_root_dotenv", text)
            self.assertIn("PROBE:shell_kept|from_global_env|from_task_env_file|task_wins", text)

    def test_interruption_cleans_grandchild_and_dependency_skips_downstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "grandchild.pid"
            spawner = root / "spawn_child.py"
            spawner.write_text(
                f"""
import subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
Path({json.dumps(str(pid_file))}).write_text(str(child.pid), encoding="utf-8")
print("SPAWNED", flush=True)
time.sleep(30)
""".strip(),
                encoding="utf-8",
            )
            cfg = WorkspaceConfig(
                root=root,
                tasks={
                    "sleeper": Task(name="sleeper", command=f'"{sys.executable}" "{spawner}"', source="devup.toml", cwd=root),
                    "fail_up": Task(name="fail_up", command=f'"{sys.executable}" -c "raise SystemExit(3)"', source="devup.toml", cwd=root),
                    "down": Task(name="down", command=f'"{sys.executable}" -c "print(\'SHOULD_NOT_RUN\')"', source="devup.toml", cwd=root, depends_on=["fail_up"]),
                },
            )
            # 1) Upstream failure skips downstream without running it
            out_fail = io.StringIO()
            res_fail = run_tasks(
                cfg,
                ["fail_up", "down"],
                options=RunOptions(color=False, port_check=False, fail_fast=False),
                stream=out_fail,
            )
            by_name = {r.name: r for r in res_fail}
            self.assertEqual(by_name["fail_up"].status, "failed")
            self.assertEqual(by_name["down"].status, "skipped")
            self.assertNotIn("SHOULD_NOT_RUN", out_fail.getvalue())

            # 2) Interruption via abort_event stops parent + grandchild cleanly
            abort = threading.Event()
            holder: dict[str, object] = {}

            def _trigger_abort() -> None:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and not pid_file.exists():
                    time.sleep(0.05)
                time.sleep(0.15)
                abort.set()

            t = threading.Thread(target=_trigger_abort, daemon=True)
            t.start()
            out_int = io.StringIO()
            res_int = run_tasks(
                cfg,
                ["sleeper"],
                options=RunOptions(color=False, port_check=False, shutdown_timeout=0.5),
                stream=out_int,
                stop_signal=abort,
            )
            t.join(timeout=3.0)
            self.assertEqual(res_int[0].status, "stopped")
            self.assertTrue(pid_file.exists())
            grand_pid = int(pid_file.read_text(encoding="utf-8").strip())
            # Give OS a short moment to reap terminated grandchild
            alive = True
            for _ in range(25):
                if os.name == "nt":
                    import ctypes

                    k32 = ctypes.windll.kernel32
                    handle = k32.OpenProcess(0x1000 | 0x00100000, False, grand_pid)
                    if not handle:
                        alive = False
                    else:
                        try:
                            code = ctypes.c_ulong()
                            alive = bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
                        finally:
                            k32.CloseHandle(handle)
                else:
                    try:
                        os.kill(grand_pid, 0)
                        alive = True
                    except OSError:
                        alive = False
                if not alive:
                    break
                time.sleep(0.1)
            self.assertFalse(alive, f"Grandchild PID {grand_pid} survived shutdown")

    @unittest.skipUnless(shutil.which("node") is not None, "Node.js is required for polyglot live example test")
    def test_polyglot_microservices_example_runs_live_end_to_end(self) -> None:
        for port in (18081, 18082):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    self.skipTest(f"Port {port} is busy")

        ex_dir = REPO_ROOT / "examples" / "polyglot-microservices"
        rep = validate_workspace(ex_dir)
        self.assertTrue(rep.valid, rep.errors)

        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / "polyglot-microservices"
            shutil.copytree(ex_dir, work)
            probe_cmd = (
                f'"{sys.executable}" -c "import urllib.request; '
                "print(urllib.request.urlopen('http://127.0.0.1:18082/status', timeout=5).read().decode()); "
                'raise SystemExit(1)"'
            )
            toml_text = (ex_dir / "devup.toml").read_text(encoding="utf-8")
            toml_text = toml_text.replace('command = "python server.py"', f"command = {json.dumps(f'\"{sys.executable}\" server.py')}")
            (work / "devup.toml").write_text(
                toml_text + f'\n[tasks.check]\ncommand = {json.dumps(probe_cmd)}\ndepends_on = ["gateway"]\n',
                encoding="utf-8",
            )
            out = io.StringIO()
            rc = main(["-C", str(work), "--no-color", "-t", "check"], stream=out)
            text = out.getvalue()
            self.assertEqual(rc, 1, text)
            self.assertIn("http://127.0.0.1:18081/health", text)
            self.assertIn("http://127.0.0.1:18082/status", text)
            self.assertIn('"gateway":"polyglot-gateway"', text)
            self.assertIn('"mode":"microservices"', text)
            self.assertIn('"greeting":"polyglot-ok"', text)
            self.assertIn('"service":"auth"', text)
