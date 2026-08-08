"""Small, atomic helpers for preserving user environment files."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path


def update_env_value(path: Path, key: str, value: str) -> None:
    """Replace or append one shell-style env assignment atomically.

    Existing keys, comments, blank lines, and unrelated secrets are preserved.
    The file mode is preserved when the target already exists.
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        raise ValueError(f"invalid environment key: {key!r}")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        original = path.read_text(encoding="utf-8")
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        original = ""
        mode = 0o600

    assignment = f"{key}={value}"
    pattern = re.compile(rf"^(\s*(?:export\s+)?{re.escape(key)}\s*=).*$")
    lines = original.splitlines(keepends=True)
    replaced = False
    updated: list[str] = []
    for line in lines:
        if not replaced and pattern.match(line.rstrip("\r\n")):
            ending = "\n" if line.endswith("\n") else ""
            updated.append(f"{assignment}{ending}")
            replaced = True
        else:
            updated.append(line)

    if not replaced:
        if updated and not updated[-1].endswith(("\n", "\r")):
            updated[-1] += "\n"
        updated.append(f"{assignment}\n")

    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp:
            temp.write("".join(updated))
            temp.flush()
            os.fsync(temp.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
