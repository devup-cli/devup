"""Automate version updates across pyproject.toml, __init__.py, packaging, and CHANGELOG.md."""

from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9_.-]+)?$")


def bump_version(new_version: str, root: Path = ROOT) -> list[Path]:
    if not _SEMVER_RE.match(new_version):
        raise ValueError(f"Invalid semantic version: '{new_version}'")

    updated: list[Path] = []

    pyproject = root / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    new_text = re.sub(
        r'(?m)^version\s*=\s*"[^"]+"',
        f'version = "{new_version}"',
        text,
        count=1,
    )
    if new_text != text:
        pyproject.write_text(new_text, encoding="utf-8")
        updated.append(pyproject)

    init_py = root / "src" / "devup" / "__init__.py"
    init_text = init_py.read_text(encoding="utf-8")
    new_init = re.sub(
        r'(?m)^__version__\s*=\s*"[^"]+"',
        f'__version__ = "{new_version}"',
        init_text,
        count=1,
    )
    if new_init != init_text:
        init_py.write_text(new_init, encoding="utf-8")
        updated.append(init_py)

    scoop = root / "packaging" / "scoop" / "devup.json"
    if scoop.is_file():
        data = json.loads(scoop.read_text(encoding="utf-8"))
        data["version"] = new_version
        data["url"] = (
            f"https://github.com/devup-cli/devup/releases/download/v{new_version}/devup.pyz"
        )
        scoop.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        updated.append(scoop)

    formula = root / "packaging" / "homebrew" / "devup-cli.rb"
    if formula.is_file():
        rb_text = formula.read_text(encoding="utf-8")
        new_rb = re.sub(
            r"devup_cli-[0-9A-Za-z_.-]+\.tar\.gz",
            f"devup_cli-{new_version}.tar.gz",
            rb_text,
        )
        if new_rb != rb_text:
            formula.write_text(new_rb, encoding="utf-8")
            updated.append(formula)

    changelog = root / "CHANGELOG.md"
    if changelog.is_file():
        cl_text = changelog.read_text(encoding="utf-8")
        header = f"## [{new_version}]"
        if header not in cl_text:
            today = date.today().isoformat()
            cl_text = cl_text.replace(
                "## [",
                f"## [{new_version}] - {today}\n\n### Added\n- Release {new_version}.\n\n## [",
                1,
            )
            changelog.write_text(cl_text, encoding="utf-8")
            updated.append(changelog)

    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description="Bump devup-cli version across project files.")
    parser.add_argument("version", help="New semantic version (e.g. 0.3.0)")
    args = parser.parse_args()
    changed = bump_version(args.version)
    for path in changed:
        print(f"Updated {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
