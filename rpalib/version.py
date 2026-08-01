# --- Version detection ---
# Prefer importlib.metadata (works when package is pip-installed or built with PyInstaller).
# Falls back to a dev version when running rpa.py directly from source.
try:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    try:
        __version__ = _pkg_version("ruida-pa")
    except PackageNotFoundError:
        __version__ = "0.13.0"
except ImportError:
    # Python < 3.8 fallback
    __version__ = "0.13.0"
