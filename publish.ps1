<#
.SYNOPSIS
    Build and optionally publish RPA to PyPI.
.PARAMETER Test
    Upload to TestPyPI instead of PyPI.
.PARAMETER NoUpload
    Build but don't upload (dry run).
.EXAMPLE
    .\publish.ps1
    .\publish.ps1 -Test
#>

param(
    [switch]$Test,
    [switch]$NoUpload
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# Check requirements
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Write-Error "python not found"; exit 1 }

try { python -c "import build" 2>&1 | Out-Null }
catch { Write-Error "Install build: pip install build"; exit 1 }

if (-not $NoUpload) {
    try { python -c "import twine" 2>&1 | Out-Null }
    catch { Write-Error "Install twine: pip install twine"; exit 1 }
}

# Build
$version = & python -c @"
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open('pyproject.toml', 'rb') as f:
    data = tomllib.load(f)
print(data['project']['version'])
"@

Write-Host "Building RPA v$version for PyPI..."
Remove-Item -Recurse -Force "dist","build" -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "*.egg-info" -ErrorAction SilentlyContinue
python -m build

if ($NoUpload) {
    Write-Host "Build complete. Artifacts in dist/:"
    Get-ChildItem dist/*.whl,dist/*.tar.gz | Select-Object Name, Length
} else {
    $repo = if ($Test) { "testpypi" } else { "pypi" }
    Write-Host "Uploading to $repo..."
    python -m twine upload --repository $repo dist/*
    Write-Host "Published to $repo successfully!"
}
