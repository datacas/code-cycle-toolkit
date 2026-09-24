# One-command installer for Windows: download a Code Cycle Toolkit release and
# install its skills and runtime, without cloning the repository first.
#
#   irm https://raw.githubusercontent.com/datacas/code-cycle-toolkit/main/scripts/get.ps1 | iex
#
# With options:
#
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/datacas/code-cycle-toolkit/main/scripts/get.ps1))) -Version v0.3.0 -Agent claude
#
# -Version latest|main|vX.Y.Z picks what to download (default: latest release,
# or $env:CODE_CYCLE_VERSION). The other parameters are passed to
# scripts\install.ps1. -Force is always passed, so running this again updates
# an installation.

param(
    [string]$Version = $(if ($env:CODE_CYCLE_VERSION) { $env:CODE_CYCLE_VERSION } else { 'latest' }),
    [ValidateSet('all', 'claude', 'codex', 'opencode')]
    [string]$Agent = 'all',
    [ValidateSet('global', 'project')]
    [string]$Scope = 'global',
    [string]$ProjectDir = (Get-Location).Path,
    [switch]$NoRuntime
)

$ErrorActionPreference = 'Stop'
$Repo = 'datacas/code-cycle-toolkit'

switch ($Version) {
    'latest' {
        $Ref = (Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -Headers @{ 'User-Agent' = 'code-cycle-toolkit' }).tag_name
        $Archive = "https://github.com/$Repo/archive/refs/tags/$Ref.zip"
    }
    'main' {
        $Ref = 'main'
        $Archive = "https://github.com/$Repo/archive/refs/heads/main.zip"
    }
    default {
        $Ref = $Version
        $Archive = "https://github.com/$Repo/archive/refs/tags/$Ref.zip"
    }
}

if ($Ref -ne 'main' -and $Ref -notmatch '^v\d+\.\d+\.\d+$') {
    throw "Not a release tag: $Ref (expected latest, main, or vX.Y.Z)"
}

$Temp = Join-Path ([System.IO.Path]::GetTempPath()) ("code-cycle-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Temp | Out-Null
try {
    Write-Host "Downloading Code Cycle Toolkit $Ref..."
    $Zip = Join-Path $Temp 'toolkit.zip'
    try {
        Invoke-WebRequest -Uri $Archive -OutFile $Zip -UseBasicParsing
    } catch {
        throw "Could not download $Ref. Check that the version exists: https://github.com/$Repo/releases"
    }
    Expand-Archive -Path $Zip -DestinationPath $Temp
    $Source = Get-ChildItem -Path $Temp -Directory | Select-Object -First 1
    $Installer = Join-Path $Source.FullName 'scripts\install.ps1'
    if (-not (Test-Path -LiteralPath $Installer -PathType Leaf)) {
        throw 'The downloaded archive has no scripts\install.ps1.'
    }

    # A child process with -ExecutionPolicy Bypass, as the documented manual
    # command does: under the default Restricted policy, calling the downloaded
    # .ps1 directly from this session would be refused.
    $Shell = (Get-Process -Id $PID).Path
    $Arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $Installer,
        '-Agent', $Agent, '-Scope', $Scope, '-ProjectDir', $ProjectDir, '-Force')
    if ($NoRuntime) { $Arguments += '-NoRuntime' }
    & $Shell @Arguments
    if ($LASTEXITCODE -ne 0) { throw "The installer exited with code $LASTEXITCODE." }
    Write-Host "Code Cycle Toolkit $Ref is installed."
} finally {
    Remove-Item -LiteralPath $Temp -Recurse -Force -ErrorAction SilentlyContinue
}
