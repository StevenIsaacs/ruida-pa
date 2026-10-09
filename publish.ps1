<#
.SYNOPSIS
    Build and optionally publish RPA to PyPI.
.PARAMETER Test
    Upload to TestPyPI instead of PyPI (appends a commit-derived dev version).
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

# Rewrite the [project] version line in pyproject.toml.
function Set-PyProjectVersion {
    param([string]$Version)
    $path = Join-Path $PWD "pyproject.toml"
    $text = [System.IO.File]::ReadAllText($path)
    $pattern = '(?m)^(\s*version\s*=\s*["''])[^"'']+(["'']\s*$)'
    $rx = [regex]::new($pattern)
    $patched = $rx.Replace($text, "`${1}$Version`${2}", 1)
    if ($patched -eq $text) {
        Write-Error "Could not locate the version line in pyproject.toml"
        exit 1
    }
    [System.IO.File]::WriteAllText($path, $patched)
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

$originalVersion = $version
$patched = $false
$readmeWritten = $false

try {
    if ($Test) {
        $git = Get-Command git -ErrorAction SilentlyContinue
        if (-not $git) { Write-Error "git not found; cannot derive commit ID"; exit 1 }
        $sha = & git rev-parse --short HEAD 2>$null
        if (-not $sha -or $LASTEXITCODE -ne 0) {
            Write-Error "Not in a git repository; cannot derive commit ID"
            exit 1
        }
        $devNum = [Convert]::ToString([Convert]::ToInt64($sha, 16))
        $version = "$version.dev$devNum"
        Set-PyProjectVersion $version
        $patched = $true
    }

    Write-Host "Building RPA v$version for PyPI..."
    Remove-Item -Recurse -Force "dist","build" -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force "*.egg-info" -ErrorAction SilentlyContinue
    python scripts/pypi_readme.py write
    $readmeWritten = ($LASTEXITCODE -eq 0)
    python -m build
    if ($readmeWritten) {
        python scripts/pypi_readme.py restore
        $readmeWritten = $false
    }
}
finally {
    if ($readmeWritten) {
        python scripts/pypi_readme.py restore
    }
    if ($patched) {
        Set-PyProjectVersion $originalVersion
    }
}

if ($NoUpload) {
    Write-Host "Build complete. Artifacts in dist/:"
    Get-ChildItem dist/*.whl,dist/*.tar.gz | Select-Object Name, Length
} else {
    $repo = if ($Test) { "testpypi" } else { "pypi" }
    Write-Host "Uploading to $repo..."
    python -m twine upload --repository $repo dist/*
    Write-Host "Published to $repo successfully!"
}
