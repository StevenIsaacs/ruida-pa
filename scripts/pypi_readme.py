#!/usr/bin/env python3
"""Rewrite README.md repo-relative links to absolute GitHub URLs for PyPI.

PyPI renders the long_description from ``README.md`` (``pyproject.toml``
``readme = "README.md"``) but cannot resolve repo-relative links, so they
must be absolutized at package-build time. The working copy is backed up
before the rewrite and restored afterwards.

Usage:
    python scripts/pypi_readme.py write    # back up + rewrite README.md
    python scripts/pypi_readme.py restore  # restore README.md from backup
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
BACKUP = REPO_ROOT / ".README.md.pypi-backup"
PYPROJECT = REPO_ROOT / "pyproject.toml"

DEFAULT_REPO_URL = "https://github.com/StevenIsaacs/ruida-pa"
BRANCH = "main"

_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")
_LINK_RE = re.compile(r"(!?)\[([^\]]*)\]\(([^)\s]+)\)")
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


def _repo_url() -> str:
    """Return the project Homepage URL from pyproject.toml, or the default."""
    try:
        text = PYPROJECT.read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_REPO_URL
    match = re.search(r'^\s*Homepage\s*=\s*["\']([^"\']+)["\']', text, re.M)
    return match.group(1).rstrip("/") if match else DEFAULT_REPO_URL


def _to_absolute(target: str, is_image: bool, repo_url: str) -> str:
    """Absolutize one link/image target, leaving anchors and URLs untouched."""
    if target.startswith("#") or target.startswith("/") or _SCHEME_RE.match(target):
        return target
    if is_image or target.lower().endswith(_IMAGE_EXTENSIONS):
        slug = repo_url.removeprefix("https://github.com/").removeprefix("https://www.github.com/")
        return f"https://raw.githubusercontent.com/{slug}/{BRANCH}/{target}"
    return f"{repo_url}/blob/{BRANCH}/{target}"


def _rewrite(text: str, repo_url: str) -> str:
    """Rewrite inline links/images outside fenced code blocks."""
    out = []
    in_fence = False
    fence = ""
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if not in_fence and (stripped.startswith("```") or stripped.startswith("~~~")):
            in_fence = True
            fence = stripped[:3]
            out.append(line)
            continue
        if in_fence:
            if stripped.startswith(fence):
                in_fence = False
            out.append(line)
            continue
        out.append(
            _LINK_RE.sub(
                lambda m: (
                    f"{m.group(1)}[{m.group(2)}]"
                    f"({_to_absolute(m.group(3), m.group(1) == '!', repo_url)})"
                ),
                line,
            )
        )
    return "".join(out)


def write() -> None:
    """Back up README.md and rewrite its relative links to absolute URLs."""
    if BACKUP.exists():
        sys.exit(f"Error: backup already exists: {BACKUP}. Run 'restore' first.")
    original = README.read_text(encoding="utf-8")
    BACKUP.write_text(original, encoding="utf-8")
    README.write_text(_rewrite(original, _repo_url()), encoding="utf-8")


def restore() -> None:
    """Restore README.md from the backup, removing the backup. Idempotent."""
    if BACKUP.exists():
        BACKUP.replace(README)


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ("write", "restore"):
        sys.exit(__doc__)
    {"write": write, "restore": restore}[sys.argv[1]]()


if __name__ == "__main__":
    main()
