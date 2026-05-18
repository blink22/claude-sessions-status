"""Tiny env-file loader shared by dashboard.py and install.py.

Reads KEY=VALUE lines from a file and merges them into os.environ. Used
because parent processes (Claude Code, SwiftBar) sometimes pass through
empty string values for keys like ANTHROPIC_API_KEY, which would mask
the user's real key — so we treat empty values as 'unset' and let the
file populate them.

Quoting rules:
  - A single matching pair of surrounding quotes (single or double) is
    stripped. Mismatched quotes are left intact.
  - Blank lines and lines starting with '#' are ignored.
  - Lines without '=' are ignored.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: str | os.PathLike[str]) -> None:
    """Merge KEY=VALUE lines from `path` into os.environ in-place.

    Only sets a variable if it isn't already set to a truthy value, so
    the file never clobbers a real value the parent process intentionally
    exported. Silent on missing files / parse errors — this is best-effort
    config, not a hard dependency."""
    p = Path(path)
    if not p.exists():
        return
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and not os.environ.get(key):
            os.environ[key] = value
