"""Profile-driven Hermes tool whitelist and instructions.

A profile is a directory with two optional files:

* ``tools.txt``      — one tool name per line; ``#`` starts a comment. Empty
                       or missing means "all tools allowed". Negative lines
                       (``-weather``) ban a specific tool from the parent
                       profile's whitelist.
* ``instructions.txt`` — plain text appended to the system prompt. Missing
                         means no override.

Profiles ship with YRobot in ``yrobot/profiles/`` and can be overridden at
runtime via the ``YROBOT_PROFILE`` environment variable and the optional
``YROBOT_PROFILE_DIR`` (user-level override directory).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

PACKAGE_PROFILES_DIR = Path(__file__).parent / "profiles"
DEFAULT_PROFILE_NAME = "default"


@dataclass(frozen=True)
class Profile:
    name: str
    allowed_tools: frozenset[str] | None  # None ⇒ all tools allowed
    instructions: str = ""
    path: Path | None = None

    def allows(self, tool_name: str) -> bool:
        if self.allowed_tools is None:
            return True
        return tool_name in self.allowed_tools


def _read_tools_file(path: Path) -> set[str] | None:
    """Return None if file is empty (allow-all), else the allow set."""
    if not path.exists():
        return None
    out: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line)
    return out or None


def _read_instructions_file(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _resolve_dir(profile_name: str, override_dir: str | os.PathLike[str] | None) -> Path:
    """User-level override dir takes precedence over package-shipped profiles."""
    if override_dir is not None:
        candidate = Path(override_dir) / profile_name
        if candidate.is_dir():
            return candidate
    candidate = PACKAGE_PROFILES_DIR / profile_name
    if candidate.is_dir():
        return candidate
    # Fallback: user override dir may not exist yet, but the override path
    # still wins for predictable errors downstream.
    return (
        Path(override_dir) / profile_name
        if override_dir is not None
        else PACKAGE_PROFILES_DIR / profile_name
    )


def load_profile(
    name: str = DEFAULT_PROFILE_NAME,
    *,
    override_dir: str | os.PathLike[str] | None = None,
) -> Profile:
    """Load a profile by name.

    Resolution order:
      1. ``override_dir / <name>`` if it is a directory
      2. ``yrobot/profiles/<name>`` (shipped)
      3. Missing — raise ``FileNotFoundError`` with a helpful message.
    """
    profile_dir = _resolve_dir(name, override_dir)
    if not profile_dir.is_dir():
        raise FileNotFoundError(
            f"Profile {name!r} not found. Looked in: {profile_dir}. "
            f"Create the directory with a tools.txt (optional) and "
            f"instructions.txt (optional), or use one of: "
            f"{', '.join(list_profiles(override_dir))}"
        )
    allowed = _read_tools_file(profile_dir / "tools.txt")
    instructions = _read_instructions_file(profile_dir / "instructions.txt")
    return Profile(
        name=name,
        allowed_tools=frozenset(allowed) if allowed is not None else None,
        instructions=instructions,
        path=profile_dir,
    )


def list_profiles(override_dir: str | os.PathLike[str] | None = None) -> list[str]:
    """Return available profile names, user overrides first then shipped."""
    names: set[str] = set()
    if override_dir is not None:
        od = Path(override_dir)
        if od.is_dir():
            for entry in sorted(od.iterdir()):
                if entry.is_dir():
                    names.add(entry.name)
    if PACKAGE_PROFILES_DIR.is_dir():
        for entry in sorted(PACKAGE_PROFILES_DIR.iterdir()):
            if entry.is_dir():
                names.add(entry.name)
    return sorted(names)