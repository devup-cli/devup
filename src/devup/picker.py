"""Interactive arrow-key multi-select picker with numbered fallback."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import TextIO

from devup.colors import BOLD, CYAN, DIM, GREEN, style


@dataclass(slots=True)
class PickChoice:
    """Selectable task or group entry for interactive picking."""

    name: str
    detail: str


def _read_key_windows() -> str:
    import msvcrt

    ch = msvcrt.getwch()  # type: ignore[attr-defined]
    if ch in ("\x00", "\xe0"):
        ext = msvcrt.getwch()  # type: ignore[attr-defined]
        return {"H": "UP", "P": "DOWN"}.get(ext, "")
    if ch in ("\r", "\n"):
        return "ENTER"
    if ch == " ":
        return "SPACE"
    if ch in ("\x03", "\x1b", "q", "Q"):
        return "CANCEL"
    if ch == "k":
        return "UP"
    if ch == "j":
        return "DOWN"
    return ""


def _read_key_posix(fd: int) -> str:
    ch = os.read(fd, 1)
    if not ch:
        return "CANCEL"
    if ch in (b"\r", b"\n"):
        return "ENTER"
    if ch == b" ":
        return "SPACE"
    if ch in (b"\x03", b"q", b"Q"):
        return "CANCEL"
    if ch == b"k":
        return "UP"
    if ch == b"j":
        return "DOWN"
    if ch == b"\x1b":
        # Peek for ANSI escape sequence `[A` / `[B`.
        import select

        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready:
            return "CANCEL"
        seq = os.read(fd, 2)
        if seq == b"[A":
            return "UP"
        if seq == b"[B":
            return "DOWN"
    return ""


def _render_menu(
    choices: list[PickChoice],
    cursor: int,
    selected: set[int],
    out: TextIO,
    use_color: bool,
    redraw_lines: int = 0,
) -> int:
    if redraw_lines > 0:
        out.write(f"\x1b[{redraw_lines}A\r")

    header = style(
        "Select task(s) or group(s) (Up/Down move, Space toggle, Enter run, q cancel):",
        BOLD,
        CYAN,
        enabled=use_color,
    )
    lines = [header]
    for idx, choice in enumerate(choices):
        pointer = style(">", BOLD, CYAN, enabled=use_color) if idx == cursor else " "
        mark = "[x]" if idx in selected else "[ ]"
        mark_styled = style(mark, GREEN, BOLD, enabled=use_color) if idx in selected else mark
        label = style(choice.name, BOLD if idx == cursor else "", enabled=use_color)
        detail = style(choice.detail, DIM, enabled=use_color)
        lines.append(f" {pointer} {mark_styled} {label}  {detail}")

    for line in lines:
        out.write(f"\x1b[2K{line}\n")
    out.flush()
    return len(lines)


def _interactive_arrow_pick(
    choices: list[PickChoice],
    out: TextIO,
    use_color: bool,
    stdin: TextIO,
) -> list[str]:
    cursor = 0
    selected: set[int] = set()
    rendered = _render_menu(choices, cursor, selected, out, use_color, redraw_lines=0)

    if os.name == "nt":
        while True:
            key = _read_key_windows()
            if key == "UP":
                cursor = (cursor - 1) % len(choices)
            elif key == "DOWN":
                cursor = (cursor + 1) % len(choices)
            elif key == "SPACE":
                if cursor in selected:
                    selected.remove(cursor)
                else:
                    selected.add(cursor)
            elif key == "ENTER":
                chosen_indices = sorted(selected) if selected else [cursor]
                return [choices[i].name for i in chosen_indices]
            elif key == "CANCEL":
                return []
            else:
                continue
            rendered = _render_menu(choices, cursor, selected, out, use_color, redraw_lines=rendered)

    import termios
    import tty

    fd = stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            key = _read_key_posix(fd)
            if key == "UP":
                cursor = (cursor - 1) % len(choices)
            elif key == "DOWN":
                cursor = (cursor + 1) % len(choices)
            elif key == "SPACE":
                if cursor in selected:
                    selected.remove(cursor)
                else:
                    selected.add(cursor)
            elif key == "ENTER":
                chosen_indices = sorted(selected) if selected else [cursor]
                return [choices[i].name for i in chosen_indices]
            elif key == "CANCEL":
                return []
            else:
                continue
            rendered = _render_menu(choices, cursor, selected, out, use_color, redraw_lines=rendered)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _numbered_pick(
    choices: list[PickChoice],
    out: TextIO,
    use_color: bool,
    stdin: TextIO,
) -> list[str]:
    out.write(style("Select task(s) or group(s) to run:\n", BOLD, CYAN, enabled=use_color))
    for idx, choice in enumerate(choices, start=1):
        out.write(
            f"  {style(f'{idx:>2}.', BOLD, enabled=use_color)} "
            f"{style(choice.name, GREEN, enabled=use_color)} "
            f"{style(choice.detail, DIM, enabled=use_color)}\n"
        )
    out.write("Enter numbers or names separated by spaces/commas (empty to cancel): ")
    out.flush()

    try:
        raw = stdin.readline()
    except KeyboardInterrupt:
        out.write("\n")
        return []

    tokens = [tok.strip() for tok in raw.replace(",", " ").split() if tok.strip()]
    picked: list[str] = []
    for tok in tokens:
        if tok.isdigit():
            idx = int(tok)
            if 1 <= idx <= len(choices):
                picked.append(choices[idx - 1].name)
            else:
                raise ValueError(f"Selection out of range: {tok}")
        else:
            picked.append(tok)
    return picked


def pick_targets(
    choices: list[PickChoice],
    out: TextIO,
    use_color: bool,
    stdin: TextIO | None = None,
) -> list[str]:
    """Run an arrow-key multi-select menu in a TTY, falling back to numbered input."""
    if not choices:
        return []
    in_stream = stdin or sys.stdin
    try:
        is_tty = bool(in_stream.isatty() and out.isatty())
    except Exception:
        is_tty = False

    if is_tty:
        try:
            return _interactive_arrow_pick(choices, out, use_color, in_stream)
        except Exception:
            pass
    return _numbered_pick(choices, out, use_color, in_stream)
