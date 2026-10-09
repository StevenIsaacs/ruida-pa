#!/usr/bin/env bash
set -euo pipefail

_self=$(basename "$0")
_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$_script_dir"

usage () {
  cat <<EOF
Usage: $_self [--test] [--no-upload]

Build and optionally publish RPA to PyPI.

Options:
  --test         Upload to TestPyPI instead of PyPI (appends a commit-derived dev version)
  --no-upload    Build but don't upload (dry run)
  -h, --help     Show this help
EOF
  exit 1
}

_test=false
_no_upload=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --test) _test=true; shift ;;
    --no-upload) _no_upload=true; shift ;;
    -h|--help) usage ;;
    *) echo "Unknown option: $1"; usage ;;
  esac
done

# Check requirements
command -v python >/dev/null 2>&1 || { echo "Error: python not found"; exit 1; }
python -c "import build" 2>/dev/null || { echo "Install build: pip install build"; exit 1; }

if [ "$_no_upload" = false ]; then
  python -c "import twine" 2>/dev/null || { echo "Install twine: pip install twine"; exit 1; }
fi

# Rewrite the [project] version line in pyproject.toml.
_set_version () {
  python - "$1" <<'EOF'
import re
import sys

version = sys.argv[1]
path = "pyproject.toml"
with open(path, encoding="utf-8") as f:
    text = f.read()
pattern = r'^(\s*version\s*=\s*[\x22\x27])[^\x22\x27]+([\x22\x27])\s*$'
patched, count = re.subn(
    pattern,
    lambda m: m.group(1) + version + m.group(2),
    text,
    count=1,
    flags=re.M,
)
if count == 0:
    sys.exit("Error: could not locate the version line in pyproject.toml")
with open(path, "w", encoding="utf-8") as f:
    f.write(patched)
EOF
}

# Build
_version=$(python -c "
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open('pyproject.toml', 'rb') as f:
    data = tomllib.load(f)
print(data['project']['version'])
")

_orig_version="$_version"
_patched=false
_restore_version () {
  if [ "$_patched" = true ]; then
    _set_version "$_orig_version"
    _patched=false
  fi
}

# Absolutize README links for PyPI, restoring the working copy afterwards.
_readme_written=false
_restore_readme () {
  if [ "$_readme_written" = true ]; then
    python scripts/pypi_readme.py restore || true
    _readme_written=false
  fi
}
trap '_restore_readme; _restore_version' EXIT

if [ "$_test" = true ]; then
  _sha=$(git rev-parse --short HEAD 2>/dev/null) || { echo "Error: not in a git repository (cannot derive commit ID)"; exit 1; }
  _dev_num=$(python -c "print(int('$_sha', 16))")
  _version="$_version.dev$_dev_num"
  _set_version "$_version"
  _patched=true
fi

echo "Building RPA v$_version for PyPI..."
rm -rf dist/ build/ *.egg-info
python scripts/pypi_readme.py write
_readme_written=true
python -m build
_restore_readme

_restore_version

if [ "$_no_upload" = true ]; then
  echo "Build complete. Artifacts in dist/:"
  ls -lh dist/
else
  _repo="pypi"
  [ "$_test" = true ] && _repo="testpypi"
  echo "Uploading to $_repo..."
  python -m twine upload --repository "$_repo" dist/*
  echo "Published to $_repo successfully!"
fi
