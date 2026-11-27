# Standalone Executables Evaluation

Because `devup-cli` uses **zero third-party runtime dependencies**, we evaluated four ways to distribute standalone executables for users who don't want to manage a Python virtual environment:

| Approach | Output Size | Startup Speed | Python Required on Host? | Tradeoffs |
| :--- | :--- | :--- | :--- | :--- |
| **`zipapp` (`devup.pyz`)** | ~45 KB | Instant (~25 ms) | Yes (Python 3.11+) | Zero external build tools; single portable file attached to releases and usable by Scoop. |
| **PyInstaller** | ~12–20 MB | ~300–600 ms (onefile unpack) | No | Bundles full CPython runtime; onefile builds frequently trigger Windows SmartScreen/AV false positives without code-signing certificates. |
| **Nuitka** | ~10–16 MB | Fast (~40 ms) | No | Compiles Python to C; slower CI build times (needs C compiler per OS) and requires code signing on macOS/Windows to avoid OS Gatekeeper/SmartScreen warnings. |
| **`uvx` / `pipx`** | Managed | Instant (cached) | Managed automatically by `uv` | `uv` installs Python automatically if missing and isolates the CLI cleanly. |

## Recommendation

1. **Primary zero-Python path today:** Recommend `uvx --from devup-cli devup` (since `uv` downloads and caches Python automatically on machines without Python).
2. **Lightweight single-file artifact:** Build and attach `devup.pyz` via `python scripts/build_zipapp.py` to every GitHub Release for Scoop and air-gapped environments that already have Python 3.11+.
3. **Future native binaries:** If demand grows for signed native executables without `uv`, add a Nuitka release matrix job once code-signing certificates are available for Windows and macOS.
