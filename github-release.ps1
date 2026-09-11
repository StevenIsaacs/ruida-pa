<#
.SYNOPSIS
    Create a GitHub release with RPA artifacts.
.PARAMETER Tag
    Git tag for the release (default: reads version from pyproject.toml).
.PARAMETER Draft
    Create as a draft release.
.EXAMPLE
    .\github-release.ps1
    .\github-release.ps1 -Tag v0.2.0 -Draft
#>

param(
    [string]$Tag,
    [switch]$Draft
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# Determine version
if (-not $Tag) {
    $version = & python -c @"
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open('pyproject.toml', 'rb') as f:
    data = tomllib.load(f)
print(data['project']['version'])
"@
    $Tag = "v$version"
}

# Check gh CLI
$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $gh) { Write-Error "gh CLI not found. Install from https://cli.github.com/"; exit 1 }

# Verify artifacts exist
$releaseDir = "release\rpa-$($Tag.TrimStart('v'))"
if (-not (Test-Path $releaseDir)) {
    Write-Error "$releaseDir not found. Run release.ps1 first."
    exit 1
}

Write-Host "Creating GitHub release $Tag..."

$notes = [System.IO.Path]::GetTempFileName()
@"
## RPA $($Tag.TrimStart('v'))

### Artifacts
"@ | Out-File -FilePath $notes -Encoding utf8

$ghArgs = @("release", "create", $Tag, "--title", "RPA $($Tag.TrimStart('v'))", "--notes-file", $notes)
if ($Draft) { $ghArgs += "--draft" }

# Add all artifacts
Get-ChildItem $releaseDir | ForEach-Object { $ghArgs += $_.FullName }

gh @ghArgs

Remove-Item $notes -Force
Write-Host "Release $Tag created successfully!"
