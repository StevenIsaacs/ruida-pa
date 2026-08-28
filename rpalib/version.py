"""Package version detection.

Version resolution priority (first match wins):

1. Source-tree ``pyproject.toml`` [project] table — authoritative when
   the package runs directly from the checkout. A stale installed dist
   (e.g. ``ruida-pa==0.12.0``) must never shadow the in-tree version.
2. ``importlib.metadata`` for the ``ruida-pa`` distribution — correct
   for pip-installed layouts and PyInstaller bundles, where no
   ``pyproject.toml`` sits next to the package and the build copies the
   metadata in (see ``rpa.spec`` copy_metadata).
3. Hardcoded fallback — last resort when neither the source tree nor
   installed metadata is available.
"""

import re
from pathlib import Path

# --- Version detection ---


def _version_from_pyproject():
    """Return the [project] version from the source-tree pyproject.toml.

    Returns None when the file is missing, unreadable, or when the
    [project] table does not declare both name = "ruida-pa" and a
    version (the name guard keeps vendored copies from being used).
    """
    pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not pyproject_path.exists():
        return None

    try:
        lines = pyproject_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None

    in_project = False
    name_found = False
    version = None
    for line in lines:
        if in_project and re.match(r"^\[", line):
            break  # Left the [project] table.
        if not in_project:
            if re.match(r"^\[project\]\s*(#.*)?$", line):
                in_project = True
            continue
        if re.match(r'^\s*name\s*=\s*["\']ruida-pa["\']', line):
            name_found = True
        version_match = re.match(r'^\s*version\s*=\s*["\']([^"\']+)["\']', line)
        if version_match:
            version = version_match.group(1)

    if name_found and version:
        return version
    return None


def _version_from_metadata():
    """Return the version importlib.metadata reports for ruida-pa, or None."""
    try:
        from importlib.metadata import PackageNotFoundError, version as _pkg_version

        try:
            return _pkg_version("ruida-pa")
        except PackageNotFoundError:
            return None
    except ImportError:
        # Python < 3.8 ships no importlib.metadata module.
        return None


# Keep in sync with pyproject.toml [project] version
__version__ = _version_from_pyproject() or _version_from_metadata() or "0.18.1"
