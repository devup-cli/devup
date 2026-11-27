"""Terminal color and output formatting helpers."""

from __future__ import annotations

import os
import sys
from typing import TextIO

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"

RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
BLUE = "\x1b[34m"
MAGENTA = "\x1b[35m"
CYAN = "\x1b[36m"

SERVICE_PALETTE = (
    "\x1b[36m",  # cyan
    "\x1b[33m",  # yellow
    "\x1b[35m",  # magenta
    "\x1b[32m",  # green
    "\x1b[34m",  # blue
    "\x1b[96m",  # bright cyan
    "\x1b[93m",  # bright yellow
    "\x1b[95m",  # bright magenta
    "\x1b[92m",  # bright green
    "\x1b[94m",  # bright blue
)


def enable_windows_ansi() -> None:
    """Enable virtual terminal processing on Windows consoles when available."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        for std_handle_id in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(std_handle_id)
            if handle in (0, -1):
                continue
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        # Non-console handles or restricted environments fall back cleanly.
        pass


def supports_color(stream: TextIO | None = None, force: bool | None = None) -> bool:
    """Determine whether ANSI colors should be emitted for the given stream."""
    if force is not None:
        return force
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR") not in (None, "", "0"):
        return True
    target = stream or sys.stdout
    try:
        return bool(target.isatty())
    except Exception:
        return False


def style(text: str, *codes: str, enabled: bool = True) -> str:
    """Wrap text with ANSI SGR codes when color output is enabled."""
    if not enabled or not codes:
        return text
    return f"{''.join(codes)}{text}{RESET}"


def assign_service_colors(names: list[str], enabled: bool = True) -> dict[str, str]:
    """Assign distinct palette colors deterministically to service names."""
    if not enabled:
        return {name: "" for name in names}
    return {
        name: SERVICE_PALETTE[idx % len(SERVICE_PALETTE)]
        for idx, name in enumerate(names)
    }
