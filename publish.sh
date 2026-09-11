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
  --test         Upload to TestPyPI instead of PyPI
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

echo "Building RPA v$_version for PyPI..."
rm -rf dist/ build/ *.egg-info
python -m build

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
