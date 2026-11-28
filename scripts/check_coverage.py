"""Zero-dependency Python line coverage runner using stdlib trace and dis."""

from __future__ import annotations

import argparse
import dis
import os
import sys
import threading
import trace
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
PKG_DIR = SRC_DIR / "devup"


def _executable_lines(py_file: Path) -> set[int]:
    source = py_file.read_text(encoding="utf-8")
    code = compile(source, str(py_file), "exec")
    lines: set[int] = set()

    def walk_code(obj: object) -> None:
        for _, lineno in dis.findlinestarts(obj):  # type: ignore[arg-type]
            if lineno is not None and lineno > 0:
                lines.add(lineno)
        for const in getattr(obj, "co_consts", ()):
            if hasattr(const, "co_code"):
                walk_code(const)

    walk_code(code)
    return lines


def measure_coverage(min_coverage: float = 70.0) -> int:
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    if str(ROOT / "tests") not in sys.path:
        sys.path.insert(0, str(ROOT / "tests"))

    tracer = trace.Trace(count=1, trace=0, ignoredirs=[sys.prefix, sys.exec_prefix])
    suite = tracer.runfunc(unittest.defaultTestLoader.discover, str(ROOT / "tests"))
    runner = unittest.TextTestRunner(verbosity=1)
    threading.settrace(tracer.globaltrace)
    try:
        result = tracer.runfunc(runner.run, suite)
    finally:
        threading.settrace(None)
    if not result.wasSuccessful():
        return 1

    counts = tracer.results().counts
    executed_by_file: dict[str, set[int]] = {}
    for (filename, lineno), hits in counts.items():
        if hits > 0:
            norm = os.path.normcase(os.path.abspath(filename))
            executed_by_file.setdefault(norm, set()).add(lineno)

    total_exec = 0
    total_hit = 0
    print("\n=== devup Python Line Coverage ===")
    for py_file in sorted(PKG_DIR.glob("*.py")):
        norm = os.path.normcase(str(py_file.resolve()))
        exec_lines = _executable_lines(py_file)
        hits = len(exec_lines & executed_by_file.get(norm, set()))
        total = len(exec_lines)
        total_exec += total
        total_hit += hits
        pct = (100.0 * hits / total) if total else 100.0
        print(f"  {py_file.name:<18} {hits:>4}/{total:<4} ({pct:5.1f}%)")

    overall = (100.0 * total_hit / total_exec) if total_exec else 100.0
    print(f"  TOTAL              {total_hit:>4}/{total_exec:<4} ({overall:5.1f}%)")

    if overall < min_coverage:
        print(f"ERROR: coverage {overall:.1f}% is below required minimum {min_coverage:.1f}%")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure and enforce Python unit test coverage.")
    parser.add_argument("--min-coverage", type=float, default=65.0, help="Minimum required line coverage percentage.")
    args = parser.parse_args()
    return measure_coverage(min_coverage=args.min_coverage)


if __name__ == "__main__":
    raise SystemExit(main())
