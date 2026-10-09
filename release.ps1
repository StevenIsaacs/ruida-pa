<#
.SYNOPSIS
    Build a versioned release of RPA in release/
.DESCRIPTION
    Creates a release/ directory with pip wheel + PyInstaller binary + SHA256 checksums.
.PARAMETER DistOnly
    Only build PyInstaller binary (skip wheel).
.PARAMETER WheelOnly
    Only build pip wheel (skip PyInstaller).
.EXAMPLE
    .\release.ps1
    .\release.ps1 -WheelOnly
#>

param(
    [switch]$DistOnly,
    [switch]$WheelOnly
)

if ($DistOnly -and $WheelOnly) {
    Write-Error "--dist-only and --wheel-only are mutually exclusive."
    exit 1
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# --- Determine version ---
$version = & python -c @"
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open('pyproject.toml', 'rb') as f:
    data = tomllib.load(f)
print(data['project']['version'])
"@

Write-Host "Building RPA release v$version"
Write-Host ""

$releaseDir = "release\rpa-$version"
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null

# --- Build pip wheel ---
if (-not $DistOnly) {
    Write-Host "--- Building pip wheel ---"
    python scripts/pypi_readme.py write
    $readmeWritten = ($LASTEXITCODE -eq 0)
    try {
        pip wheel --no-deps -w $releaseDir .
    }
    finally {
        if ($readmeWritten) {
            python scripts/pypi_readme.py restore
            $readmeWritten = $false
        }
    }
    Write-Host "Wheel built."
    Write-Host ""
}

# --- Build PyInstaller binary ---
if (-not $WheelOnly) {
    Write-Host "--- Building standalone binary ---"
    $pyinst = Get-Command pyinstaller -ErrorAction SilentlyContinue
    if (-not $pyinst) {
        Write-Host "Installing PyInstaller..."
        pip install pyinstaller
    }
    pyinstaller --clean --onefile rpa.spec
    Copy-Item "dist\rpa.exe" "$releaseDir\rpa-v$version.exe"
    Write-Host "Binary built."
    Write-Host ""
}

# --- Generate checksums ---
Write-Host "--- Generating checksums ---"
Get-ChildItem $releaseDir -File | ForEach-Object {
    $hash = Get-FileHash $_.FullName -Algorithm SHA256
    "$($hash.Hash.ToLower())  $($_.Name)"
} | Out-File -FilePath "$releaseDir\SHA256SUMS" -Encoding ascii
Write-Host ""

# --- Summary ---
Write-Host "============================================"
Write-Host "Release v$version complete!"
Write-Host "Artifacts in: $releaseDir"
Get-ChildItem $releaseDir | Select-Object Name, Length
Write-Host "============================================"
