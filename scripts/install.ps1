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
function Install-Runtime {
    if (-not (Test-Path -LiteralPath $RuntimeManifest -PathType Leaf)) {
        throw "Runtime manifest not found: $RuntimeManifest"
    }

    $Destination = Join-Path (Join-Path $BaseDir '.code-cycle') 'runtime'
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null

    # The directory holds installed code, never anything a project should carry.
    # Writing the rule next to it means a project-scope installation cannot be
    # committed by accident.
    $Ignore = Join-Path (Join-Path $BaseDir '.code-cycle') '.gitignore'
    Set-Content -LiteralPath $Ignore -Value '*'

    Get-Content -LiteralPath $RuntimeManifest | ForEach-Object {
        $Module = $_.Trim()
        if (-not $Module -or $Module.StartsWith('#')) { return }

        $Source = Join-Path $PSScriptRoot $Module
        if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
            throw "Runtime module listed but missing: $Source"
        }
        $Target = Join-Path $Destination $Module
        if ((Test-Path -LiteralPath $Target) -and (-not $Force)) {
            throw "Already exists: $Target (use -Force to replace it)"
        }
        Copy-Item -LiteralPath $Source -Destination $Target -Force
    }

    Write-Host "Installed runtime -> $Destination"
    Write-Host "Add it to PYTHONPATH to record a cycle: $Destination"
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
