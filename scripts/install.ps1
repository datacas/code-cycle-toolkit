[CmdletBinding()]
param(
    [ValidateSet('all', 'claude', 'codex', 'opencode')]
    [string]$Agent = 'all',
    [ValidateSet('global', 'project')]
    [string]$Scope = 'global',
    [string]$ProjectDir = (Get-Location).Path,
    [switch]$Force,
    [switch]$NoRuntime
)

$ErrorActionPreference = 'Stop'
$PackageRoot = Split-Path -Parent $PSScriptRoot
$UserHome = [Environment]::GetFolderPath('UserProfile')
$SkillsRoot = Join-Path $PackageRoot 'skills'
$RuntimeManifest = Join-Path $PSScriptRoot 'runtime.manifest'

if (-not (Test-Path -LiteralPath $SkillsRoot -PathType Container)) {
    throw "Skills directory not found: $SkillsRoot"
}

if ($Scope -eq 'project') {
    $BaseDir = (Resolve-Path -LiteralPath $ProjectDir).Path
    if ($BaseDir -eq [IO.Path]::GetPathRoot($BaseDir)) {
        throw 'Refusing to use a filesystem root as the project directory.'
    }
} else {
    $BaseDir = $UserHome
}

function Copy-Skills {
    param([Parameter(Mandatory = $true)][string]$Destination)

    New-Item -ItemType Directory -Path $Destination -Force | Out-Null

    Get-ChildItem -LiteralPath $SkillsRoot -Directory | ForEach-Object {
        $Target = Join-Path $Destination $_.Name
        if (Test-Path -LiteralPath $Target) {
            if (-not $Force) {
                throw "Already exists: $Target (use -Force to replace it)"
            }
            Remove-Item -LiteralPath $Target -Recurse -Force
        }
        Copy-Item -LiteralPath $_.FullName -Destination $Target -Recurse
        Write-Host "Installed $($_.Name) -> $Target"
    }
}

function Install-Claude {
    Copy-Skills (Join-Path $BaseDir '.claude\skills')
}

function Install-Codex {
    # .agents/skills is the cross-agent compatibility path. .codex/skills is
    # retained for Codex installations that use the traditional home path.
    Copy-Skills (Join-Path $BaseDir '.agents\skills')
    if ($Scope -eq 'global') {
        Copy-Skills (Join-Path $BaseDir '.codex\skills')
    }
}

function Install-OpenCode {
    if ($Scope -eq 'project') {
        Copy-Skills (Join-Path $BaseDir '.opencode\skills')
    } else {
        Copy-Skills (Join-Path $BaseDir '.config\opencode\skills')
    }
}

# The runtime is host-independent: one copy per scope, not one per agent. A
# skill is instructions and can be duplicated harmlessly; the runtime is code
# that records a cycle, and three copies of it would be three answers to the
# question of which one a run used.
# The installer writes into a directory somebody else chose, which may be a
# repository it did not create. A reparse point on any path it writes to is a
# request to modify a file outside that directory, so every one of them is
# refused rather than followed: an installation is not a reason to truncate a
# file nobody named.
function Assert-NotReparsePoint {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$What
    )

    # Get-Item rather than Test-Path: a link whose target is gone is still a
    # link, and Test-Path reports it as absent.
    $Item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (-not $Item) { return }

    if ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Refusing to write through a link ($What): $Path"
    }
}

# The directory holds installed code, never anything a project should carry, so
# a fresh installation ignores it. An existing file is somebody's own rules and
# is left exactly as it is - not merged, not replaced by -Force, which asks to
# replace this toolkit's files and not the target project's.
function Install-Gitignore {
    param([Parameter(Mandatory = $true)][string]$Root)

    $Ignore = Join-Path $Root '.gitignore'
    Assert-NotReparsePoint -Path $Ignore -What 'ignore file'

    if (Get-Item -LiteralPath $Ignore -Force -ErrorAction SilentlyContinue) {
        Write-Host "Kept the existing $Ignore; add 'runtime/' to it to leave installed code uncommitted."
        return
    }

    Set-Content -LiteralPath $Ignore -Value '*'
}

function Install-Runtime {
    if (-not (Test-Path -LiteralPath $RuntimeManifest -PathType Leaf)) {
        throw "Runtime manifest not found: $RuntimeManifest"
    }

    $Root = Join-Path $BaseDir '.code-cycle'
    $Destination = Join-Path $Root 'runtime'
    Assert-NotReparsePoint -Path $Root -What 'installation directory'
    Assert-NotReparsePoint -Path $Destination -What 'runtime directory'
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null

    Install-Gitignore -Root $Root

    Get-Content -LiteralPath $RuntimeManifest | ForEach-Object {
        $Module = $_.Trim()
        if (-not $Module -or $Module.StartsWith('#')) { return }

        $Source = Join-Path $PSScriptRoot $Module
        if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
            throw "Runtime module listed but missing: $Source"
        }
        $Target = Join-Path $Destination $Module
        if (Get-Item -LiteralPath $Target -Force -ErrorAction SilentlyContinue) {
            if (-not $Force) {
                throw "Already exists: $Target (use -Force to replace it)"
            }
            # Remove rather than copy over: copying onto a link writes to
            # whatever it points at, which -Force never authorised.
            Remove-Item -LiteralPath $Target -Force
        }
        Copy-Item -LiteralPath $Source -Destination $Target
    }

    Write-Host "Installed runtime -> $Destination"
    Write-Host "Runtime installed at $Destination. run_cycle.py and cc-stats use it as-is; add it to PYTHONPATH only to import its modules from other code."
}

switch ($Agent) {
    'claude' { Install-Claude }
    'codex' { Install-Codex }
    'opencode' { Install-OpenCode }
    'all' {
        Install-Claude
        Install-Codex
        Install-OpenCode
    }
}

if (-not $NoRuntime) {
    Install-Runtime
}

Write-Host 'Code Cycle Toolkit installation completed.'
