"""Cross-runtime parity, dual-install conflict, and live example service tests."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from devup.cli import main as py_main
from devup.diagnostics import run_doctor

REPO_ROOT = Path(__file__).resolve().parents[1]
NPM_BIN = REPO_ROOT / "packages" / "npm" / "bin" / "devup.js"
HAS_NODE = shutil.which("node") is not None


def _run_with_timeout(cmd: list[str], cwd: Path | None, env: dict[str, str] | None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd or REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        raise AssertionError(f"Command {cmd} timed out after 25s.\nSTDOUT:\n{out}\nSTDERR:\n{err}") from exc


def _run_node_cli(args: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return _run_with_timeout(["node", str(NPM_BIN), *args], cwd, env)


def _run_py_cli(args: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged_env = dict(os.environ if env is None else env)
    merged_env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return _run_with_timeout([sys.executable, "-m", "devup", *args], cwd, merged_env)


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


@unittest.skipUnless(HAS_NODE, "Node.js is required for parity tests")
class NpmParityAndConflictsTests(unittest.TestCase):
    def test_list_and_validate_json_parity_across_all_examples(self) -> None:
        examples_dir = REPO_ROOT / "examples"
        project_dirs = [
            d for d in sorted(examples_dir.iterdir()) if d.is_dir() and d.name != "templates"
        ]
        self.assertEqual(len(project_dirs), 7)

        for proj_dir in project_dirs:
            with self.subTest(example=proj_dir.name):
                py_list = _run_py_cli(["-C", str(proj_dir), "list", "--json"])
                node_list = _run_node_cli(["-C", str(proj_dir), "list", "--json"])
                self.assertEqual(py_list.returncode, node_list.returncode)
                py_data = json.loads(py_list.stdout)
                node_data = json.loads(node_list.stdout)
                self.assertEqual(py_data["default"], node_data["default"])
                self.assertEqual(py_data["groups"], node_data["groups"])
                self.assertEqual(set(py_data["tasks"].keys()), set(node_data["tasks"].keys()))
                for tname in py_data["tasks"]:
                    self.assertEqual(py_data["tasks"][tname]["depends_on"], node_data["tasks"][tname]["depends_on"])
                    self.assertEqual(py_data["tasks"][tname]["ready_url"], node_data["tasks"][tname]["ready_url"])
                    self.assertEqual(py_data["tasks"][tname]["ready_tcp"], node_data["tasks"][tname]["ready_tcp"])

                py_val = _run_py_cli(["-C", str(proj_dir), "validate", "--json"])
                node_val = _run_node_cli(["-C", str(proj_dir), "validate", "--json"])
                self.assertEqual(py_val.returncode, node_val.returncode)
                self.assertEqual(json.loads(py_val.stdout), json.loads(node_val.stdout))

    def test_init_dry_run_dotenv_and_exit_code_parity(self) -> None:
        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            d1, d2 = Path(td1), Path(td2)
            for label, runner_fn, d in (("py", _run_py_cli, d1), ("node", _run_node_cli, d2)):
                r_init = runner_fn(["-C", str(d), "init", "--template", "python-node", "--yes"])
                self.assertEqual(r_init.returncode, 0, label + ": " + r_init.stdout + r_init.stderr)

            self.assertEqual((d1 / "devup.toml").read_text(), (d2 / "devup.toml").read_text())

            for name in ("fastapi-react", "node-monorepo", "maven-multimodule", "gradle-multimodule"):
                ex = REPO_ROOT / "examples" / name
                r_py = _run_py_cli(["-C", str(ex), "--no-color", "--dry-run"])
                r_nd = _run_node_cli(["-C", str(ex), "--no-color", "--dry-run"])
                self.assertEqual(r_py.returncode, r_nd.returncode)
                self.assertEqual(r_py.stdout, r_nd.stdout)

            # Unknown task returns exit code 2 in both
            bad_py = _run_py_cli(["-C", str(d1), "nonexistent"])
            bad_nd = _run_node_cli(["-C", str(d2), "nonexistent"])
            self.assertEqual(bad_py.returncode, 2)
            self.assertEqual(bad_nd.returncode, 2)

    def test_fastapi_react_backend_real_http_service_with_node_and_python(self) -> None:
        if not _port_free(8000):
            self.skipTest("Port 8000 is already occupied")

        ex_dir = REPO_ROOT / "examples" / "fastapi-react"
        for label, runner_fn in (("python", _run_py_cli), ("node", _run_node_cli)):
            with tempfile.TemporaryDirectory() as td:
                work = Path(td) / "fastapi-react"
                shutil.copytree(ex_dir, work)
                probe_cmd = (
                    f'"{sys.executable}" -c "import urllib.request; '
                    "print(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).read().decode()); "
                    'raise SystemExit(1)"'
                )
                base_toml = (ex_dir / "devup.toml").read_text(encoding="utf-8")
                # Use sys.executable reliably across OSes and disable watch in this one-shot parity test
                # so any readiness timeout exits promptly instead of spinning in watch mode.
                api_cmd = f'"{sys.executable}" main.py'
                base_toml = base_toml.replace('command = "python main.py"', f"command = {json.dumps(api_cmd)}")
                base_toml = base_toml.replace('watch = ["*.py"]', "watch = false\nready_timeout = 10")
                (work / "devup.toml").write_text(
                    base_toml
                    + f'\n[tasks.check]\ncommand = {json.dumps(probe_cmd)}\ndepends_on = ["api"]\n',
                    encoding="utf-8",
                )
                res = runner_fn(["-C", str(work), "--no-color", "check"])
                self.assertEqual(res.returncode, 1, f"{label}: {res.stdout}\n{res.stderr}")
                self.assertIn("ready (http://127.0.0.1:8000/health)", res.stdout)
                self.assertIn('{"status": "ok"}', res.stdout)
                self.assertTrue(_port_free(8000), f"{label} left port 8000 bound after shutdown")

    def test_dual_install_conflict_detection_in_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            dir_a = root / "bin_a"
            dir_b = root / "bin_b"
            dir_a.mkdir()
            dir_b.mkdir()
            bin_name = "devup.cmd" if os.name == "nt" else "devup"
            for d in (dir_a, dir_b):
                p = d / bin_name
                p.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
                if os.name != "nt":
                    p.chmod(0o755)

            env = dict(os.environ)
            env["PATH"] = os.pathsep.join([str(dir_a), str(dir_b), env.get("PATH", "")])

            orig_path = os.environ.get("PATH", "")
            try:
                os.environ["PATH"] = env["PATH"]
                py_report = run_doctor(REPO_ROOT, online=False)
                self.assertEqual(py_report["runtime"], "python")
                self.assertGreaterEqual(len(py_report["installations"]), 2)
                self.assertIsNotNone(py_report["conflict_warning"])
            finally:
                os.environ["PATH"] = orig_path

            nd_doc = _run_node_cli(["-C", str(REPO_ROOT), "doctor", "--json"], env=env)
            self.assertEqual(nd_doc.returncode, 0, nd_doc.stderr)
            nd_report = json.loads(nd_doc.stdout)
            self.assertEqual(nd_report["runtime"], "node")
            self.assertGreaterEqual(len(nd_report["installations"]), 2)
            self.assertIsNotNone(nd_report["conflict_warning"])
