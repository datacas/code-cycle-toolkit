[CmdletBinding()]
param(
    [ValidateSet('all', 'claude', 'codex', 'opencode')]
    [string]$Agent = 'all',
    [ValidateSet('global', 'project')]
    [string]$Scope = 'global',
    [string]$ProjectDir = (Get-Location).Path,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$PackageRoot = Split-Path -Parent $PSScriptRoot
$UserHome = [Environment]::GetFolderPath('UserProfile')
$SkillsRoot = Join-Path $PackageRoot 'skills'

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

Write-Host 'Code Cycle Toolkit installation completed.'
